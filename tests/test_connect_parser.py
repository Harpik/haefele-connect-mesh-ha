"""Tests for .connect file parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from connect_parser import parse_connect_file


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_minimal_connect_file():
    result = parse_connect_file(_load("minimal.connect.json"))

    assert result["network_key"] == "00112233445566778899aabbccddeeff"
    assert result["app_key"] == "ffeeddccbbaa99887766554433221100"
    assert result["iv_index"] == 1

    assert len(result["nodes"]) == 2
    kitchen, hall = result["nodes"]

    assert kitchen["name"] == "Kitchen TW"
    assert kitchen["mac"] == "AA:BB:CC:DD:EE:01"
    assert kitchen["unicast"] == 0x0101
    assert kitchen["device_type"] == "tunable_white"
    assert 0xC000 in kitchen["groups"]

    assert hall["name"] == "Hall TW"
    assert hall["unicast"] == 0x0102


def test_parse_rejects_invalid_json():
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_connect_file("{not valid json")


def test_parse_rejects_missing_keys():
    content = json.dumps({"nodes": []})
    with pytest.raises(ValueError, match="network key"):
        parse_connect_file(content)


def test_parse_rejects_empty_nodes():
    doc = json.loads(_load("minimal.connect.json"))
    doc["nodes"] = []
    with pytest.raises(ValueError, match="No light nodes"):
        parse_connect_file(json.dumps(doc))


def test_parse_skips_remotes_and_sensors():
    doc = json.loads(_load("minimal.connect.json"))
    # Turn the second node into a remote — should be filtered out.
    doc["nodes"][1]["tos_node"]["type"] = "com.haefele.remote.4button"
    result = parse_connect_file(json.dumps(doc))
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["name"] == "Kitchen TW"
