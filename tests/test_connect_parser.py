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
    # Turn the second node into a remote without light server models — should be filtered out.
    doc["nodes"][1]["tos_node"]["type"] = "com.haefele.remote.4button"
    doc["nodes"][1]["elements"] = []
    result = parse_connect_file(json.dumps(doc))
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["name"] == "Kitchen TW"


def test_reserved_unicasts_include_skipped_remotes():
    """Rotation must avoid remotes' addresses even though they get no entity."""
    doc = json.loads(_load("minimal.connect.json"))
    doc["nodes"][1]["tos_node"]["type"] = "com.haefele.remote.4button"
    doc["nodes"][1]["elements"] = [{"index": 0}, {"index": 1}, {"index": 2}]
    result = parse_connect_file(json.dumps(doc))
    assert len(result["nodes"]) == 1  # the remote is still skipped as a light
    assert {"unicast": 0x0102, "elements": 3} in result["reserved_unicasts"]
    assert {"unicast": 0x0101, "elements": 1} in result["reserved_unicasts"]


def test_provisioner_mesh_address_is_parsed_as_hex():
    """tos_network.provisionerMeshAddress is a hex string like "7FF9"."""
    doc = json.loads(_load("minimal.connect.json"))
    doc.setdefault("tos_network", {})["provisionerMeshAddress"] = "7FF9"
    result = parse_connect_file(json.dumps(doc))
    assert result["provisioner_address"] == 0x7FF9


def test_allocated_unicast_ranges_from_every_provisioner():
    doc = json.loads(_load("minimal.connect.json"))
    doc["provisioners"] = [
        {"allocatedUnicastRange": [{"lowAddress": "0001", "highAddress": "1000"}]},
        {"allocatedUnicastRange": [
            {"lowAddress": "2000", "highAddress": "2FFF"},
            {"lowAddress": "9000", "highAddress": "9FFF"},  # not unicast: dropped
            {"lowAddress": "0500", "highAddress": "0100"},  # inverted: dropped
        ]},
    ]
    result = parse_connect_file(json.dumps(doc))
    assert result["allocated_unicast_ranges"] == [[0x0001, 0x1000], [0x2000, 0x2FFF]]


def test_odd_allocated_range_containers_do_not_break_the_import():
    for odd in ({"lowAddress": "0001", "highAddress": "1000"}, 5, None, "0001-1000"):
        doc = json.loads(_load("minimal.connect.json"))
        doc["provisioners"] = [{"allocatedUnicastRange": odd}]
        result = parse_connect_file(json.dumps(doc))
        assert result["allocated_unicast_ranges"] == []


def test_out_of_range_provisioner_mesh_address_falls_back():
    doc = json.loads(_load("minimal.connect.json"))
    doc.setdefault("tos_network", {})["provisionerMeshAddress"] = "9000"  # group range
    result = parse_connect_file(json.dumps(doc))
    assert result["provisioner_address"] != 0x9000
    assert result["provisioner_address"] <= 0x7FFF
