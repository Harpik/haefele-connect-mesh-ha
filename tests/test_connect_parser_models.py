"""Tests for connect_parser device_type detection."""
from __future__ import annotations

import json

import pytest  # noqa: F401 (pytest autouse fixtures via conftest)

from connect_parser import parse_connect_file


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
                "type": n["type"],
                "proxyBleAddress": f"AA:BB:CC:DD:EE:{i:02X}",
                "firmwareVersion": "1.2.3",
            },
            "tos_devices": [{"name": n.get("name", f"node {i}")}],
            "elements": [],
        })
    payload = {
        "netKeys": [{"key": _NETKEY}],
        "appKeys": [{"key": _APPKEY}],
        "ivIndex": 1,
        "provisioners": [],
        "nodes": node_entries,
    }
    return json.dumps(payload)


def test_tunable_white_detected():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.meshbox.tw1c", "name": "kitchen"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "tunable_white"


def test_rgb_detected_from_rgb_type():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.led.rgb_strip", "name": "rgb strip"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "rgb"


def test_rgb_detected_from_hsl_or_color_keywords():
    parsed = parse_connect_file(_mk_connect([
        {"type": "com.haefele.driver.hsl", "name": "driver hsl"},
        {"type": "com.haefele.led.colored", "name": "colored"},
    ]))
    assert parsed["nodes"][0]["device_type"] == "rgb"
    assert parsed["nodes"][1]["device_type"] == "rgb"


def test_dimmable_detected():
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


def test_onoff_detected():
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
