"""
Light platform for Häfele Connect Mesh integration.

Supports on/off, brightness and color temperature (Tunable White) via
BT Mesh Generic OnOff + Light CTL server models.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    LIGHT_MAX_KELVIN,
    LIGHT_MIN_KELVIN,
)
from .coordinator import HaefeleCoordinator, _node_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaefeleCoordinator = hass.data[DOMAIN][entry.entry_id]
    nodes_cfg = entry.data.get("nodes", [])

    entities: list[HaefeleLight] = []
    for node_cfg in nodes_cfg:
        nid = _node_id(node_cfg)
        if nid in coordinator.nodes:
            entities.append(HaefeleLight(coordinator, nid, node_cfg))

    async_add_entities(entities)


class HaefeleLight(CoordinatorEntity[HaefeleCoordinator], LightEntity):
    """Representation of a Häfele Connect Mesh tunable-white light."""

    _attr_has_entity_name = True
    _attr_name = None  # entity inherits the device name (single entity per device)
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = LIGHT_MIN_KELVIN
    _attr_max_color_temp_kelvin = LIGHT_MAX_KELVIN

    def __init__(
        self,
        coordinator: HaefeleCoordinator,
        node_id: str,
        node_cfg: dict,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._node_cfg = node_cfg
        self._node = coordinator.nodes[node_id]

        # Destination for mesh messages: first group address, else unicast.
        groups = node_cfg.get("groups", [])
        self._dst = groups[0] if groups else node_cfg["unicast"]

        # Local optimistic state
        self._is_on = False
        self._brightness = 128
        self._color_temp_kelvin = (LIGHT_MIN_KELVIN + LIGHT_MAX_KELVIN) // 2

        mac = node_cfg["mac"]
        mac_clean = mac.replace(":", "").lower()

        # Entity unique_id — stable per (device, capability).
        self._attr_unique_id = f"{mac_clean}_light"

        # Device identifier — shared across any future entities on the same node.
        device_identifier = (DOMAIN, mac_clean)
        self._attr_device_info = DeviceInfo(
            identifiers={device_identifier},
            connections={("mac", mac)},
            name=node_cfg["name"],
            manufacturer="Häfele",
            model=_pretty_model(node_cfg.get("device_type", "")),
            sw_version=node_cfg.get("firmware") or None,
        )

    # ------------------------------------------------------------------
    # Availability / state
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.coordinator.is_available(self._node_id)

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int:
        return self._color_temp_kelvin

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        node = self._node
        dst = self._dst

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._color_temp_kelvin = max(
                LIGHT_MIN_KELVIN,
                min(LIGHT_MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN]),
            )

        try:
            # Always turn on first, then send the detailed command.
            # The small gap lets the mesh propagate the OnOff before CTL/Level.
            await node.set_onoff(dst, True)
            await asyncio.sleep(0.2)

            if ATTR_COLOR_TEMP_KELVIN in kwargs or ATTR_BRIGHTNESS in kwargs:
                lightness = self._brightness_to_lightness(self._brightness)
                await node.set_ctl(dst, lightness, self._color_temp_kelvin)
            else:
                level = self._brightness_to_level(self._brightness)
                await node.set_level(dst, level)

            self._is_on = True
            self.async_write_ha_state()

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn on %s: %s", self._node.name, err)
            self.coordinator.availability[self._node_id] = False
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._node.set_onoff(self._dst, False)
            self._is_on = False
            self.async_write_ha_state()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn off %s: %s", self._node.name, err)
            self.coordinator.availability[self._node_id] = False
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _brightness_to_lightness(brightness: int) -> int:
        """HA brightness (0-255) → BT Mesh lightness (0-65535)."""
        return int(brightness / 255 * 65535)

    @staticmethod
    def _brightness_to_level(brightness: int) -> int:
        """HA brightness (0-255) → Generic Level (-32768 to 32767)."""
        return int((brightness / 255) * 65535) - 32768


def _pretty_model(device_type: str) -> str:
    """Human-readable model name from parsed device_type."""
    mapping = {
        "tunable_white": "Connect Mesh Tunable White",
        "rgb": "Connect Mesh RGB",
        "dimmable": "Connect Mesh Dimmable",
    }
    return mapping.get(device_type, "Connect Mesh Light")
