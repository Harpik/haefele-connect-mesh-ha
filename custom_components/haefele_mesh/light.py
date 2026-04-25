"""
Light platform for Häfele Connect Mesh integration.

Supports on/off, brightness and color temperature (Tunable White) via
BT Mesh Generic OnOff + Light CTL server models. Receives status
messages from the mesh so HA state stays in sync with physical
switches and changes made from the Häfele app.
"""

from __future__ import annotations

import asyncio
import logging
import struct
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


# BT Mesh status opcodes we understand
OP_ONOFF_STATUS = 0x8204
OP_LEVEL_STATUS = 0x8208
OP_LIGHTNESS_STATUS = 0x824E
OP_CTL_STATUS = 0x8260


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
        self._unicast = node_cfg["unicast"]

        # Destination for outbound mesh messages: first group address, else unicast.
        groups = node_cfg.get("groups", [])
        self._dst = groups[0] if groups else node_cfg["unicast"]

        # Local state (seeded optimistically, corrected by status messages)
        self._is_on = False
        self._brightness = 128
        self._color_temp_kelvin = (LIGHT_MIN_KELVIN + LIGHT_MAX_KELVIN) // 2

        self._unsub_status = None

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
    # Entity lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscribe to status messages and request an initial sync."""
        await super().async_added_to_hass()

        # Listen for status messages coming from our unicast address
        self._unsub_status = self.coordinator.register_status_handler(
            self._unicast, self._on_status
        )

        # Fire-and-forget initial state request. CTL Get covers lightness +
        # temperature + implicit on/off (presentLightness>0 means on).
        async def _initial_sync() -> None:
            try:
                if not await self._node.ensure_connected(timeout=10.0):
                    return
                await self._node.get_ctl(self._unicast)
                # Also ask OnOff explicitly — some firmwares only answer the
                # model you asked. Small delay to avoid flooding.
                await asyncio.sleep(0.15)
                await self._node.get_onoff(self._unicast)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Initial status sync for %s skipped: %s", self._node.name, err
                )

        self.hass.async_create_task(_initial_sync())

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_status is not None:
            self._unsub_status()
            self._unsub_status = None
        await super().async_will_remove_from_hass()

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
    # Incoming status dispatch
    # ------------------------------------------------------------------

    @callback
    def _on_status(self, opcode: int, params: bytes) -> None:
        """Apply a decoded status message from this light's unicast."""
        try:
            changed = self._apply_status(opcode, params)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to apply status 0x%04X on %s", opcode, self._node.name)
            return
        if changed:
            # Coordinator ensures node is available if we're hearing from it.
            self.coordinator.availability[self._node_id] = True
            self.async_write_ha_state()

    def _apply_status(self, opcode: int, params: bytes) -> bool:
        """Return True if local state changed."""
        before = (self._is_on, self._brightness, self._color_temp_kelvin)

        if opcode == OP_ONOFF_STATUS:
            # [present_onoff(1), (target_onoff(1), remaining_time(1))?]
            if len(params) >= 1:
                self._is_on = bool(params[0])

        elif opcode == OP_LEVEL_STATUS:
            # [present_level(2 signed LE), (target(2), remaining(1))?]
            if len(params) >= 2:
                level = struct.unpack_from("<h", params, 0)[0]
                self._brightness = self._level_to_brightness(level)
                self._is_on = self._brightness > 0

        elif opcode == OP_LIGHTNESS_STATUS:
            # [present_lightness(2 LE), (target(2), remaining(1))?]
            if len(params) >= 2:
                lightness = struct.unpack_from("<H", params, 0)[0]
                self._brightness = self._lightness_to_brightness(lightness)
                self._is_on = lightness > 0

        elif opcode == OP_CTL_STATUS:
            # [present_lightness(2), present_temperature(2),
            #  (target_lightness(2), target_temp(2), remaining(1))?]
            if len(params) >= 4:
                lightness, temperature = struct.unpack_from("<HH", params, 0)
                self._brightness = self._lightness_to_brightness(lightness)
                # Clamp to advertised range to avoid "out of bounds" warnings.
                self._color_temp_kelvin = max(
                    LIGHT_MIN_KELVIN, min(LIGHT_MAX_KELVIN, temperature or self._color_temp_kelvin)
                )
                self._is_on = lightness > 0
        else:
            return False

        return (self._is_on, self._brightness, self._color_temp_kelvin) != before

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

    @staticmethod
    def _lightness_to_brightness(lightness: int) -> int:
        """BT Mesh lightness (0-65535) → HA brightness (0-255)."""
        return max(0, min(255, round(lightness / 65535 * 255)))

    @staticmethod
    def _level_to_brightness(level: int) -> int:
        """Generic Level (-32768..32767) → HA brightness (0-255)."""
        return max(0, min(255, round((level + 32768) / 65535 * 255)))


def _pretty_model(device_type: str) -> str:
    """Human-readable model name from parsed device_type."""
    mapping = {
        "tunable_white": "Connect Mesh Tunable White",
        "rgb": "Connect Mesh RGB",
        "dimmable": "Connect Mesh Dimmable",
    }
    return mapping.get(device_type, "Connect Mesh Light")
