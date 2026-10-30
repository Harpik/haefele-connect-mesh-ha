"""Select entities: what each position of a Häfele remote does.

``app`` keeps what the Häfele app configured, ``ha_only`` makes the
position control nothing but Home Assistant, ``disabled`` turns it off.
A change is sent the next time the remote is pressed (it sleeps between
presses) and shows as pending until the remote confirms it.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaefeleCoordinator
from .event import _HAS_NAME_PLACEHOLDERS, module_unique_id
from .remote import module_count, remote_device_identifier


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaefeleCoordinator = hass.data[DOMAIN][entry.entry_id]
    configurator = coordinator.remote_config
    async_add_entities(
        HaefeleRemotePositionMode(coordinator, remote, module)
        for remote in coordinator.remotes_cfg
        # Without the remote's device key (exports imported before this
        # feature) it cannot be configured; a Reconfigure stores the key.
        if configurator.can_configure(remote)
        for module in range(min(module_count(remote), len(remote.get("modules") or [])))
    )


class HaefeleRemotePositionMode(SelectEntity):
    """Mode of one position of a Häfele remote."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "remote_position_mode"

    def __init__(
        self, coordinator: HaefeleCoordinator, remote_cfg: dict, module: int,
    ) -> None:
        self._configurator = coordinator.remote_config
        self._remote_cfg = remote_cfg
        self._module = module
        position = str(module + 1)
        if _HAS_NAME_PLACEHOLDERS:
            self._attr_translation_placeholders = {"position": position}
        else:
            self._attr_name = f"Position {position} mode"
        self._attr_unique_id = f"{module_unique_id(remote_cfg, module)}_mode"
        self._attr_options = self._configurator.modes(remote_cfg, module)
        self._attr_device_info = DeviceInfo(identifiers={remote_device_identifier(remote_cfg)})
        self._unsub = None

    @property
    def current_option(self) -> str:
        return self._configurator.mode(self._remote_cfg, self._module)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "pending": self._configurator.is_pending(self._remote_cfg, self._module),
            "error": self._configurator.has_error(self._remote_cfg, self._module),
        }

    @property
    def icon(self) -> str:
        if self._configurator.is_pending(self._remote_cfg, self._module):
            return "mdi:timer-sand"
        return "mdi:remote"

    async def async_select_option(self, option: str) -> None:
        await self._configurator.async_set_mode(self._remote_cfg, self._module, option)

    async def async_added_to_hass(self) -> None:
        self._unsub = self._configurator.register_listener(self._on_change)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()
