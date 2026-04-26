"""
DataUpdateCoordinator for Häfele Connect Mesh.

Owns a single shared MeshProxyConnection (one GATT link into the mesh)
and exposes typed send helpers for entities. Nodes with no Proxy
feature are reached through whichever node currently holds the active
proxy role, via advertising bearer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    HEARTBEAT_INTERVAL,
    SEQ_SEED_MIN,
    SRC_ADDRESS_BASE,
)
from .gatt import MeshProxyConnection, MeshSession

_LOGGER = logging.getLogger(__name__)

SEQ_STORAGE_VERSION = 1
SEQ_STORAGE_KEY = f"{DOMAIN}_seq"
SEQ_STARTUP_JUMP = 200

# How often we poll each node for its current state. External control
# (wall remotes, Häfele app) doesn't reliably publish Status to groups
# we subscribe to, so polling is the simplest way to keep HA in sync.
STATE_POLL_INTERVAL = 15  # seconds
# Small gap between the Gets we send to different nodes to avoid
# flooding the proxy link.
STATE_POLL_PER_NODE_GAP = 0.2


class HaefeleCoordinator(DataUpdateCoordinator):
    """Owns mesh session + single proxy connection for all nodes."""

    def __init__(self, hass: HomeAssistant, config: dict):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=HEARTBEAT_INTERVAL),
        )
        self._config = config
        # {unicast_address -> list[callback(opcode, params)]}
        self._status_handlers: dict[int, list[Callable[[int, bytes], None]]] = {}
        # SEQ store (shared by our single SRC, but kept as a map for safety
        # in case SRC_ADDRESS_BASE is bumped in future releases).
        self._seq_store: Store = Store(hass, SEQ_STORAGE_VERSION, SEQ_STORAGE_KEY)
        self._seq_state: dict[int, int] = {}
        self._seq_lock = asyncio.Lock()

        self.session: MeshSession | None = None
        self.proxy: MeshProxyConnection | None = None
        self._nodes_cfg: list[dict] = config.get("nodes", [])
        self._poll_task: asyncio.Task | None = None
        # Per-node availability (True means the mesh is reachable *and* we
        # have no reason to believe the node specifically is offline; we
        # don't currently track per-node liveness beyond "mesh is up").
        self.availability: dict[str, bool] = {
            _node_id(n): False for n in self._nodes_cfg
        }

    # ------------------------------------------------------------------
    # Status routing
    # ------------------------------------------------------------------

    def register_status_handler(
        self, src_address: int, callback: Callable[[int, bytes], None]
    ) -> Callable[[], None]:
        self._status_handlers.setdefault(src_address, []).append(callback)

        def _unsub() -> None:
            handlers = self._status_handlers.get(src_address, [])
            if callback in handlers:
                handlers.remove(callback)
            if not handlers:
                self._status_handlers.pop(src_address, None)

        return _unsub

    def _dispatch_status(self, src_address: int, opcode: int, params: bytes) -> None:
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
        await self._seq_store.async_save(
            {str(k): v for k, v in self._seq_state.items()}
        )

    async def next_seq(self, src_address: int) -> int:
        async with self._seq_lock:
            current = self._seq_state.get(src_address)
            if current is None:
                current = max(SEQ_SEED_MIN, int(time.time()) & 0xFFFFFF)
                _LOGGER.info(
                    "Seeding fresh SEQ for SRC 0x%04X at %d", src_address, current,
                )
            seq = (current + 1) & 0xFFFFFF
            self._seq_state[src_address] = seq
            await self._seq_store.async_save(
                {str(k): v for k, v in self._seq_state.items()}
            )
            return seq

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._load_seq()
        # Jump SEQ forward on startup for every known SRC to swallow any
        # un-persisted emissions around the last unclean shutdown.
        for src, seq in list(self._seq_state.items()):
            self._seq_state[src] = (seq + SEQ_STARTUP_JUMP) & 0xFFFFFF
        await self._save_seq()

        net_key = self._config["network_key"]
        app_key = self._config["app_key"]
        iv_index = self._config.get("iv_index", 1)

        # One SRC for the whole integration. SRC_ADDRESS_BASE is chosen to
        # be fresh vs the Haefele app (provisioner address, usually 0x7FFD)
        # and any earlier gateway implementation.
        src_address = self._config.get("src_address", SRC_ADDRESS_BASE) & 0xFFFF

        self.session = MeshSession(
            net_key_hex=net_key,
            app_key_hex=app_key,
            src_address=src_address,
            iv_index=iv_index,
            seq_provider=self.next_seq,
        )
        self.proxy = MeshProxyConnection(
            hass=self.hass,
            session=self.session,
            message_handler=self._dispatch_status,
        )
        self.proxy.set_candidates([
            (n["mac"], n["name"]) for n in self._nodes_cfg if n.get("mac")
        ])
        self.proxy.set_filter_addresses(self._filter_addresses())

        ok = await self.proxy.connect_any()
        # Mark every node as available if *any* proxy is up — they're all
        # reachable through the mesh from there.
        for nid in self.availability:
            self.availability[nid] = ok

        # Start the state-polling loop.
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._state_poll_loop(), name="haefele-state-poll",
            )

    async def async_shutdown(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poll_task = None
        if self.proxy is not None:
            try:
                await self.proxy.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Heartbeat — just keep the single connection alive
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self.proxy is None:
            return {nid: {"available": False} for nid in self.availability}

        ok = self.proxy.is_connected
        if not ok:
            _LOGGER.debug("Proxy disconnected, trying to reconnect...")
            ok = await self.proxy.connect_any()

        for nid in self.availability:
            self.availability[nid] = ok
        return {nid: {"available": ok} for nid in self.availability}

    def _filter_addresses(self) -> list[int]:
        """Compute the set of DST addresses the proxy should forward.

        Includes:
          * our own SRC (so unicast Status replies make it back)
          * every lamp unicast (replies overheard via relay)
          * every group configured on the lamps (so publications from
            the physical remote or the Häfele app come through)
        """
        addrs: set[int] = set()
        if self.session is not None:
            addrs.add(self.session.src & 0xFFFF)
        for n in self._nodes_cfg:
            unicast = n.get("unicast")
            if isinstance(unicast, int):
                addrs.add(unicast & 0xFFFF)
            for g in n.get("groups", []) or []:
                if isinstance(g, int):
                    addrs.add(g & 0xFFFF)
        return sorted(addrs)

    def is_available(self, node_id: str) -> bool:
        return self.availability.get(node_id, False)

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------

    async def _state_poll_loop(self) -> None:
        """Poll each node for its current on/off + CTL state.

        Keeps HA in sync with physical remote presses and Häfele-app
        changes — the lamps don't reliably publish status to groups
        that we subscribe to, so active polling is the most robust path.
        """
        # Small initial delay so initial_sync gets to run first.
        await asyncio.sleep(3.0)
        while True:
            try:
                await asyncio.sleep(STATE_POLL_INTERVAL)
                if self.proxy is None or not self.proxy.is_connected:
                    continue
                for node_cfg in self._nodes_cfg:
                    unicast = node_cfg.get("unicast")
                    if not unicast:
                        continue
                    try:
                        await self.proxy.get_onoff(unicast)
                        await asyncio.sleep(STATE_POLL_PER_NODE_GAP)
                        await self.proxy.get_ctl(unicast)
                        await asyncio.sleep(STATE_POLL_PER_NODE_GAP)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "State poll for %s failed: %s",
                            node_cfg.get("name", "?"), err,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("State poll loop crashed, continuing")


def _node_id(node_cfg: dict) -> str:
    mac = node_cfg["mac"].replace(":", "").lower()
    return f"haefele_{mac}"
