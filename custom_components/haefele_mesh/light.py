"""
Light platform for Häfele Connect Mesh integration.

Supports four capability tiers, selected per node from
`node_cfg["device_type"]` (populated by `connect_parser.py`):

    tunable_white  → on/off + brightness + color temp (CTL)
    rgb            → on/off + brightness + hue/saturation (HSL)   [experimental]
    dimmable       → on/off + brightness (Light Lightness)
    onoff / other  → on/off only (Generic OnOff)

Status messages from the mesh are decoded so HA state stays in sync
with physical switches and changes made from the Häfele app.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
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
OP_HSL_STATUS = 0x8278


# ---------------------------------------------------------------------------
# Capability resolution — pure, testable
# ---------------------------------------------------------------------------

# Module-level string constants so tests don't need to import the HA
# ColorMode enum (keeps the unit suite offline).
CAP_COLOR_TEMP = "color_temp"
CAP_HS = "hs"
CAP_BRIGHTNESS = "brightness"
CAP_ONOFF = "onoff"


def resolve_capability(device_type: str | None) -> str:
    """Map a parser device_type to one capability tier.

    Pure helper — deliberately returns strings (not ColorMode) so
    tests can run without importing Home Assistant.
    """
    dt = (device_type or "").lower()
    if dt == "tunable_white":
        return CAP_COLOR_TEMP
    if dt == "rgb":
        return CAP_HS
    if dt == "dimmable":
        return CAP_BRIGHTNESS
    if dt in ("onoff", "on_off", "switch", "relay"):
        return CAP_ONOFF
    # Conservative fallback: unknown types get on/off only. This keeps a
    # mystery device usable without risking wrong opcodes being sent at
    # it. Users can override by editing device_type via reconfigure.
    return CAP_ONOFF


_CAP_TO_COLOR_MODE = {
    CAP_COLOR_TEMP: ColorMode.COLOR_TEMP,
    CAP_HS: ColorMode.HS,
    CAP_BRIGHTNESS: ColorMode.BRIGHTNESS,
    CAP_ONOFF: ColorMode.ONOFF,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaefeleCoordinator = hass.data[DOMAIN][entry.entry_id]
    nodes_cfg = entry.data.get("nodes", [])

    entities: list[HaefeleLight] = []
    for node_cfg in nodes_cfg:
        entities.append(HaefeleLight(coordinator, node_cfg))

    async_add_entities(entities)


class HaefeleLight(CoordinatorEntity[HaefeleCoordinator], LightEntity):
    """Representation of a Häfele Connect Mesh light.

    Capability set (color_mode / brightness / temperature) is picked at
    __init__ based on the node's parsed device_type.
    """

    _attr_has_entity_name = True
    _attr_name = None  # entity inherits the device name (single entity per device)

    def __init__(
        self,
        coordinator: HaefeleCoordinator,
        node_cfg: dict,
    ) -> None:
        super().__init__(coordinator)
        self._node_id = _node_id(node_cfg)
        self._node_cfg = node_cfg
        self._unicast = node_cfg["unicast"]
        self._unicast_addr = node_cfg["unicast"]
        self._name_for_log = node_cfg["name"]
        self._device_type = node_cfg.get("device_type") or "unknown"
        self._capability = resolve_capability(self._device_type)

        color_mode = _CAP_TO_COLOR_MODE[self._capability]
        self._attr_color_mode = color_mode
        self._attr_supported_color_modes = {color_mode}

        if self._capability == CAP_COLOR_TEMP:
            self._attr_min_color_temp_kelvin = LIGHT_MIN_KELVIN
            self._attr_max_color_temp_kelvin = LIGHT_MAX_KELVIN

        # Local state (seeded optimistically, corrected by status messages).
        self._is_on = False
        self._brightness = 255
        self._color_temp_kelvin = (LIGHT_MIN_KELVIN + LIGHT_MAX_KELVIN) // 2
        self._hs_color: tuple[float, float] = (0.0, 0.0)

        # One-shot warning for the unverified RGB path.
        self._rgb_warned = False

        self._unsub_status = None

        mac = node_cfg["mac"]
        mac_clean = mac.replace(":", "").lower()
        self._attr_unique_id = f"{mac_clean}_light"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac_clean)},
            connections={("mac", mac)},
            name=node_cfg["name"],
            manufacturer="Häfele",
            model=_pretty_model(self._device_type),
            sw_version=node_cfg.get("firmware") or None,
        )

    # ------------------------------------------------------------------
    # Entity lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscribe to status messages and request an initial sync."""
        await super().async_added_to_hass()

        self._unsub_status = self.coordinator.register_status_handler(
            self._unicast, self._on_status
        )

        async def _initial_sync() -> None:
            try:
                proxy = self.coordinator.proxy
                if proxy is None or not await proxy.ensure_connected():
                    return
                if self._capability == CAP_COLOR_TEMP:
                    await proxy.get_ctl(self._unicast)
                elif self._capability == CAP_HS:
                    # Only fire HSL Get if gatt.py exposes it (it does
                    # in >=0.4.0; older versions would AttributeError).
                    get_hsl = getattr(proxy, "get_hsl", None)
                    if get_hsl is not None:
                        await get_hsl(self._unicast)
                elif self._capability == CAP_BRIGHTNESS:
                    await proxy.get_lightness(self._unicast)
                await asyncio.sleep(0.15)
                await proxy.get_onoff(self._unicast)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Initial status sync for %s skipped: %s",
                    self._name_for_log, err,
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
    def brightness(self) -> int | None:
        if self._capability == CAP_ONOFF:
            return None
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._capability != CAP_COLOR_TEMP:
            return None
        return self._color_temp_kelvin

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if self._capability != CAP_HS:
            return None
        return self._hs_color

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
            _LOGGER.exception("Failed to apply status 0x%04X on %s", opcode, self._name_for_log)
            return
        if changed:
            self.coordinator.availability[self._node_id] = True
            self.async_write_ha_state()

    def _apply_status(self, opcode: int, params: bytes) -> bool:
        """Return True if local state changed."""
        before = (
            self._is_on, self._brightness, self._color_temp_kelvin, self._hs_color,
        )

        if opcode == OP_ONOFF_STATUS:
            if len(params) >= 1:
                self._is_on = bool(params[0])

        elif opcode == OP_LEVEL_STATUS:
            if len(params) >= 2 and self._capability != CAP_ONOFF:
                level = struct.unpack_from("<h", params, 0)[0]
                self._brightness = self._level_to_brightness(level)
                self._is_on = self._brightness > 0

        elif opcode == OP_LIGHTNESS_STATUS:
            if len(params) >= 2 and self._capability != CAP_ONOFF:
                lightness = struct.unpack_from("<H", params, 0)[0]
                self._brightness = self._lightness_to_brightness(lightness)
                self._is_on = lightness > 0

        elif opcode == OP_CTL_STATUS:
            if len(params) >= 4 and self._capability == CAP_COLOR_TEMP:
                lightness, temperature = struct.unpack_from("<HH", params, 0)
                self._brightness = self._lightness_to_brightness(lightness)
                self._color_temp_kelvin = max(
                    LIGHT_MIN_KELVIN, min(LIGHT_MAX_KELVIN, temperature or self._color_temp_kelvin)
                )
                self._is_on = lightness > 0

        elif opcode == OP_HSL_STATUS:
            # [lightness(2), hue(2), saturation(2), (remaining_time(1))?]
            if len(params) >= 6 and self._capability == CAP_HS:
                lightness, hue, saturation = struct.unpack_from("<HHH", params, 0)
                self._brightness = self._lightness_to_brightness(lightness)
                self._hs_color = (
                    (hue / 65535.0) * 360.0,
                    (saturation / 65535.0) * 100.0,
                )
                self._is_on = lightness > 0
        else:
            return False

        return (
            self._is_on, self._brightness, self._color_temp_kelvin, self._hs_color,
        ) != before

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        proxy = self.coordinator.proxy
        if proxy is None:
            _LOGGER.warning("No mesh proxy available for %s", self._name_for_log)
            return

        # Brightness bookkeeping (ignored for ONOFF-only devices).
        if self._capability != CAP_ONOFF:
            if ATTR_BRIGHTNESS in kwargs:
                self._brightness = kwargs[ATTR_BRIGHTNESS]
            elif not self._is_on and (self._brightness or 0) <= 0:
                self._brightness = 255

        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._capability == CAP_COLOR_TEMP:
            self._color_temp_kelvin = max(
                LIGHT_MIN_KELVIN,
                min(LIGHT_MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN]),
            )

        if ATTR_HS_COLOR in kwargs and self._capability == CAP_HS:
            self._hs_color = kwargs[ATTR_HS_COLOR]

        try:
            if self._capability == CAP_COLOR_TEMP:
                lightness = self._brightness_to_lightness(self._brightness)
                if ATTR_COLOR_TEMP_KELVIN in kwargs:
                    await proxy.set_ctl(
                        self._unicast_addr, lightness, self._color_temp_kelvin,
                    )
                else:
                    await proxy.set_lightness(self._unicast_addr, lightness)

            elif self._capability == CAP_HS:
                if not self._rgb_warned:
                    _LOGGER.warning(
                        "%s: using experimental HSL opcode (0x8277) for RGB control. "
                        "If colour changes don't apply, please open an issue with a "
                        "diagnostics dump — your device may use a vendor opcode.",
                        self._name_for_log,
                    )
                    self._rgb_warned = True
                set_hsl = getattr(proxy, "set_hsl", None)
                if set_hsl is None:
                    _LOGGER.error(
                        "Proxy does not expose set_hsl — upgrade haefele_mesh to "
                        "a version with HSL support.",
                    )
                    return
                lightness = self._brightness_to_lightness(self._brightness)
                hue_raw = int((self._hs_color[0] / 360.0) * 65535) & 0xFFFF
                sat_raw = int((self._hs_color[1] / 100.0) * 65535) & 0xFFFF
                await set_hsl(self._unicast_addr, lightness, hue_raw, sat_raw)

            elif self._capability == CAP_BRIGHTNESS:
                lightness = self._brightness_to_lightness(self._brightness)
                await proxy.set_lightness(self._unicast_addr, lightness)

            else:  # CAP_ONOFF
                await proxy.set_onoff(self._unicast_addr, True)

            self._is_on = True
            self.async_write_ha_state()

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn on %s: %s", self._name_for_log, err)
            self.coordinator.availability[self._node_id] = False
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        proxy = self.coordinator.proxy
        if proxy is None:
            return
        try:
            if self._capability == CAP_ONOFF:
                await proxy.set_onoff(self._unicast_addr, False)
            else:
                # Lightness=0 implies OnOff=0 on Light Lightness / CTL /
                # HSL servers alike — one frame, no race with OnOff.
                await proxy.set_lightness(self._unicast_addr, 0)
            self._is_on = False
            self.async_write_ha_state()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn off %s: %s", self._name_for_log, err)
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


_SHARED_GROUPS = {0xC002, 0xC003, 0xC006, 0xC007, 0xC017, 0xC018}


def _pretty_model(device_type: str) -> str:
    """Human-readable model name from parsed device_type."""
    mapping = {
        "tunable_white": "Connect Mesh Tunable White",
        "rgb": "Connect Mesh RGB",
        "dimmable": "Connect Mesh Dimmable",
        "onoff": "Connect Mesh Switch",
    }
    return mapping.get(device_type, "Connect Mesh Light")
