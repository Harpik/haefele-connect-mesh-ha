"""
Config flow for Häfele Connect Mesh integration.

Guides the user through:
1. Uploading the .connect file exported from the Häfele app
2. Confirming discovered devices

The Bluetooth adapter is managed by Home Assistant's core
`bluetooth` integration (including ESPHome Bluetooth Proxies),
so there is no adapter selection step here.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, SRC_ADDRESS_BASE
from .connect_parser import parse_connect_file

_LOGGER = logging.getLogger(__name__)


class HaefeleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Häfele Connect Mesh configuration flow."""

    VERSION = 1

    def __init__(self):
        self._parsed_config: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Upload .connect file."""
        errors: dict[str, str] = {}

        if user_input is not None:
            connect_content = user_input.get("connect_file", "")
            try:
                parsed = await self.hass.async_add_executor_job(
                    parse_connect_file, connect_content
                )
                self._parsed_config = parsed
                await self.async_set_unique_id(
                    parsed.get("network_key", "haefele_mesh")[:16]
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_confirm()

            except ValueError as e:
                _LOGGER.error("Failed to parse .connect file: %s", e)
                errors["connect_file"] = "invalid_config"
            except Exception as e:  # noqa: BLE001
                _LOGGER.exception("Unexpected error parsing .connect file: %s", e)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("connect_file"): str,
            }),
            description_placeholders={
                "instructions": (
                    "Paste the contents of your Häfele Connect Mesh export file "
                    "(.connect). You can export it from the Häfele Connect app: "
                    "Settings → Export Configuration."
                )
            },
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Confirm discovered devices."""
        assert self._parsed_config is not None

        if user_input is not None:
            entry_data = {
                **self._parsed_config,
                "src_address_base": SRC_ADDRESS_BASE,
            }
            return self.async_create_entry(
                title="Häfele Connect Mesh",
                data=entry_data,
            )

        nodes = self._parsed_config.get("nodes", [])
        node_names = ", ".join(n["name"] for n in nodes)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "node_count": str(len(nodes)),
                "node_names": node_names,
                "iv_index": str(self._parsed_config.get("iv_index", 0)),
            },
        )
