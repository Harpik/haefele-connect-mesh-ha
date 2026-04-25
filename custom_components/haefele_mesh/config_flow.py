"""
Config flow for Häfele Connect Mesh integration.

Guides the user through:
1. Uploading the .connect file from the Häfele app
2. Selecting the Bluetooth adapter
3. Confirming discovered devices
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .connect_parser import parse_connect_file

_LOGGER = logging.getLogger(__name__)


class HaefeleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Häfele Connect Mesh configuration flow."""

    VERSION = 1

    def __init__(self):
        self._parsed_config = None
        self._adapter = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Upload .connect file."""
        errors = {}

        if user_input is not None:
            connect_content = user_input.get("connect_file", "")
            try:
                parsed = await self.hass.async_add_executor_job(
                    parse_connect_file, connect_content
                )
                self._parsed_config = parsed
                return await self.async_step_adapter()

            except ValueError as e:
                _LOGGER.error("Failed to parse .connect file: %s", e)
                errors["connect_file"] = "invalid_config"
            except Exception as e:
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

    async def async_step_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Select Bluetooth adapter."""
        errors = {}

        # Get available BT adapters
        adapters = {}
        try:
            bt_adapters = await bluetooth.async_get_bluetooth_adapters(self.hass)
            for adapter_id, adapter_info in bt_adapters.items():
                label = f"{adapter_id}"
                if hasattr(adapter_info, 'name') and adapter_info.name:
                    label += f" ({adapter_info.name})"
                if hasattr(adapter_info, 'address') and adapter_info.address:
                    label += f" - {adapter_info.address}"
                adapters[adapter_id] = label
        except Exception:
            pass

        # Fallback if no adapters detected via HA API
        if not adapters:
            adapters = {
                "hci0": "hci0 (default)",
                "hci1": "hci1 (USB dongle)",
            }

        if user_input is not None:
            self._adapter = user_input.get("adapter", "hci0")
            return await self.async_step_confirm()

        return self.async_show_form(
            step_id="adapter",
            data_schema=vol.Schema({
                vol.Required("adapter", default="hci0"): vol.In(adapters),
            }),
            description_placeholders={
                "node_count": str(len(self._parsed_config.get("nodes", []))),
            },
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Confirm discovered devices."""
        if user_input is not None:
            # Build final config entry data
            entry_data = {
                **self._parsed_config,
                "adapter": self._adapter,
                "src_address_base": 0x0060,
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
                "adapter": self._adapter,
                "iv_index": str(self._parsed_config.get("iv_index", 0)),
            },
        )
