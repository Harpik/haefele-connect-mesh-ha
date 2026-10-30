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
    RemoteDecoder,
    module_count,
)

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
        {"id": GROUP_DESAYUNOS, "name": "Luz desayunos"},
        {"id": GROUP_FUEGOS, "name": "Luz fuegos"},
        {"id": GROUP_TODAS, "name": "Todas cocina"},
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


def test_module_count_falls_back_to_element_count():
    assert module_count({"elements": 24, "modules": []}) == 6
    assert module_count({"elements": 24, "modules": [{}, {}]}) == 2


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
