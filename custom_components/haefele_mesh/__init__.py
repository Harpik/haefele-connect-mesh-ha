"""Häfele Connect Mesh integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import DOMAIN, LEGACY_SRC_ADDRESSES, SRC_ADDRESS_BASE
from .coordinator import HaefeleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light"]


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
    coordinator = HaefeleCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)

    # A transient failure raises ConfigEntryNotReady so Home Assistant retries
    # it with backoff. Bad entry data (malformed keys, missing fields) cannot
    # fix itself, so it raises ConfigEntryError and is not retried. Either
    # way, whatever the coordinator already started (poll loop, proxy link)
    # is torn down first; otherwise every retry would leave one running.
    try:
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        await coordinator.async_shutdown()
        if isinstance(err, (ConfigEntryNotReady, ConfigEntryError)):
            raise
        if isinstance(err, (ValueError, KeyError, TypeError)):
            _LOGGER.exception("Häfele Mesh entry data is invalid")
            raise ConfigEntryError(
                f"Invalid Häfele Mesh configuration: {err!r}"
            ) from err
        _LOGGER.debug("Häfele Mesh setup failed, will retry", exc_info=True)
        raise ConfigEntryNotReady(
            f"Failed to set up Häfele Mesh coordinator: {err}"
        ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Häfele Connect Mesh config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: HaefeleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
