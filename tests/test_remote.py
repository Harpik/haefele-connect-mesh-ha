"""Häfele remote: parsing from the .connect export and decoding of presses.

The frames below are the ones a real remote v2 sent during a live test
(unicast 0x0017, 24 elements, 6 positions of 4 elements).
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.haefele_mesh.connect_parser import parse_connect_file
from custom_components.haefele_mesh.remote import (
    DEDUP_WINDOW,
    EVENT_TYPES,
    RemoteDecoder,
    module_count,
)
from custom_components.haefele_mesh.remote_config import MODES

FIXTURES = Path(__file__).parent / "fixtures"

GROUP_DESAYUNOS = "090ED94E-27F7-4DBD-8DA8-74C45BDDD77C"
GROUP_FUEGOS = "550CAF16-85A1-4C94-83B3-5B4B5D36A40E"
GROUP_TODAS = "D66D1900-35C5-4CA4-AA26-D3E09E3E0EB5"


def _remote_node() -> dict:
    def module(pos, group=None, scenes=None):
        settings = {"type": "group", "groupId": group} if group else {"type": "disabled"}
        return {"id": pos, "controlSettings": settings, "sceneIdList": scenes}

    return {
        "UUID": "94876076-1E92-440F-B6AD-E681A260D1F7",
        "unicastAddress": "0017",
        "elements": [{"index": i, "models": []} for i in range(24)],
        "tos_node": {
            "elementCount": 24,
            "type": "com.haefele.remote.v2",
            "firmwareVersion": "6.0.5",
            "proxyBleAddress": "E0:A2:81:E6:AD:F6",
        },
        "tos_devices": [{
            "name": "Mando a distancia",
            "modules": [
                module(0, GROUP_DESAYUNOS, [0, 0, 0, 6]),
                module(1, GROUP_FUEGOS, [0, 0, 0, 6]),
                module(2, GROUP_TODAS, [3, 4, 5, 6]),
                module(3), module(4), module(5),
            ],
        }],
    }


def _parsed_with_remote() -> dict:
    doc = json.loads((FIXTURES / "minimal.connect.json").read_text())
    doc["nodes"].append(_remote_node())
    doc["tos_groups"] = [
        {"id": GROUP_DESAYUNOS, "name": "Luz desayunos", "multicastAddress": 0xC00F},
        {"id": GROUP_FUEGOS, "name": "Luz fuegos", "multicastAddress": 0xC00B},
        {"id": GROUP_TODAS, "name": "Todas cocina", "multicastAddress": 0xC017},
    ]
    return parse_connect_file(json.dumps(doc))


def _decoder() -> RemoteDecoder:
    return RemoteDecoder(_parsed_with_remote()["remotes"])


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_describes_the_remote_and_its_positions():
    parsed = _parsed_with_remote()
    (remote,) = parsed["remotes"]
    assert remote["name"] == "Mando a distancia"
    assert remote["unicast"] == 0x0017 and remote["elements"] == 24
    assert remote["type"] == "com.haefele.remote.v2"
    assert [m["name"] for m in remote["modules"]] == [
        "Luz desayunos", "Luz fuegos", "Todas cocina", None, None, None,
    ]
    assert [m["enabled"] for m in remote["modules"]] == [True, True, True, False, False, False]
    assert remote["modules"][2]["scenes"] == [3, 4, 5, 6]
    # The remote is still not a light.
    assert all(n["unicast"] != 0x0017 for n in parsed["nodes"])


def test_export_without_remotes_has_an_empty_list():
    parsed = parse_connect_file((FIXTURES / "minimal.connect.json").read_text())
    assert parsed["remotes"] == []


def test_module_count_covers_every_element():
    assert module_count({"elements": 24, "modules": []}) == 6
    # An export listing fewer positions than its elements still gets all six.
    assert module_count({"elements": 24, "modules": [{}, {}]}) == 6
    assert module_count({"elements": 4, "modules": [{}, {}]}) == 2


# ---------------------------------------------------------------------------
# Decoding (frames captured from the real remote)
# ---------------------------------------------------------------------------


def test_centre_button_on_and_off():
    d = _decoder()
    on = d.decode(0x0017, 0x8203, bytes.fromhex("01d8"), now=0)
    assert (on.module, on.event_type) == (0, "turn_on")
    off = d.decode(0x0017, 0x8203, bytes.fromhex("00da"), now=1)
    assert off.event_type == "turn_off"


def test_ring_up_down_is_brightness_on_offset_0():
    d = _decoder()
    assert d.decode(0x0017, 0x8207, bytes.fromhex("ff7f11"), now=0).event_type == "brightness_max"
    assert d.decode(0x0017, 0x8207, bytes.fromhex("018012"), now=0).event_type == "brightness_min"


def test_ring_left_right_is_colour_temperature_on_offset_2():
    d = _decoder()
    assert d.decode(0x0019, 0x8207, bytes.fromhex("00801a"), now=0).event_type == "color_temp_warm"
    assert d.decode(0x0019, 0x8207, bytes.fromhex("ff7f1b"), now=0).event_type == "color_temp_cold"


def test_offsets_1_and_3_are_saturation_and_hue():
    d = _decoder()
    assert d.decode(0x0018, 0x8207, bytes.fromhex("008002"), now=0).event_type == "saturation_min"
    assert d.decode(0x001A, 0x8207, bytes.fromhex("ff7f03"), now=0).event_type == "hue_max"


def test_positions_are_told_apart_by_element():
    d = _decoder()
    assert d.decode(0x001B, 0x8203, bytes.fromhex("011e"), now=0).module == 1
    assert d.decode(0x001F, 0x8203, bytes.fromhex("015c"), now=0).module == 2


def test_scene_buttons_a_b_c_with_scene_number():
    d = _decoder()
    a = d.decode(0x001F, 0x8243, bytes.fromhex("030007"), now=0)
    b = d.decode(0x0020, 0x8243, bytes.fromhex("040004"), now=0)
    c = d.decode(0x0021, 0x8243, bytes.fromhex("050003"), now=0)
    assert [(e.module, e.event_type, e.attributes["scene"]) for e in (a, b, c)] == [
        (2, "scene_a", 3), (2, "scene_b", 4), (2, "scene_c", 5),
    ]


def test_retransmission_with_same_tid_is_dropped():
    d = _decoder()
    assert d.decode(0x0017, 0x8203, bytes.fromhex("01d3"), now=10.0) is not None
    assert d.decode(0x0017, 0x8203, bytes.fromhex("01d3"), now=10.2) is None
    # Next press has a new TID.
    assert d.decode(0x0017, 0x8203, bytes.fromhex("00d4"), now=14.0) is not None


def test_same_tid_after_the_window_is_a_new_press():
    d = _decoder()
    assert d.decode(0x0017, 0x8203, bytes.fromhex("01d3"), now=0) is not None
    assert d.decode(0x0017, 0x8203, bytes.fromhex("01d3"), now=DEDUP_WINDOW + 0.1) is not None


def test_frames_from_lights_or_unknown_sources_are_ignored():
    d = _decoder()
    assert not d.owns(0x002F)
    assert d.decode(0x002F, 0x8204, bytes.fromhex("01"), now=0) is None
    assert d.decode(0x0017 + 24, 0x8203, bytes.fromhex("01aa"), now=0) is None


def test_status_and_truncated_messages_are_not_presses():
    d = _decoder()
    assert d.decode(0x0017, 0x8204, bytes.fromhex("01"), now=0) is None  # OnOff Status
    assert d.decode(0x0017, 0x8203, bytes.fromhex("01"), now=0) is None  # no TID
    assert d.decode(0x0017, 0x8207, bytes.fromhex("ff7f"), now=0) is None


def test_press_on_a_position_the_export_omits_is_still_decoded():
    remote = {"unicast": 0x0100, "elements": 24, "modules": [{"enabled": True}]}
    d = RemoteDecoder([remote])
    event = d.decode(0x0114, 0x8203, bytes.fromhex("0101"), now=0)
    assert (event.module, event.event_type) == (5, "turn_on")


def test_event_carries_the_group_of_the_pressed_position():
    d = _decoder()
    assert d.decode(0x001B, 0x8203, bytes.fromhex("0120"), now=0).group_address == 0xC00B


def test_odd_remote_fields_never_reject_the_import():
    doc = json.loads((FIXTURES / "minimal.connect.json").read_text())
    node = _remote_node()
    node["tos_node"]["elementCount"] = "abc"
    node["tos_node"]["firmwareVersion"] = 605
    node["tos_node"]["proxyBleAddress"] = ["x"]
    node["tos_devices"][0]["modules"][0] = "not a dict"
    broken = _remote_node()
    broken["unicastAddress"] = "zz"
    doc["nodes"] += [node, broken]
    doc["tos_groups"] = [{"id": GROUP_FUEGOS, "name": "Luz fuegos", "multicastAddress": "nope"}]
    parsed = parse_connect_file(json.dumps(doc))
    assert len(parsed["nodes"]) == 2  # the lights are still imported
    (remote,) = parsed["remotes"]
    assert remote["elements"] == 24
    assert remote["firmware"] == "" and remote["mac"] == ""
    assert remote["modules"][0]["name"] == "Luz fuegos"
    assert remote["modules"][0]["group_address"] is None


def test_translations_cover_every_event_type():
    base = Path(__file__).parent.parent / "custom_components" / "haefele_mesh"
    for name in ("strings.json", "translations/en.json", "translations/es.json"):
        data = json.loads((base / name).read_text(encoding="utf-8"))
        triggers = data["device_automation"]["trigger_type"]
        for key in ("remote_module", "remote_module_group"):
            states = data["entity"]["event"][key]["state_attributes"]["event_type"]["state"]
            assert set(states) == set(EVENT_TYPES), (name, key)
        assert set(triggers) == set(EVENT_TYPES), name
        assert "position_24" in data["device_automation"]["trigger_subtype"], name
        modes = data["entity"]["select"]["remote_position_mode"]["state"]
        assert set(modes) == set(MODES), name


def test_client_models_and_app_publication_per_position():
    doc = json.loads((FIXTURES / "minimal.connect.json").read_text())
    node = _remote_node()
    node["elements"][0]["models"] = [
        {"modelId": "0000"},  # Configuration Server: not a client
        {"modelId": "1001", "publish": {
            "address": "C00F", "index": 0, "ttl": 255,
            "period": {"numberOfSteps": 0, "resolution": 100},
            "retransmit": {"count": 1, "interval": 100}}},
    ]
    node["elements"][12]["models"] = [{"modelId": "1001"}]
    node["deviceKey"] = "00112233445566778899AABBCCDDEEFF"
    doc["nodes"].append(node)
    remote = parse_connect_file(json.dumps(doc))["remotes"][0]
    assert remote["device_key"] == "00112233445566778899aabbccddeeff"
    assert remote["modules"][0]["clients"] == [{
        "element": 0x0017, "model": 0x1001,
        "publish": {"address": 0xC00F, "app_key_index": 0, "ttl": 255,
                    "period": 0, "retransmit": 0x09},
    }]
    assert remote["modules"][3]["clients"] == [
        {"element": 0x0017 + 12, "model": 0x1001, "publish": None},
    ]


def test_malformed_device_key_is_dropped():
    doc = json.loads((FIXTURES / "minimal.connect.json").read_text())
    node = _remote_node()
    node["deviceKey"] = "not-a-key"
    doc["nodes"].append(node)
    assert parse_connect_file(json.dumps(doc))["remotes"][0]["device_key"] == ""
