"""Device triggers for Häfele remotes ("Position 1: on pressed", ...)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .remote import (
    EVENT_BRIGHTNESS_MAX,
    EVENT_BRIGHTNESS_MIN,
    EVENT_COLOR_TEMP_COLD,
    EVENT_COLOR_TEMP_WARM,
    EVENT_SCENE_A,
    EVENT_SCENE_B,
    EVENT_SCENE_C,
    EVENT_TURN_OFF,
    EVENT_TURN_ON,
    EVENT_TYPES,
    REMOTE_BUS_EVENT,
    module_count,
    module_info,
    remote_device_identifier,
)

CONF_SUBTYPE = "subtype"

# Offered in the automation editor for every enabled position. Saturation
# and hue events exist on the event entity but no control of the remote v2
# was seen sending them, so they are not listed here.
_BASE_TRIGGERS = [
    EVENT_TURN_ON,
    EVENT_TURN_OFF,
    EVENT_BRIGHTNESS_MAX,
    EVENT_BRIGHTNESS_MIN,
    EVENT_COLOR_TEMP_COLD,
    EVENT_COLOR_TEMP_WARM,
]
_SCENE_TRIGGERS = [EVENT_SCENE_A, EVENT_SCENE_B, EVENT_SCENE_C]

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(EVENT_TYPES),
        vol.Required(CONF_SUBTYPE): vol.Match(r"^position_[1-9][0-9]?$"),
    }
)


def _remote_for_device(hass: HomeAssistant, device_id: str) -> dict | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for remote in getattr(coordinator, "remotes_cfg", []):
            if remote_device_identifier(remote) in device.identifiers:
                return remote
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str,
) -> list[dict[str, Any]]:
    remote = _remote_for_device(hass, device_id)
    if remote is None:
        return []
    triggers = []
    for module in range(module_count(remote)):
        info = module_info(remote, module)
        if not info.get("enabled", True):
            continue
        scenes = info.get("scenes") or []
        types = list(_BASE_TRIGGERS)
        types += [t for i, t in enumerate(_SCENE_TRIGGERS) if i < len(scenes) and scenes[i]]
        for trigger_type in types:
            triggers.append({
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_TYPE: trigger_type,
                CONF_SUBTYPE: f"position_{module + 1}",
            })
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    module = int(config[CONF_SUBTYPE].rsplit("_", 1)[1])
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: REMOTE_BUS_EVENT,
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                "module": module,
                CONF_TYPE: config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device",
    )
