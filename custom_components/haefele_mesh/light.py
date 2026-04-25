"""
Light platform for Häfele Connect Mesh integration.

Supports on/off, brightness and color temperature
for Tunable White nodes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    LIGHT_MAX_MIREDS,
    LIGHT_MIN_MIREDS,
    LIGHT_MIN_KELVIN,
    LIGHT_MAX_KELVIN,
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

    entities = []
    for node_cfg in nodes_cfg:
        nid = _node_id(node_cfg)
        if nid in coordinator.nodes:
            entities.append(
                HaefeleLight(coordinator, nid, node_cfg)
            )

    async_add_entities(entities)


class HaefeleLight(CoordinatorEntity, LightEntity):
    """Representation of a Häfele Connect Mesh light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = LIGHT_MIN_KELVIN
    _attr_max_color_temp_kelvin = LIGHT_MAX_KELVIN
    _attr_min_mireds = LIGHT_MIN_MIREDS
    _attr_max_mireds = LIGHT_MAX_MIREDS

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

        # Choose destination: first group or unicast
        groups = node_cfg.get("groups", [])
        self._dst = groups[0] if groups else node_cfg["unicast"]

        # State
        self._is_on = False
        self._brightness = 128
        self._color_temp = 300  # mireds (~3300K)

        # HA entity metadata
        mac_clean = node_cfg["mac"].replace(":", "").lower()
        self._attr_unique_id = f"haefele_{mac_clean}"
        self._attr_name = node_cfg["name"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": node_cfg["name"],
            "manufacturer": "Häfele",
            "model": "Connect Mesh Tunable White",
            "connections": {("mac", node_cfg["mac"])},
        }

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
    def color_temp(self) -> int:
        return self._color_temp

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        node = self._node
        dst = self._dst

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP in kwargs:
            self._color_temp = kwargs[ATTR_COLOR_TEMP]

        try:
            if ATTR_COLOR_TEMP in kwargs or ATTR_BRIGHTNESS in kwargs:
                # Use CTL if color temp is requested
                kelvin = self._mireds_to_kelvin(self._color_temp)
                lightness = self._brightness_to_lightness(self._brightness)
                await node.set_onoff(dst, True)
                await __import__('asyncio').sleep(0.2)
                await node.set_ctl(dst, lightness, kelvin)
            else:
                # Simple on with level
                level = self._brightness_to_level(self._brightness)
                await node.set_onoff(dst, True)
                await __import__('asyncio').sleep(0.2)
                await node.set_level(dst, level)

            self._is_on = True
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Failed to turn on %s: %s", self.name, err)
            self.coordinator.availability[self._node_id] = False
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._node.set_onoff(self._dst, False)
            self._is_on = False
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Failed to turn off %s: %s", self.name, err)
            self.coordinator.availability[self._node_id] = False
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mireds_to_kelvin(mireds: int) -> int:
        kelvin = 1000000 // max(1, mireds)
        return max(LIGHT_MIN_KELVIN, min(LIGHT_MAX_KELVIN, kelvin))

    @staticmethod
    def _brightness_to_lightness(brightness: int) -> int:
        """HA brightness (0-255) → BT Mesh lightness (0-65535)"""
        return int(brightness / 255 * 65535)

    @staticmethod
    def _brightness_to_level(brightness: int) -> int:
        """HA brightness (0-255) → Generic Level (-32768 to 32767)"""
        return int((brightness / 255) * 65535) - 32768
