"""
DataUpdateCoordinator for Häfele Connect Mesh.

Manages BLE connections to all nodes, owns the shared BT Mesh
sequence-number store (persisted via HA Store), and monitors
availability via periodic heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, HEARTBEAT_INTERVAL, SRC_ADDRESS_BASE
from .gatt import MeshGattNode

_LOGGER = logging.getLogger(__name__)

SEQ_STORAGE_VERSION = 1
SEQ_STORAGE_KEY = f"{DOMAIN}_seq"
# Jump forward at startup so we never reuse a SEQ we might have
# emitted but not persisted yet. The BT Mesh SEQ space is 24 bits (~16M).
SEQ_STARTUP_JUMP = 200

# Callback signature for entities registering for status updates
# (opcode, params) -> None
StatusCallback = "Callable[[int, bytes], None]"


class HaefeleCoordinator(DataUpdateCoordinator):
    """Manages all Häfele Mesh nodes and their availability."""

    def __init__(self, hass: HomeAssistant, config: dict):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=HEARTBEAT_INTERVAL),
        )
        self._config = config
        self.nodes: dict[str, MeshGattNode] = {}
        self.availability: dict[str, bool] = {}

        # Routing: incoming mesh src_address -> list of entity callbacks.
        # Entities register the unicast addresses they care about (usually
        # just their own) at setup time.
        self._status_handlers: dict[int, list] = {}

        # SEQ state: {src_address(int) -> seq(int)} persisted via HA Store
        self._seq_store: Store = Store(hass, SEQ_STORAGE_VERSION, SEQ_STORAGE_KEY)
        self._seq_state: dict[int, int] = {}
        self._seq_lock = asyncio.Lock()
        self._seq_dirty = False

    # ------------------------------------------------------------------
    # Status routing
    # ------------------------------------------------------------------

    def register_status_handler(self, src_address: int, callback) -> "Callable[[], None]":
        """Subscribe to status messages coming from a given mesh unicast.

        Returns an unsubscribe callable.
        """
        self._status_handlers.setdefault(src_address, []).append(callback)

        def _unsub() -> None:
            handlers = self._status_handlers.get(src_address, [])
            if callback in handlers:
                handlers.remove(callback)
            if not handlers:
                self._status_handlers.pop(src_address, None)

        return _unsub

    def _dispatch_status(self, src_address: int, opcode: int, params: bytes) -> None:
        """Called by GATT nodes when a decoded access PDU arrives."""
        for cb in self._status_handlers.get(src_address, ()):
            try:
                cb(opcode, params)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Status handler failed for %04X", src_address)

    # ------------------------------------------------------------------
    # SEQ management
    # ------------------------------------------------------------------

    async def _load_seq(self) -> None:
        raw = await self._seq_store.async_load()
        state: dict[int, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    state[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        self._seq_state = state

    async def _save_seq(self) -> None:
        # JSON keys must be strings
        await self._seq_store.async_save(
            {str(k): v for k, v in self._seq_state.items()}
        )
        self._seq_dirty = False

    async def next_seq(self, src_address: int) -> int:
        """Return and persist the next BT Mesh SEQ for a given source address."""
        async with self._seq_lock:
            current = self._seq_state.get(src_address)
            if current is None:
                # First use ever for this src: seed with a time-based value so
                # we don't collide with any historical emissions.
                current = int(time.time()) & 0xFFFFFF
            seq = (current + 1) & 0xFFFFFF
            self._seq_state[src_address] = seq
            # Persist on every emission — cheap (Store debounces writes).
            await self._seq_store.async_save(
                {str(k): v for k, v in self._seq_state.items()}
            )
            return seq

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Initialize all node connections."""
        await self._load_seq()

        # Jump SEQ forward at startup to cover any un-persisted emissions.
        for src, seq in list(self._seq_state.items()):
            self._seq_state[src] = (seq + SEQ_STARTUP_JUMP) & 0xFFFFFF
        await self._save_seq()

        nodes_cfg = self._config.get("nodes", [])
        net_key = self._config["network_key"]
        app_key = self._config["app_key"]
        iv_index = self._config.get("iv_index", 1)
        src_base = self._config.get("src_address_base", SRC_ADDRESS_BASE)

        for i, node_cfg in enumerate(nodes_cfg):
            node_id = _node_id(node_cfg)
            src_address = (src_base + i * 0x10) & 0xFFFF

            node = MeshGattNode(
                hass=self.hass,
                mac=node_cfg["mac"],
                unicast=node_cfg["unicast"],
                net_key_hex=net_key,
                app_key_hex=app_key,
                iv_index=iv_index,
                src_address=src_address,
                seq_provider=self.next_seq,
                name=node_cfg["name"],
                message_handler=self._dispatch_status,
            )
            self.nodes[node_id] = node
            self.availability[node_id] = False

            # Initial connection (best-effort; heartbeat will retry)
            available = await node.connect(timeout=20.0)
            self.availability[node_id] = available

    async def async_shutdown(self) -> None:
        for node in self.nodes.values():
            try:
                await node.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Heartbeat — check connectivity of all nodes."""
        result: dict[str, Any] = {}
        for node_id, node in self.nodes.items():
            try:
                if not node.is_connected:
                    _LOGGER.debug("Reconnecting %s...", node_id)
                    available = await node.connect(timeout=15.0)
                else:
                    available = True

                self.availability[node_id] = available
                result[node_id] = {"available": available}

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Heartbeat failed for %s: %s", node_id, err)
                self.availability[node_id] = False
                result[node_id] = {"available": False}

        return result

    def is_available(self, node_id: str) -> bool:
        return self.availability.get(node_id, False)


def _node_id(node_cfg: dict) -> str:
    """Generate a stable node ID from MAC address."""
    mac = node_cfg["mac"].replace(":", "").lower()
    return f"haefele_{mac}"
