"""The Home Assistant facing side of remotes, against the real HA package.

Covers what the stubbed suite in tests/ cannot: the event entity, the
device-trigger schema and plumbing, the bus forwarder and device removal,
wired together through the shared payload keys.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

ha_const = pytest.importorskip("homeassistant.const")
if not hasattr(ha_const, "MAJOR_VERSION"):
    pytest.skip("real Home Assistant not installed", allow_module_level=True)


import custom_components.haefele_mesh as integration  # noqa: E402
from custom_components.haefele_mesh import device_trigger, event  # noqa: E402
from custom_components.haefele_mesh.remote import (  # noqa: E402
    EVENT_TYPES,
    REMOTE_BUS_EVENT,
    RemoteEvent,
    remote_device_identifier,
)

REMOTE = {
    "name": "Mando a distancia",
    "uuid": "94876076-1E92-440F-B6AD-E681A260D1F7",
    "unicast": 0x0017,
    "elements": 24,
    "type": "com.haefele.remote.v2",
    "firmware": "6.0.5",
    "modules": [
        {"index": 0, "enabled": True, "name": "Luz desayunos", "group_address": 0xC00F,
         "scenes": [0, 0, 0, 6]},
        {"index": 1, "enabled": True, "name": "Luz fuegos", "group_address": 0xC00B,
         "scenes": [0, 0, 0, 6]},
        {"index": 2, "enabled": True, "name": "Todas cocina", "group_address": 0xC017,
         "scenes": [3, 4, 5, 6]},
        {"index": 3, "enabled": False, "name": None, "scenes": []},
        {"index": 4, "enabled": False, "name": None, "scenes": []},
        {"index": 5, "enabled": False, "name": None, "scenes": []},
    ],
}


class FakeCoordinator:
    def __init__(self):
        self.remotes_cfg = [REMOTE]
        self.listeners = []

    def register_remote_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


# ---------------------------------------------------------------------------
# Event entity
# ---------------------------------------------------------------------------


def test_entity_identity_and_defaults():
    coord = FakeCoordinator()
    first = event.HaefeleRemoteModuleEvent(coord, REMOTE, 0)
    fourth = event.HaefeleRemoteModuleEvent(coord, REMOTE, 3)
    assert first.unique_id == "remote_94876076-1e92-440f-b6ad-e681a260d1f7_module_1"
    assert first.event_types == EVENT_TYPES
    assert first.entity_registry_enabled_default is True
    # A position disabled in the app just never fires: no need to disable
    # it (re-enabling later would make HA reload the entry once more).
    assert fourth.entity_registry_enabled_default is True
    assert first.device_info["identifiers"] == {remote_device_identifier(REMOTE)}
    assert first.translation_key == "remote_module_group"
    assert first.translation_placeholders == {"position": "1", "group": "Luz desayunos"}
    assert fourth.translation_key == "remote_module"


def test_entity_records_only_its_own_presses():
    coord = FakeCoordinator()
    ent = event.HaefeleRemoteModuleEvent(coord, REMOTE, 2)
    ent.async_write_ha_state = lambda: None
    asyncio.run(ent.async_added_to_hass())
    for cb in coord.listeners:
        cb(RemoteEvent(0x0017, 0, "turn_on"))  # another position
    assert ent.state_attributes.get("event_type") is None
    for cb in coord.listeners:
        cb(RemoteEvent(0x0017, 2, "scene_a", {"scene": 3}, 0xC017))
    assert ent.state_attributes["event_type"] == "scene_a"
    assert ent.state_attributes["scene"] == 3
    asyncio.run(ent.async_will_remove_from_hass())
    assert coord.listeners == []


def test_only_remotes_gone_from_the_export_can_be_deleted():
    coord = FakeCoordinator()
    hass = SimpleNamespace(data={"haefele_mesh": {"e1": coord}})
    entry = SimpleNamespace(entry_id="e1")

    def device(*identifiers):
        return SimpleNamespace(identifiers=set(identifiers))

    def removable(dev):
        return asyncio.run(integration.async_remove_config_entry_device(hass, entry, dev))

    assert removable(device(("haefele_mesh", "remote_old-uuid"))) is True
    assert removable(device(remote_device_identifier(REMOTE))) is False
    assert removable(device(("haefele_mesh", "aabbccddeeff"))) is False  # a light
    assert removable(device(("other", "remote_x"))) is False


# ---------------------------------------------------------------------------
# Device triggers and the bus forwarder, end to end through the payload
# ---------------------------------------------------------------------------


def test_triggers_offered_validate_against_the_schema(monkeypatch):
    monkeypatch.setattr(device_trigger, "_remote_for_device", lambda hass, dev: REMOTE)
    triggers = asyncio.run(device_trigger.async_get_triggers(None, "dev1"))
    for trig in triggers:
        device_trigger.TRIGGER_SCHEMA(trig)
    by_pos = {}
    for trig in triggers:
        by_pos.setdefault(trig["subtype"], set()).add(trig["type"])
    assert set(by_pos) == {"position_1", "position_2", "position_3"}  # 4-6 disabled
    assert {"scene_a", "scene_b", "scene_c"} <= by_pos["position_3"]
    assert not {"scene_a", "scene_b", "scene_c"} & by_pos["position_1"]


def test_trigger_matches_what_the_forwarder_fires(monkeypatch):
    fired = []
    hass = SimpleNamespace(bus=SimpleNamespace(
        async_fire=lambda etype, data: fired.append((etype, data)),
    ))
    registry = SimpleNamespace(
        async_get_device=lambda identifiers: SimpleNamespace(id="dev1")
        if identifiers == {remote_device_identifier(REMOTE)} else None,
    )
    monkeypatch.setattr(integration.dr, "async_get", lambda hass: registry)
    forward = integration._remote_bus_forwarder(hass, FakeCoordinator())
    forward(RemoteEvent(0x0017, 2, "scene_b", {"scene": 4}, 0xC017))
    ((etype, data),) = fired
    assert etype == REMOTE_BUS_EVENT

    captured = {}

    async def fake_attach(hass, config, action, info, platform_type):
        captured.update(config)
        return lambda: None

    monkeypatch.setattr(device_trigger.event_trigger, "async_attach_trigger", fake_attach)
    # The real schema renders templates and needs a running HA instance.
    monkeypatch.setattr(device_trigger.event_trigger, "TRIGGER_SCHEMA", lambda conf: conf)
    config = device_trigger.TRIGGER_SCHEMA({
        "platform": "device", "domain": "haefele_mesh", "device_id": "dev1",
        "type": "scene_b", "subtype": "position_3",
    })
    asyncio.run(device_trigger.async_attach_trigger(None, config, None, {}))
    assert captured["event_type"] == REMOTE_BUS_EVENT
    # Every key the trigger filters on is present with the same value.
    for key, value in captured["event_data"].items():
        assert data[key] == value, key
