"""Häfele Connect Mesh integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HaefeleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Häfele Connect Mesh from a config entry."""
    coordinator = HaefeleCoordinator(hass, dict(entry.data))

    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.error("Failed to set up Häfele Mesh coordinator: %s", err)
        return False

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Häfele Connect Mesh config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: HaefeleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Disconnect all nodes
        for node in coordinator.nodes.values():
            try:
                await node.disconnect()
            except Exception:
                pass

    return unload_ok
