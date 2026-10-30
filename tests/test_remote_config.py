"""Remote position modes: Configuration messages and the job applying them."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.haefele_mesh import remote_configurator as rc_mod
from custom_components.haefele_mesh.remote_config import (
    HA_ONLY_ADDRESS,
    MODE_APP,
    MODE_DISABLED,
    MODE_HA_ONLY,
    OP_MODEL_PUBLICATION_SET,
    available_modes,
    encode_publication_set,
    parse_publication_status,
    publication_for,
)
from custom_components.haefele_mesh.remote_configurator import RemoteConfigurator

APP_PUB = {"address": 0xC00F, "app_key_index": 0, "ttl": 0xFF, "period": 0, "retransmit": 0x09}


def _clients(first_element: int, app: bool) -> list[dict]:
    layout = [(0, 0x1001), (0, 0x1003), (0, 0x1205), (1, 0x1003), (1, 0x1205),
              (2, 0x1003), (2, 0x1205), (3, 0x1003)]
    return [
        {"element": first_element + off, "model": model, "publish": dict(APP_PUB) if app else None}
        for off, model in layout
    ]


def _remote(device_key: str = "00" * 16) -> dict:
    return {
        "name": "Remote", "uuid": "r1", "unicast": 0x0017, "elements": 24,
        "device_key": device_key,
        "modules": [
            {"index": i, "enabled": i < 3, "name": None, "scenes": [],
             "clients": _clients(0x0017 + 4 * i, app=i < 3)}
            for i in range(6)
        ],
    }


class FakeStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data
        self.saves += 1


class FakeRemote:
    """Answers Publication Sets like the remote does, while it is awake."""

    def __init__(self, answers: int | None = None, status: int = 0):
        self.answers = answers  # None: answer everything
        self.status = status
        self.sets: list[bytes] = []
        self.configurator: RemoteConfigurator | None = None

    async def send(self, dst: int, opcode: int, params: bytes) -> None:
        assert (dst, opcode) == (0x0017, OP_MODEL_PUBLICATION_SET)
        self.sets.append(params)
        if self.answers is not None:
            if self.answers == 0:
                return
            self.answers -= 1
        # Status echoes element, address, key index, TTL, period,
        # retransmit and model of the Set.
        status = bytes([self.status]) + params
        asyncio.get_running_loop().call_soon(
            self.configurator.on_publication_status, 0x0017, status,
        )


def _configurator(remote_dev: FakeRemote, store: FakeStore | None = None, remote=None):
    remote = remote or _remote()
    tasks = []

    def spawn(coro, _name):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task

    c = RemoteConfigurator([remote], store or FakeStore(), remote_dev.send, spawn)
    remote_dev.configurator = c
    return c, remote, tasks


def test_publication_set_layout():
    params = encode_publication_set(0x0023, 0x1001, publication_for(MODE_HA_ONLY, {"publish": None}))
    assert params.hex() == "2300fffe0000ff00090110"


def test_publications_per_mode():
    client = {"publish": dict(APP_PUB)}
    assert publication_for(MODE_APP, client) == APP_PUB
    assert publication_for(MODE_HA_ONLY, client)["address"] == HA_ONLY_ADDRESS
    assert publication_for(MODE_DISABLED, client)["address"] == 0
    # A model the app left without publication stays silent in app mode.
    assert publication_for(MODE_APP, {"publish": None})["address"] == 0


def test_status_parsing():
    status = parse_publication_status(bytes.fromhex("00" "2300fffe0000ff00090110"))
    assert (status["status"], status["element"], status["address"], status["model"]) == (
        0, 0x0023, HA_ONLY_ADDRESS, 0x1001)
    assert parse_publication_status(bytes(11)) is None


def test_app_mode_only_where_the_app_configured_the_position():
    remote = _remote()
    assert available_modes(remote["modules"][0]) == [MODE_APP, MODE_HA_ONLY, MODE_DISABLED]
    assert available_modes(remote["modules"][3]) == [MODE_HA_ONLY, MODE_DISABLED]


def test_change_is_pending_until_the_remote_confirms_every_model():
    fake = FakeRemote()
    store = FakeStore()
    c, remote, tasks = _configurator(fake, store)
    changes = []
    c.register_listener(lambda: changes.append(c.is_pending(remote, 3)))

    async def run():
        await c.async_set_mode(remote, 3, MODE_HA_ONLY)
        assert c.is_pending(remote, 3)
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert len(fake.sets) == 8
    assert {p[2:4] for p in fake.sets} == {HA_ONLY_ADDRESS.to_bytes(2, "little")}
    assert {int.from_bytes(p[:2], "little") for p in fake.sets} == {0x23, 0x24, 0x25, 0x26}
    assert not c.is_pending(remote, 3) and not c.has_error(remote, 3)
    assert c.mode(remote, 3) == MODE_HA_ONLY and c.is_ha_only(0x0017, 3)
    assert changes[0] is True and changes[-1] is False
    assert store.data["positions"]["remote_r1:3"]["pending"] == []


def test_remote_falling_asleep_leaves_the_rest_for_the_next_press(monkeypatch):
    monkeypatch.setattr(rc_mod, "STATUS_TIMEOUT", 0.01)
    fake = FakeRemote(answers=3)
    c, remote, tasks = _configurator(fake)

    async def run():
        await c.async_set_mode(remote, 3, MODE_HA_ONLY)
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)
        # Two unanswered Sets: it stopped instead of trying all 8.
        assert len(fake.sets) == 5
        assert c.is_pending(remote, 3)
        fake.answers = None
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert not c.is_pending(remote, 3)
    assert len(fake.sets) == 5 + 5  # the 3 confirmed ones are not sent again


def test_refused_model_is_reported_not_retried_forever():
    fake = FakeRemote(status=0x02)  # Invalid Model
    c, remote, tasks = _configurator(fake)

    async def run():
        await c.async_set_mode(remote, 0, MODE_DISABLED)
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert not c.is_pending(remote, 0)
    assert c.has_error(remote, 0)


def test_one_job_at_a_time_per_remote():
    fake = FakeRemote()
    c, remote, tasks = _configurator(fake)

    async def run():
        await c.async_set_mode(remote, 4, MODE_HA_ONLY)
        c.on_remote_activity(0x0017)
        c.on_remote_activity(0x0017)  # the press's retransmission
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert len(tasks) == 1 and len(fake.sets) == 8


def test_stored_modes_survive_a_restart():
    store = FakeStore({"positions": {
        "remote_r1:3": {"mode": MODE_HA_ONLY, "pending": [], "error": False},
        # A mode the position does not offer (app on a disabled position).
        "remote_r1:4": {"mode": MODE_APP, "pending": [], "error": False},
    }})
    c, remote, _ = _configurator(FakeRemote(), store)
    asyncio.run(c.async_load())
    assert c.mode(remote, 3) == MODE_HA_ONLY
    assert c.mode(remote, 4) == MODE_DISABLED
    assert c.mode(remote, 0) == MODE_APP


def test_without_device_key_nothing_is_sent():
    fake = FakeRemote()
    c, remote, tasks = _configurator(fake, remote=_remote(device_key=""))
    assert not c.can_configure(remote)

    async def run():
        await c.async_set_mode(remote, 3, MODE_HA_ONLY)
        c.on_remote_activity(0x0017)

    asyncio.run(run())
    assert tasks == [] and fake.sets == []


def test_unavailable_mode_is_rejected():
    c, remote, _ = _configurator(FakeRemote())
    with pytest.raises(ValueError):
        asyncio.run(c.async_set_mode(remote, 3, MODE_APP))


def test_entries_imported_without_the_app_configuration_cannot_be_configured():
    # Device key stored, but not how the app set up each control (an
    # export imported by an earlier build): "app" mode could not be shown.
    remote = _remote()
    for module in remote["modules"]:
        module["clients"] = []
    c, _, _ = _configurator(FakeRemote(), remote=remote)
    assert not c.can_configure(remote)


def test_late_status_still_confirms_the_model(monkeypatch):
    monkeypatch.setattr(rc_mod, "STATUS_TIMEOUT", 0.01)
    fake = FakeRemote(answers=0)  # answers nothing in time
    c, remote, tasks = _configurator(fake)

    async def run():
        await c.async_set_mode(remote, 3, MODE_HA_ONLY)
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)
        # Both Sets timed out; the first answer arrives afterwards.
        c.on_publication_status(0x0017, bytes([0]) + fake.sets[0])
        # An answer with another address confirms nothing.
        wrong = bytearray(fake.sets[1])
        wrong[2:4] = (0xC00F).to_bytes(2, "little")
        c.on_publication_status(0x0017, bytes([0]) + bytes(wrong))
        await asyncio.gather(*tasks)

    asyncio.run(run())
    pending = c._positions["remote_r1:3"]["pending"]
    assert len(pending) == 7
    assert [0x23, 0x1001] not in pending


def test_ha_only_counts_only_once_the_remote_confirmed_it():
    # Until then the position may still switch lights (their state must
    # still be read after a press), and a refused change never applies.
    c, remote, _ = _configurator(FakeRemote())
    c._positions["remote_r1:3"] = {"mode": MODE_HA_ONLY, "pending": [[0x23, 0x1001]]}
    assert not c.is_ha_only(0x0017, 3)
    c._positions["remote_r1:3"] = {"mode": MODE_HA_ONLY, "pending": [], "error": True}
    assert not c.is_ha_only(0x0017, 3)
    c._positions["remote_r1:3"] = {"mode": MODE_HA_ONLY, "pending": []}
    assert c.is_ha_only(0x0017, 3)


def test_the_last_active_position_cannot_be_disabled():
    # Only presses on a position that sends something wake the remote for
    # us; with every position disabled it could never be changed again.
    c, remote, _ = _configurator(FakeRemote())

    async def run():
        await c.async_set_mode(remote, 0, MODE_DISABLED)
        await c.async_set_mode(remote, 1, MODE_DISABLED)
        with pytest.raises(ValueError):
            await c.async_set_mode(remote, 2, MODE_DISABLED)

    asyncio.run(run())


def test_mode_changed_while_sending_is_sent_in_the_same_wake():
    fake = FakeRemote()
    c, remote, tasks = _configurator(fake)
    original = fake.send

    async def send_and_change_mind(dst, opcode, params):
        await original(dst, opcode, params)
        if len(fake.sets) == 2:
            await c.async_set_mode(remote, 3, MODE_DISABLED)

    fake_send = send_and_change_mind
    c._send = fake_send

    async def run():
        await c.async_set_mode(remote, 3, MODE_HA_ONLY)
        c.on_remote_activity(0x0017)
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert not c.is_pending(remote, 3)
    assert c.mode(remote, 3) == MODE_DISABLED
    # The last 8 Sets are the new mode (no publication).
    assert all(p[2:4] == b"\x00\x00" for p in fake.sets[-8:])


def test_position_whose_app_setup_could_not_be_read_is_not_unknown():
    remote = _remote()
    for client in remote["modules"][0]["clients"]:
        client["publish"] = None  # e.g. a virtual address
    c, _, _ = _configurator(FakeRemote(), remote=remote)
    assert c.mode(remote, 0) in c.modes(remote, 0)
