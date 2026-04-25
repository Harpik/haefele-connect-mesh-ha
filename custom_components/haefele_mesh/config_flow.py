"""
Config flow for Häfele Connect Mesh integration.

Guides the user through:
1. Uploading the .connect file exported from the Häfele app
   (with a paste-text fallback for devices that can't upload files)
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
from homeassistant.components import file_upload
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .connect_parser import parse_connect_file
from .const import DOMAIN, SRC_ADDRESS_BASE

_LOGGER = logging.getLogger(__name__)

_FIELD_FILE = "connect_file_upload"
_FIELD_TEXT = "connect_file_text"


class HaefeleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Häfele Connect Mesh configuration flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._parsed_config: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Step 1 — upload
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: upload the .connect file (or paste its contents)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            content = await self._read_input(user_input, errors)
            if content is not None:
                try:
                    parsed = await self.hass.async_add_executor_job(
                        parse_connect_file, content
                    )
                    self._parsed_config = parsed
                    await self.async_set_unique_id(
                        parsed.get("network_key", "haefele_mesh")[:16]
                    )
                    self._abort_if_unique_id_configured()
                    return await self.async_step_confirm()

                except ValueError as e:
                    _LOGGER.error("Failed to parse .connect file: %s", e)
                    errors["base"] = "invalid_config"
                except Exception as e:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error parsing .connect: %s", e)
                    errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Optional(_FIELD_FILE): selector.FileSelector(
                    selector.FileSelectorConfig(accept="*/*")
                ),
                vol.Optional(_FIELD_TEXT): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def _read_input(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> str | None:
        """Resolve the .connect payload from whichever field the user filled."""
        file_id = user_input.get(_FIELD_FILE)
        text = user_input.get(_FIELD_TEXT)

        if file_id:
            try:
                def _read(path):
                    return path.read_text(encoding="utf-8")

                with file_upload.process_uploaded_file(self.hass, file_id) as path:
                    return await self.hass.async_add_executor_job(_read, path)
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Failed to read uploaded file: %s", e)
                errors["base"] = "file_read_error"
                return None

        if text and text.strip():
            return text

        errors["base"] = "no_input"
        return None

    # ------------------------------------------------------------------
    # Step 2 — confirm
    # ------------------------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: confirm discovered devices."""
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
