"""Tests for the light capability router and HSL framing.

We deliberately don't import `light.py` (it pulls the HA light
platform). Instead we cover:

    * `resolve_capability` (pure string mapping) via a tiny shim.
    * `MeshProxyConnection.set_hsl` / `get_hsl` opcode + payload shape.

The HSL test drives `send_access` via monkeypatch so we can assert on
the exact opcode and bytes that would hit the wire.
"""
from __future__ import annotations

import asyncio
import struct
import sys
import types
from pathlib import Path

import pytest

# Ensure the module dir is on sys.path (conftest does this too, but this
# file can be run via `pytest tests/test_models_capability.py` in isolation).
ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "custom_components" / "haefele_mesh"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# resolve_capability  — extracted mini-module to avoid importing light.py
# ---------------------------------------------------------------------------
# We mirror the function under test here so the tests stay hermetic.
# If light.py's implementation drifts, this test will catch it via a
# direct comparison: we re-import resolve_capability inside a guarded
# path where HA isn't needed.

def _load_resolve_capability():
    """Import resolve_capability without pulling ColorMode.

    Trick: exec only the top of light.py up to the resolve_capability
    definition inside a namespace that stubs 'homeassistant.components.light'
    enough to satisfy the `from ... import ...` line.
    """
    # Minimal stubs so `from homeassistant.components.light import …` works.
    def _ensure(name, **attrs):
        if name in sys.modules:
            for k, v in attrs.items():
                setattr(sys.modules[name], k, v)
            return sys.modules[name]
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    _ensure("homeassistant")
    _ensure("homeassistant.components")

    class _ColorMode:
        COLOR_TEMP = "color_temp"
        HS = "hs"
        BRIGHTNESS = "brightness"
        ONOFF = "onoff"

    _ensure(
        "homeassistant.components.light",
        ATTR_BRIGHTNESS="brightness",
        ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
        ATTR_HS_COLOR="hs_color",
        ColorMode=_ColorMode,
        LightEntity=type("LightEntity", (), {}),
    )
    # The rest of the HA imports in light.py also need stubs, but we
    # only need resolve_capability — which is defined before any class
    # uses them. Import the whole thing under a try/except and accept
    # the AttributeError from CoordinatorEntity generics if it happens.
    _ensure(
        "homeassistant.config_entries",
        ConfigEntry=type("ConfigEntry", (), {}),
    )
    _ensure(
        "homeassistant.core",
        HomeAssistant=type("HomeAssistant", (), {}),
        callback=lambda f: f,
    )
    _ensure(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
    )
    _ensure(
        "homeassistant.helpers.entity_platform",
        AddEntitiesCallback=type("AddEntitiesCallback", (), {}),
    )

    class _CoordEntity:
        def __class_getitem__(cls, item):
            return cls

    _ensure(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordEntity,
    )

    # coordinator.py pulls more HA runtime — stub its two exports.
    if "custom_components.haefele_mesh.coordinator" not in sys.modules:
        coord = types.ModuleType("custom_components.haefele_mesh.coordinator")
        coord.HaefeleCoordinator = type("HaefeleCoordinator", (), {})
        coord._node_id = lambda cfg: cfg.get("mac", "")
        sys.modules["custom_components.haefele_mesh.coordinator"] = coord

    import importlib
    mod = importlib.import_module("custom_components.haefele_mesh.light")
    return mod.resolve_capability


resolve_capability = _load_resolve_capability()


@pytest.mark.parametrize("device_type,expected", [
    ("tunable_white", "color_temp"),
    ("rgb", "hs"),
    ("dimmable", "brightness"),
    ("onoff", "onoff"),
    ("on_off", "onoff"),
    ("switch", "onoff"),
    ("relay", "onoff"),
    ("unknown", "onoff"),
    ("", "onoff"),
    (None, "onoff"),
    # Case insensitive
    ("TUNABLE_WHITE", "color_temp"),
    ("RGB", "hs"),
])
def test_resolve_capability(device_type, expected):
    assert resolve_capability(device_type) == expected


# ---------------------------------------------------------------------------
# gatt.MeshProxyConnection.set_hsl / get_hsl framing
# ---------------------------------------------------------------------------

def test_set_hsl_sends_correct_opcode_and_payload():
    """set_hsl must emit opcode 0x8277 with <HHHB little-endian + TID."""
    from custom_components.haefele_mesh.gatt import MeshProxyConnection

    # Build a minimal object graph by hand — don't actually run __init__
    # (it wants a bleak+session+coordinator setup). We just need the
    # instance to be a shell and monkeypatch the two collaborators.
    proxy = MeshProxyConnection.__new__(MeshProxyConnection)

    class _FakeSession:
        src = 0x00C0

        async def _seq_provider(self, src):
            return 0x0000AB  # low byte 0xAB will become TID

    proxy._session = _FakeSession()

    captured: dict = {}

    async def _fake_send_access(dst, opcode, params):
        captured["dst"] = dst
        captured["opcode"] = opcode
        captured["params"] = params

    proxy.send_access = _fake_send_access  # type: ignore[assignment]

    asyncio.run(proxy.set_hsl(dst=0x002F, lightness=0xFFFF, hue=0x4000, saturation=0x8000))

    assert captured["opcode"] == 0x8277
    assert captured["dst"] == 0x002F
    # lightness, hue, saturation, tid — all little-endian, 2+2+2+1 bytes
    expected = struct.pack("<HHHB", 0xFFFF, 0x4000, 0x8000, 0xAB)
    assert captured["params"] == expected
    assert len(captured["params"]) == 7


def test_get_hsl_sends_empty_payload_and_correct_opcode():
    from custom_components.haefele_mesh.gatt import MeshProxyConnection

    proxy = MeshProxyConnection.__new__(MeshProxyConnection)

    captured: dict = {}

    async def _fake_send_access(dst, opcode, params):
        captured["dst"] = dst
        captured["opcode"] = opcode
        captured["params"] = params

    proxy.send_access = _fake_send_access  # type: ignore[assignment]

    asyncio.run(proxy.get_hsl(dst=0x003A))

    assert captured["opcode"] == 0x826D
    assert captured["dst"] == 0x003A
    assert captured["params"] == b""


def test_set_hsl_masks_oversized_values():
    """Values larger than 16 bits must be masked, not overflow struct.pack."""
    from custom_components.haefele_mesh.gatt import MeshProxyConnection

    proxy = MeshProxyConnection.__new__(MeshProxyConnection)

    class _FakeSession:
        src = 0x00C0

        async def _seq_provider(self, src):
            return 0x000001

    proxy._session = _FakeSession()
    captured: dict = {}

    async def _fake_send_access(dst, opcode, params):
        captured["params"] = params

    proxy.send_access = _fake_send_access  # type: ignore[assignment]

    # Pass >16-bit values — the masking in set_hsl should clamp them.
    asyncio.run(proxy.set_hsl(
        dst=0x002F, lightness=0x1FFFF, hue=0x10000, saturation=-1,
    ))

    lightness, hue, sat, tid = struct.unpack("<HHHB", captured["params"])
    assert lightness == 0xFFFF  # 0x1FFFF & 0xFFFF
    assert hue == 0x0000        # 0x10000 & 0xFFFF
    assert sat == 0xFFFF        # -1 & 0xFFFF
    assert tid == 0x01
