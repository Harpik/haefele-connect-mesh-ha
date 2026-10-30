"""async_setup_entry: failures raise ConfigEntryNotReady and leave nothing running."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# The loaded __init__.py resolves its relative imports through an explicit
# __package__, which differs from its spec parent by design.
pytestmark = pytest.mark.filterwarnings("ignore:__package__ != __spec__.parent:DeprecationWarning")

INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "haefele_mesh" / "__init__.py"
)


class _ConfigEntryNotReady(Exception):
    pass


class _ConfigEntryError(Exception):
    pass


class _FakeCoordinator:
    instances: list[_FakeCoordinator] = []
    fail_setup: Exception | None = None
    fail_refresh: Exception | None = None

    def __init__(self, hass, data, entry_id):
        self.shutdown_calls = 0
        self.remotes_cfg = []
        self.remote_listeners = []
        _FakeCoordinator.instances.append(self)

    def register_remote_listener(self, callback):
        self.remote_listeners.append(callback)
        return lambda: self.remote_listeners.remove(callback)

    async def async_setup(self):
        if _FakeCoordinator.fail_setup is not None:
            raise _FakeCoordinator.fail_setup

    async def async_config_entry_first_refresh(self):
        if _FakeCoordinator.fail_refresh is not None:
            raise _FakeCoordinator.fail_refresh

    async def async_shutdown(self):
        self.shutdown_calls += 1


@pytest.fixture
def integration(monkeypatch):
    """Load the integration's __init__.py against stubbed HA modules.

    The fake coordinator module is only in sys.modules while this test runs,
    so it never shadows the real one for other test files.
    """
    exc = types.ModuleType("homeassistant.exceptions")
    exc.ConfigEntryNotReady = _ConfigEntryNotReady
    exc.ConfigEntryError = _ConfigEntryError
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exc)
    if "homeassistant.config_entries" not in sys.modules:
        ce = types.ModuleType("homeassistant.config_entries")
        ce.ConfigEntry = object
        monkeypatch.setitem(sys.modules, "homeassistant.config_entries", ce)
    coord = types.ModuleType("custom_components.haefele_mesh.coordinator")
    coord.HaefeleCoordinator = _FakeCoordinator
    monkeypatch.setitem(sys.modules, "custom_components.haefele_mesh.coordinator", coord)

    name = "custom_components.haefele_mesh._setup_entry_under_test"
    spec = importlib.util.spec_from_file_location(name, INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "custom_components.haefele_mesh"
    spec.loader.exec_module(module)

    _FakeCoordinator.instances = []
    _FakeCoordinator.fail_setup = None
    _FakeCoordinator.fail_refresh = None
    return module


VALID_DATA = {
    "network_key": "00112233445566778899AABBCCDDEEFF",
    "app_key": "FFEEDDCCBBAA99887766554433221100",
    "nodes": [],
}


def _hass_and_entry(data: dict | None = None):
    forwarded: list[object] = []

    async def _forward(entry, platforms):
        forwarded.append((entry, tuple(platforms)))

    hass = types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(async_forward_entry_setups=_forward),
    )
    unloads: list = []
    entry = types.SimpleNamespace(
        entry_id="abc", data=VALID_DATA if data is None else data,
        async_on_unload=unloads.append, unloads=unloads,
    )
    return hass, entry, forwarded


def test_setup_failure_raises_not_ready_and_shuts_down(integration):
    _FakeCoordinator.fail_setup = OSError("adapter busy")
    hass, entry, forwarded = _hass_and_entry()
    with pytest.raises(_ConfigEntryNotReady, match="adapter busy"):
        asyncio.run(integration.async_setup_entry(hass, entry))
    (coordinator,) = _FakeCoordinator.instances
    assert coordinator.shutdown_calls == 1
    assert hass.data.get(integration.DOMAIN, {}) == {}
    assert forwarded == []


def test_first_refresh_failure_shuts_down_and_reraises(integration):
    _FakeCoordinator.fail_refresh = _ConfigEntryNotReady("no data")
    hass, entry, forwarded = _hass_and_entry()
    with pytest.raises(_ConfigEntryNotReady, match="no data"):
        asyncio.run(integration.async_setup_entry(hass, entry))
    (coordinator,) = _FakeCoordinator.instances
    assert coordinator.shutdown_calls == 1
    assert hass.data.get(integration.DOMAIN, {}) == {}
    assert forwarded == []


def test_successful_setup_registers_and_forwards(integration):
    hass, entry, forwarded = _hass_and_entry()
    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    (coordinator,) = _FakeCoordinator.instances
    assert coordinator.shutdown_calls == 0
    assert hass.data[integration.DOMAIN]["abc"] is coordinator
    assert forwarded == [(entry, ("light", "event"))]
    # Remote presses are forwarded to the bus for device triggers, and the
    # listener is removed when the entry unloads.
    assert len(coordinator.remote_listeners) == 1
    (unsub,) = entry.unloads
    unsub()
    assert coordinator.remote_listeners == []


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"network_key": "not hex at all"}, "network_key"),
        ({"app_key": "0011"}, "app_key"),  # valid hex, wrong length
        ({"app_key": None}, "app_key"),
        ({"nodes": "x"}, "nodes"),
    ],
)
def test_invalid_entry_data_is_a_permanent_error(integration, override, match):
    hass, entry, forwarded = _hass_and_entry({**VALID_DATA, **override})
    with pytest.raises(_ConfigEntryError, match=match):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert _FakeCoordinator.instances == []  # nothing was started
    assert forwarded == []


def test_keys_with_spaces_are_accepted_like_bytes_fromhex_does(integration):
    data = {**VALID_DATA, "network_key": "00112233 44556677 8899AABB CCDDEEFF"}
    hass, entry, _forwarded = _hass_and_entry(data)
    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True


def test_value_error_at_runtime_is_retried_not_permanent(integration):
    # e.g. raised from inside the Bluetooth stack while connecting
    _FakeCoordinator.fail_setup = ValueError("bad advertisement")
    hass, entry, _forwarded = _hass_and_entry()
    with pytest.raises(_ConfigEntryNotReady, match="bad advertisement"):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert _FakeCoordinator.instances[0].shutdown_calls == 1


def test_config_entry_error_from_coordinator_is_not_downgraded(integration):
    _FakeCoordinator.fail_refresh = _ConfigEntryError("auth failed")
    hass, entry, _forwarded = _hass_and_entry()
    with pytest.raises(_ConfigEntryError, match="auth failed"):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert _FakeCoordinator.instances[0].shutdown_calls == 1
