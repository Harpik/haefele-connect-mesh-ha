"""
DataUpdateCoordinator for Häfele Connect Mesh.

Manages BLE connections to all nodes and handles
availability monitoring via periodic heartbeat.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, HEARTBEAT_INTERVAL
from .gatt import MeshGattNode

_LOGGER = logging.getLogger(__name__)


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
        self._adapter = config.get("adapter")

    async def async_setup(self):
        """Initialize all node connections."""
        nodes_cfg = self._config.get("nodes", [])
        net_key = self._config["network_key"]
        app_key = self._config["app_key"]
        iv_index = self._config.get("iv_index", 1)
        src_base = self._config.get("src_address_base", 0x0060)

        for i, node_cfg in enumerate(nodes_cfg):
            node_id = _node_id(node_cfg)
            src_address = (src_base + i * 0x10) & 0xFFFF

            node = MeshGattNode(
                mac=node_cfg["mac"],
                unicast=node_cfg["unicast"],
                net_key_hex=net_key,
                app_key_hex=app_key,
                iv_index=iv_index,
                src_address=src_address,
                adapter=self._adapter,
                name=node_cfg["name"],
            )
            self.nodes[node_id] = node
            self.availability[node_id] = False

            # Initial connection
            available = await node.connect(timeout=20.0)
            self.availability[node_id] = available

    async def _async_update_data(self) -> dict[str, Any]:
        """Heartbeat — check connectivity of all nodes."""
        result = {}
        for node_id, node in self.nodes.items():
            try:
                if not node.is_connected:
                    _LOGGER.debug("Reconnecting %s...", node_id)
                    available = await node.connect(timeout=15.0)
                else:
                    available = True

                self.availability[node_id] = available
                result[node_id] = {"available": available}

            except Exception as err:
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
