"""Tests for SRC choice on new entries and the config-flow upload path."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import types
from pathlib import Path

from custom_components.haefele_mesh.addressing import pick_initial_src
from custom_components.haefele_mesh.const import ROTATION_SEARCH_TOP, SRC_ADDRESS_BASE

# ---------------------------------------------------------------------------
# pick_initial_src
# ---------------------------------------------------------------------------


def test_initial_src_unchanged_without_ranges():
    assert pick_initial_src({"nodes": [], "provisioner_address": 0x7FF9}) == SRC_ADDRESS_BASE


def test_initial_src_kept_when_base_is_outside_every_range():
    parsed = {"allocated_unicast_ranges": [[0x2000, 0x2FFF]]}
    assert pick_initial_src(parsed) == SRC_ADDRESS_BASE


def test_initial_src_moves_out_of_the_app_range():
    parsed = {
        "allocated_unicast_ranges": [[0x0001, 0x1000]],
        "provisioner_address": 0x7FF9,
        "reserved_unicasts": [{"unicast": 0x0017, "elements": 24}],
    }
    assert pick_initial_src(parsed) == ROTATION_SEARCH_TOP


def test_initial_src_skips_known_addresses_outside_ranges():
    parsed = {
        "allocated_unicast_ranges": [[0x0001, 0x1000]],
        "reserved_unicasts": [{"unicast": ROTATION_SEARCH_TOP - 1, "elements": 2}],
    }
    # 0x7EFE..0x7EFF is a node -> next free one below
    assert pick_initial_src(parsed) == ROTATION_SEARCH_TOP - 2


def test_initial_src_ignores_malformed_ranges():
    parsed = {"allocated_unicast_ranges": [["0001", "1000"], [5], None, "x"]}
    assert pick_initial_src(parsed) == SRC_ADDRESS_BASE


def test_initial_src_never_lands_on_a_legacy_src():
    from custom_components.haefele_mesh.const import LEGACY_SRC_ADDRESSES
    top = max(LEGACY_SRC_ADDRESSES)
    # everything above the highest legacy SRC is inside a range
    parsed = {"allocated_unicast_ranges": [[top + 1, 0x7FFF], [1, top - 1]]}
    assert pick_initial_src(parsed) != top
    assert pick_initial_src(parsed) not in LEGACY_SRC_ADDRESSES


def test_initial_src_moves_when_base_is_a_node_even_outside_ranges():
    parsed = {
        "allocated_unicast_ranges": [[0x2000, 0x2FFF]],
        "reserved_unicasts": [{"unicast": SRC_ADDRESS_BASE, "elements": 1}],
    }
    assert pick_initial_src(parsed) == ROTATION_SEARCH_TOP


def test_initial_src_skips_the_provisioner_address():
    parsed = {
        "allocated_unicast_ranges": [[0x0001, 0x1000]],
        "provisioner_address": ROTATION_SEARCH_TOP,
    }
    assert pick_initial_src(parsed) == ROTATION_SEARCH_TOP - 1


def test_out_of_bounds_ranges_are_dropped():
    from custom_components.haefele_mesh.addressing import valid_ranges
    assert valid_ranges([[0, 5], [5, 0x8000], [9, 3], [1, 2]]) == [(1, 2)]


# ---------------------------------------------------------------------------
# config_flow: the upload must be processed entirely in the executor
# ---------------------------------------------------------------------------


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):  # swallow domain=...
        super().__init_subclass__()


_stub("voluptuous", Schema=lambda *a, **k: None, Optional=lambda *a, **k: a[0])
_stub("homeassistant.config_entries", ConfigFlow=_ConfigFlow, ConfigEntry=object)
sys.modules["homeassistant.config_entries"].ConfigFlow = getattr(
    sys.modules["homeassistant.config_entries"], "ConfigFlow", _ConfigFlow,
)
_stub("homeassistant.data_entry_flow", FlowResult=dict)
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.selector")
_upload = _stub("homeassistant.components.file_upload")
sys.modules["homeassistant"].config_entries = sys.modules["homeassistant.config_entries"]
sys.modules["homeassistant.components"].file_upload = _upload
sys.modules["homeassistant.helpers"].selector = sys.modules["homeassistant.helpers.selector"]

from custom_components.haefele_mesh import config_flow as cf


class _Hass:
    def __init__(self):
        self.in_executor = False

    async def async_add_executor_job(self, func, *args):
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False


def test_uploaded_file_is_processed_in_the_executor(tmp_path, monkeypatch):
    hass = _Hass()
    upload = tmp_path / "upload.connect"
    upload.write_text("{}", encoding="utf-8")
    seen = {}

    @contextlib.contextmanager
    def process_uploaded_file(h, file_id):
        seen["enter"] = hass.in_executor
        yield Path(upload)
        seen["exit"] = hass.in_executor  # rmtree happens here in real HA

    monkeypatch.setattr(cf.file_upload, "process_uploaded_file", process_uploaded_file, raising=False)
    flow = cf.HaefeleConfigFlow.__new__(cf.HaefeleConfigFlow)
    flow.hass = hass
    errors: dict[str, str] = {}
    content = asyncio.run(flow._read_input({cf._FIELD_FILE: "abc"}, errors))
    assert content == "{}"
    assert errors == {}
    assert seen == {"enter": True, "exit": True}


def test_new_entry_is_created_with_the_picked_src():
    flow = cf.HaefeleConfigFlow.__new__(cf.HaefeleConfigFlow)
    flow._parsed_config = {
        "nodes": [],
        "allocated_unicast_ranges": [[0x0001, 0x1000]],
        "provisioner_address": 0x7FF9,
    }
    flow.async_create_entry = lambda **kw: kw
    result = asyncio.run(flow.async_step_confirm({}))
    assert result["data"]["src_address_base"] == ROTATION_SEARCH_TOP
