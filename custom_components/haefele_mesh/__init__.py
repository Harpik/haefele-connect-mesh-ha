"""Häfele Connect Mesh integration for Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, LEGACY_SRC_ADDRESSES, SRC_ADDRESS_BASE
from .coordinator import HaefeleCoordinator
from .remote import (
    REMOTE_BUS_EVENT,
    RemoteEvent,
    bus_event_data,
    remote_device_identifier,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "event", "select"]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries.

    v1 -> v2: earlier builds persisted ``src_address_base`` but the
    coordinator read the wrong key, so the mesh SRC was pinned to whatever
    constant shipped at install time. Deleting the SEQ store then rewound
    that SRC below the lamps' replay watermark, so every frame we emitted
    was silently dropped. Rewrite any legacy/default SRC to the current
    SRC_ADDRESS_BASE (a value the lamps have never seen -> empty replay
    list -> they accept us again). A genuinely custom SRC is left intact.
    """
    if entry.version > 2:
        # Downgrade: refuse rather than corrupt newer data.
        return False

    if entry.version == 1:
        data = {**entry.data}
        old = data.get("src_address_base")
        if old is None or old in LEGACY_SRC_ADDRESSES:
            data["src_address_base"] = SRC_ADDRESS_BASE
            _LOGGER.info(
                "Migrating SRC address %s -> 0x%04X (replay-cache reset)",
                hex(old) if isinstance(old, int) else old,
                SRC_ADDRESS_BASE,
            )
        hass.config_entries.async_update_entry(entry, data=data, version=2)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Häfele Connect Mesh from a config entry."""
    # Bad entry data cannot fix itself: fail for good instead of retrying.
    problem = _entry_data_problem(entry.data)
    if problem is not None:
        raise ConfigEntryError(f"Invalid Häfele Mesh configuration: {problem}")

    coordinator = HaefeleCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)

    # Anything else that fails during setup (Bluetooth, storage, the first
    # refresh) raises ConfigEntryNotReady so Home Assistant retries it with
    # backoff. Whatever the coordinator already started (poll loop, proxy
    # link) is torn down first; otherwise every retry would leave one running.
    try:
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        await coordinator.async_shutdown()
        if isinstance(err, (ConfigEntryNotReady, ConfigEntryError)):
            raise
        _LOGGER.debug("Häfele Mesh setup failed, will retry", exc_info=True)
        raise ConfigEntryNotReady(
            f"Failed to set up Häfele Mesh coordinator: {err}"
        ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(
        coordinator.register_remote_listener(_remote_bus_forwarder(hass, coordinator))
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _remote_bus_forwarder(hass: HomeAssistant, coordinator: HaefeleCoordinator):
    """Fire every remote press on the bus for device triggers.

    Done here rather than in the event entity so triggers keep working
    when the user disables a position's entity.
    """

    def _forward(event: RemoteEvent) -> None:
        remote = next(
            (r for r in coordinator.remotes_cfg if r.get("unicast") == event.remote_unicast),
            None,
        )
        if remote is None:
            return
        device = dr.async_get(hass).async_get_device(
            identifiers={remote_device_identifier(remote)},
        )
        if device is not None:
            hass.bus.async_fire(REMOTE_BUS_EVENT, bus_event_data(device.id, event))

    return _forward


def _entry_data_problem(data: Mapping[str, Any]) -> str | None:
    """Describe what is wrong with the stored entry data, or None if usable."""
    for key in ("network_key", "app_key"):
        # Same parsing as MeshSession, plus the 128-bit length mesh keys have.
        value = data.get(key)
        try:
            valid = isinstance(value, str) and len(bytes.fromhex(value)) == 16
        except ValueError:
            valid = False
        if not valid:
            return f"{key} is missing or not a 128-bit hex key"
    if not isinstance(data.get("nodes", []), list):
        return "nodes is not a list"
    return None


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry,
) -> bool:
    """Allow deleting a remote that is no longer in the export.

    A remote reset and re-paired in the Häfele app gets a new UUID, so after
    a Reconfigure its old device would otherwise stay forever.
    """
    ours = {i for i in device.identifiers if i[0] == DOMAIN}
    if not ours or not all(i[1].startswith("remote_") for i in ours):
        return False
    coordinator: HaefeleCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return False
    current = {remote_device_identifier(r) for r in coordinator.remotes_cfg}
    return not ours & current


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Häfele Connect Mesh config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: HaefeleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
