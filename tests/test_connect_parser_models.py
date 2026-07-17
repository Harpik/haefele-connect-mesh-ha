"""Tests for connect_parser device_type detection."""
from __future__ import annotations

import json

import pytest

from connect_parser import (
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_CTL_TEMPERATURE_SERVER,
    MODEL_LIGHT_HSL_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
    detect_device_type_from_models,
    parse_connect_file,
)


_NETKEY = "0" * 32
_APPKEY = "1" * 32


def _mk_connect(nodes: list[dict]) -> str:
    """Build a minimal .connect payload with the given per-node overrides."""
    node_entries = []
    for i, n in enumerate(nodes, start=1):
        node_entries.append({
            "UUID": f"uuid-{i}",
            "unicastAddress": f"{0x0010 + i:04X}",
            "deviceKey": "a" * 32,
            "tos_node": {
                "type": n.get("type", "com.haefele.meshbox.esp.mw.1c"),
                "proxyBleAddress": f"AA:BB:CC:DD:EE:{i:02X}",
                "firmwareVersion": "1.2.3",
            },
            "tos_devices": [{"name": n.get("name", f"node {i}")}],
            "elements": [{"models": n.get("models", [])}],
        })
    payload = {
        "netKeys": [{"key": _NETKEY}],
        "appKeys": [{"key": _APPKEY}],
        "ivIndex": 1,
        "provisioners": [],
        "nodes": node_entries,
    }
    return json.dumps(payload)


@pytest.mark.parametrize("models,expected", [
    ([{"modelId": "1307"}], "rgb"),
    ([{"modelId": "0x1303"}], "tunable_white"),
    ([{"modelId": MODEL_LIGHT_CTL_TEMPERATURE_SERVER}], "tunable_white"),
    ([{"modelId": MODEL_LIGHT_LIGHTNESS_SERVER}], "dimmable"),
    ([{"modelId": "1000"}], "onoff"),
    ([{"modelId": "9999"}], "unknown"),
    ([{"modelId": "not-a-model"}], "unknown"),
])
def test_detect_device_type_from_models(models, expected):
    assert detect_device_type_from_models(models) == expected


def test_mixed_models_use_requested_precedence():
    assert detect_device_type_from_models([
        {"modelId": MODEL_GENERIC_ONOFF_SERVER},
        {"modelId": MODEL_LIGHT_LIGHTNESS_SERVER},
        {"modelId": MODEL_LIGHT_CTL_SERVER},
        {"modelId": MODEL_LIGHT_HSL_SERVER},
    ]) == "rgb"
    assert detect_device_type_from_models([
        {"modelId": MODEL_GENERIC_ONOFF_SERVER},
        {"modelId": MODEL_LIGHT_LIGHTNESS_SERVER},
        {"modelId": MODEL_LIGHT_CTL_SERVER},
    ]) == "tunable_white"
    assert detect_device_type_from_models([
        {"modelId": MODEL_GENERIC_ONOFF_SERVER},
        {"modelId": MODEL_LIGHT_LIGHTNESS_SERVER},
    ]) == "dimmable"


def test_tunable_white_detected_from_ctl_models_despite_generic_type():
    parsed = parse_connect_file(_mk_connect([
        {
            "type": "com.haefele.meshbox.esp.mw.1c",
            "name": "2-way distributor TW",
            "models": [
                {"modelId": "1300"},  # Light Lightness Server
                {"modelId": "1301"},  # Light Lightness Setup Server
                {"modelId": "1303"},  # Light CTL Server
                {"modelId": "1304"},  # Light CTL Setup Server
            ],
        },
    ]))
    assert parsed["nodes"][0]["device_type"] == "tunable_white"


def test_rgb_detected_from_hsl_model():
    parsed = parse_connect_file(_mk_connect([
        {"models": [{"modelId": "1307"}], "name": "rgb strip"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "rgb"


def test_dimmable_detected_from_lightness_model():
    parsed = parse_connect_file(_mk_connect([
        {"models": [{"modelId": MODEL_LIGHT_LIGHTNESS_SERVER}], "name": "dimmer"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "dimmable"


def test_onoff_detected_from_generic_onoff_model():
    parsed = parse_connect_file(_mk_connect([
        {"models": [{"modelId": "1000"}], "name": "relay"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "onoff"


def test_unknown_models_fall_back_to_type_matching():
    parsed = parse_connect_file(_mk_connect([
        {
            "type": "com.haefele.meshbox.tw1c",
            "name": "kitchen",
            "models": [{"modelId": "9999"}],
        },
    ]))
    assert parsed["nodes"][0]["device_type"] == "tunable_white"


def test_unknown_type_and_unknown_models_remains_unknown():
    parsed = parse_connect_file(_mk_connect([
        {
            "type": "com.example.unknown",
            "name": "mystery node",
            "models": [{"modelId": "9999"}],
        },
    ]))
    assert parsed["nodes"][0]["device_type"] == "unknown"


def test_rgb_detected_from_rgb_type_fallback():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.led.rgb_strip", "name": "rgb strip"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "rgb"


def test_rgb_detected_from_hsl_or_color_keywords_fallback():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.driver.hsl", "name": "driver hsl"},
        {"type": "com.haefele.led.colored", "name": "colored"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "rgb"
    assert parsed["nodes"][1]["device_type"] == "rgb"


def test_dimmable_detected_from_type_fallback():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.driver.dimmer_60w", "name": "dimmer"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "dimmable"


def test_generic_light_falls_back_to_dimmable():
    parsed = parse_connect_file(_mk_connect([
        # Matches _LIGHT_TYPE_PREFIXES but no tw/rgb/dim keyword
        {"type": "com.haefele.led.basic", "name": "basic led"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "dimmable"


def test_onoff_detected_from_type_fallback():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.driver.relay", "name": "relay"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "onoff"


def test_remote_skipped():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.remote.wall_switch", "name": "wall"},
        {"type": "com.haefele.meshbox.tw1c", "name": "actual light"},
    ]))
    assert len(parsed["nodes"]) == 1
    assert parsed["nodes"][0]["name"] == "actual light"


def test_non_light_types_with_onoff_model_are_skipped():
    parsed = parse_connect_file(_mk_connect([
        {
            "type": "com.haefele.remote.wall_switch",
            "name": "wall remote",
            "models": [{"modelId": "1000"}],
        },
        {
            "type": "com.haefele.sensor.motion",
            "name": "motion sensor",
            "models": [{"modelId": "1000"}],
        },
        {
            "type": "com.haefele.switch.wall",
            "name": "wall switch",
            "models": [{"modelId": "1000"}],
        },
        {
            "type": "com.haefele.meshbox.esp.mw.1c",
            "name": "actual light",
            "models": [{"modelId": "1303"}],
        },
    ]))
    assert len(parsed["nodes"]) == 1
    assert parsed["nodes"][0]["name"] == "actual light"
    assert parsed["nodes"][0]["device_type"] == "tunable_white"
