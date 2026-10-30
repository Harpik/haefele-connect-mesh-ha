"""Event entities for Häfele remotes: one per remote module (position 1-6)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaefeleCoordinator
from .remote import EVENT_TYPES, RemoteEvent, module_count

# Fired on the bus for device triggers (see device_trigger.py).
REMOTE_BUS_EVENT = f"{DOMAIN}_remote_event"


def remote_device_identifier(remote_cfg: dict) -> tuple[str, str]:
    """Stable device-registry identifier for a remote."""
    key = remote_cfg.get("uuid") or f"{remote_cfg.get('unicast', 0):04x}"
    return (DOMAIN, f"remote_{str(key).lower()}")


def module_info(remote_cfg: dict, module: int) -> dict:
    modules = remote_cfg.get("modules") or []
    if module < len(modules):
        return modules[module]
    return {"index": module, "enabled": True, "name": None, "scenes": []}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaefeleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        HaefeleRemoteModuleEvent(coordinator, remote, module)
        for remote in coordinator.remotes_cfg
        for module in range(module_count(remote))
    ]
    async_add_entities(entities)


class HaefeleRemoteModuleEvent(EventEntity):
    """Presses on one position of a Häfele remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "remote_module"

    def __init__(
        self, coordinator: HaefeleCoordinator, remote_cfg: dict, module: int,
    ) -> None:
        self._coordinator = coordinator
        self._remote_unicast = remote_cfg["unicast"]
        self._module = module
        info = module_info(remote_cfg, module)
        position = module + 1
        controls = info.get("name")
        self._attr_name = (
            f"Position {position} ({controls})" if controls else f"Position {position}"
        )
        identifier = remote_device_identifier(remote_cfg)
        self._attr_unique_id = f"{identifier[1]}_module_{position}"
        # Positions disabled in the Häfele app send nothing until enabled.
        self._attr_entity_registry_enabled_default = bool(info.get("enabled", True))
        self._attr_device_info = DeviceInfo(
            identifiers={identifier},
            name=remote_cfg.get("name") or "Häfele remote",
            manufacturer="Häfele",
            model=remote_cfg.get("type") or "Remote",
            sw_version=remote_cfg.get("firmware") or None,
        )
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        self._unsub = self._coordinator.register_remote_listener(self._on_remote_event)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_remote_event(self, event: RemoteEvent) -> None:
        if event.remote_unicast != self._remote_unicast or event.module != self._module:
            return
        self._trigger_event(event.event_type, dict(event.attributes))
        self.async_write_ha_state()
        if self.registry_entry is not None and self.registry_entry.device_id:
            self.hass.bus.async_fire(REMOTE_BUS_EVENT, {
                "device_id": self.registry_entry.device_id,
                "module": self._module + 1,
                "type": event.event_type,
                **event.attributes,
            })
