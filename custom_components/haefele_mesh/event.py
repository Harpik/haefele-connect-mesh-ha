"""Event entities for Häfele remotes: one per remote module (position 1-6)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaefeleCoordinator
from .remote import (
    EVENT_TYPES,
    RemoteEvent,
    module_count,
    module_info,
    remote_device_identifier,
)

# Translated entity names with placeholders need Home Assistant 2024.2.
_HAS_NAME_PLACEHOLDERS = (MAJOR_VERSION, MINOR_VERSION) >= (2024, 2)


def module_unique_id(remote_cfg: dict, module: int) -> str:
    return f"{remote_device_identifier(remote_cfg)[1]}_module_{module + 1}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaefeleCoordinator = hass.data[DOMAIN][entry.entry_id]
    _enable_positions_turned_on_in_app(hass, coordinator.remotes_cfg)
    entities = [
        HaefeleRemoteModuleEvent(coordinator, remote, module)
        for remote in coordinator.remotes_cfg
        for module in range(module_count(remote))
    ]
    async_add_entities(entities)


def _enable_positions_turned_on_in_app(hass: HomeAssistant, remotes: list[dict]) -> None:
    """Re-enable entities this integration disabled for a then-unused position.

    ``entity_registry_enabled_default`` only applies when the registry entry
    is created. If the position was later enabled in the Häfele app and the
    export re-imported, lift the integration's own disable. A disable set by
    the user is left alone.
    """
    registry = er.async_get(hass)
    for remote in remotes:
        for module in range(module_count(remote)):
            if not module_info(remote, module).get("enabled", True):
                continue
            entity_id = registry.async_get_entity_id(
                "event", DOMAIN, module_unique_id(remote, module),
            )
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity_id, disabled_by=None)


class HaefeleRemoteModuleEvent(EventEntity):
    """Presses on one position of a Häfele remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_event_types = EVENT_TYPES

    def __init__(
        self, coordinator: HaefeleCoordinator, remote_cfg: dict, module: int,
    ) -> None:
        self._coordinator = coordinator
        self._remote_unicast = remote_cfg["unicast"]
        self._module = module
        info = module_info(remote_cfg, module)
        position = str(module + 1)
        group = info.get("name")
        if _HAS_NAME_PLACEHOLDERS:
            self._attr_translation_key = "remote_module_group" if group else "remote_module"
            self._attr_translation_placeholders = {"position": position, "group": group or ""}
        else:
            self._attr_translation_key = "remote_module"
            self._attr_name = f"Position {position} ({group})" if group else f"Position {position}"
        self._attr_unique_id = module_unique_id(remote_cfg, module)
        # Positions disabled in the Häfele app send nothing until enabled.
        self._attr_entity_registry_enabled_default = bool(info.get("enabled", True))
        self._attr_device_info = DeviceInfo(
            identifiers={remote_device_identifier(remote_cfg)},
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
