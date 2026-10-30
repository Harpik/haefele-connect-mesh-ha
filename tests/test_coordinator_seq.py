"""SEQ lifecycle tests for HaefeleCoordinator.

Covers the guarantees that keep the lamps accepting our frames:

    * SEQ never wraps (a wrapped SEQ is dropped as a replay, silently).
    * The active SRC rotates to a fresh, never-used address before its
      SEQ space runs out, and the rotation survives restarts.
    * SEQ is persisted in blocks, and the on-disk value is always >= the
      last SEQ handed out.
    * State polling sends exactly one Get per node.
    * MeshSession builds each PDU with a single, consistent SRC even if
      the SEQ provider rotates SRC mid-call.

Home Assistant is not installed in the test env, so the few HA helpers
coordinator.py imports are stubbed below.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import ClassVar

import pytest


def _ensure(name: str, **attrs: object) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store.

    Data is shared per key across instances so a second coordinator
    "restarts" on top of what the first one persisted.
    """

    data: ClassVar[dict[str, object]] = {}
    saves: ClassVar[dict[str, int]] = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        value = FakeStore.data.get(self.key)
        return dict(value) if isinstance(value, dict) else value

    async def async_save(self, data):
        FakeStore.data[self.key] = dict(data)
        FakeStore.saves[self.key] = FakeStore.saves.get(self.key, 0) + 1


class FakeDataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass

    def async_set_updated_data(self, data):  # pragma: no cover - unused
        self.data = data

    def __class_getitem__(cls, item):  # pragma: no cover - generic alias
        return cls


_ensure("homeassistant.helpers")
_ensure("homeassistant.helpers.storage", Store=FakeStore)
_ensure(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=FakeDataUpdateCoordinator,
)

from custom_components.haefele_mesh import coordinator as coord_mod
from custom_components.haefele_mesh.const import (
    LEGACY_SRC_ADDRESSES,
    NODE_ADDRESS_MARGIN,
    ROTATED_SRC_SEQ_START,
    ROTATION_SEARCH_TOP,
    SEQ_MAX,
    SEQ_PERSIST_BLOCK,
    SEQ_ROTATE_THRESHOLD,
)
from custom_components.haefele_mesh.coordinator import (
    STORE_KEY_ACTIVE_SRC,
    HaefeleCoordinator,
    SeqExhaustedError,
)
from custom_components.haefele_mesh.gatt import MeshSession

STORE_KEY = coord_mod.SEQ_STORAGE_KEY
NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"


class FakeProxy:
    """Records what the coordinator asks of the GATT layer."""

    instances: ClassVar[list[FakeProxy]] = []

    def __init__(self, hass=None, session=None, message_handler=None,
                 reconnect_callback=None):
        self.session = session
        self.filter_addresses: list[int] = []
        self.pushed_filters: list[list[int]] = []
        self.calls: list[tuple[str, int]] = []
        self.is_connected = False
        FakeProxy.instances.append(self)

    def set_candidates(self, candidates):
        self.candidates = candidates

    def set_filter_addresses(self, addresses):
        self.filter_addresses = list(addresses)

    async def connect_any(self):
        return False

    async def disconnect(self):
        pass

    async def add_filter_addresses(self, addresses):
        self.pushed_filters.append(list(addresses))

    async def refresh_filter(self):
        self.calls.append(("refresh_filter", list(self.filter_addresses)))

    async def get_onoff(self, dst):
        self.calls.append(("onoff", dst))

    async def get_ctl(self, dst):
        self.calls.append(("ctl", dst))

    async def send_devkey(self, dst, opcode, params):
        self.calls.append(("devkey", dst, opcode, params))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeStore.data = {}
    FakeStore.saves = {}
    FakeProxy.instances = []
    monkeypatch.setattr(coord_mod, "MeshProxyConnection", FakeProxy)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)


def _config(**overrides):
    cfg = {
        "network_key": NET_KEY_HEX,
        "app_key": APP_KEY_HEX,
        "iv_index": 1,
        "provisioner_address": 0x7FFD,
        "src_address_base": 0x00C8,
        "nodes": [
            {"name": "TW spot", "mac": "AA:BB:CC:DD:EE:01", "unicast": 0x0017,
             "groups": [0xC001], "device_type": "tunable_white"},
            {"name": "Dim", "mac": "AA:BB:CC:DD:EE:02", "unicast": 0x002F,
             "groups": [], "device_type": "dimmable"},
            {"name": "Relay", "mac": "AA:BB:CC:DD:EE:03", "unicast": 0x003A,
             "groups": [], "device_type": "onoff"},
        ],
    }
    cfg.update(overrides)
    return cfg


def _coordinator(**overrides) -> HaefeleCoordinator:
    return HaefeleCoordinator(object(), _config(**overrides))


async def _setup(c: HaefeleCoordinator) -> None:
    await c.async_setup()
    await c.async_shutdown()


# ---------------------------------------------------------------------------
# Never wrap
# ---------------------------------------------------------------------------

def test_seq_never_wraps_when_rotation_is_impossible():
    c = _coordinator()
    # Not the active SRC -> no rotation is attempted for it.
    c._seq_state[0x0100] = SEQ_MAX - 1

    async def run():
        assert await c.next_seq(0x0100) == SEQ_MAX
        with pytest.raises(SeqExhaustedError):
            await c.next_seq(0x0100)

    asyncio.run(run())
    assert c._seq_state[0x0100] == SEQ_MAX


def test_startup_jump_clamps_instead_of_masking():
    FakeStore.data[STORE_KEY] = {"256": SEQ_MAX - 5}  # retired SRC 0x0100
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c._seq_state[0x0100] == SEQ_MAX
    assert all(0 <= v <= SEQ_MAX for v in c._seq_state.values())


# ---------------------------------------------------------------------------
# SRC rotation
# ---------------------------------------------------------------------------

def test_rotates_to_fresh_src_when_threshold_is_crossed():
    FakeStore.data[STORE_KEY] = {"200": SEQ_ROTATE_THRESHOLD - 300}
    c = _coordinator()

    async def run():
        await c.async_setup()
        assert c.session.src == 0x00C8
        # startup jump (+200) leaves us 100 below the threshold
        for _ in range(100):
            await c.next_seq(c.session.src)
        assert c.session.src != 0x00C8
        new_src = c.session.src
        assert await c.next_seq(new_src) == ROTATED_SRC_SEQ_START + 1
        await c.async_shutdown()
        return new_src

    new_src = asyncio.run(run())
    stored = FakeStore.data[STORE_KEY]
    assert stored[STORE_KEY_ACTIVE_SRC] == new_src
    # The retired SRC stays in the store so it is never reused.
    assert stored["200"] >= SEQ_ROTATE_THRESHOLD


def test_upgrade_with_seq_already_past_threshold_rotates_before_connecting():
    # 0.4.4-format store: exact SEQ, no _active_src, already past threshold.
    FakeStore.data[STORE_KEY] = {"200": SEQ_ROTATE_THRESHOLD + 10, "_iv_index": 3}
    c = _coordinator()
    asyncio.run(_setup(c))
    new_src = c.session.src
    assert new_src != 0x00C8
    assert c.session.iv_index == 3
    proxy = FakeProxy.instances[0]
    # The very first filter list the proxy got already has the fresh SRC.
    assert new_src in proxy.filter_addresses
    assert 0x00C8 not in proxy.filter_addresses
    assert FakeStore.data[STORE_KEY][STORE_KEY_ACTIVE_SRC] == new_src


def test_persisted_active_src_wins_over_config_after_restart():
    FakeStore.data[STORE_KEY] = {
        "200": SEQ_ROTATE_THRESHOLD + 10,
        "217": 5000,
        STORE_KEY_ACTIVE_SRC: 0x00D9,
    }
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c.session.src == 0x00D9
    assert c._seq_state[0x00D9] >= 5000


def test_rotation_skips_nodes_legacy_and_retired_addresses():
    nodes = [
        {"name": "Next door", "mac": "AA:BB:CC:DD:EE:09", "unicast": 0x00C9,
         "groups": [], "device_type": "tunable_white"},
    ]
    c = _coordinator(nodes=nodes)
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD, 0x00D9: 10}  # 0x00D9 retired
    taken = c._reserved_addresses()
    assert set(range(0x00C9, 0x00C9 + NODE_ADDRESS_MARGIN)) <= taken
    assert set(LEGACY_SRC_ADDRESSES) <= taken
    assert 0x7FFD in taken
    # 0x00C9..0x00D8 is the node's margin, 0x00D9 was used before.
    assert c._pick_rotation_src() == 0x00DA


def test_rotation_skips_reserved_unicasts_of_skipped_nodes():
    # A 4-element remote at 0x00C9 has no light entity, but emits with that SRC.
    c = _coordinator(reserved_unicasts=[{"unicast": 0x00C9, "elements": 4}])
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert {0x00C9, 0x00CA, 0x00CB, 0x00CC} <= c._reserved_addresses()
    assert c._pick_rotation_src() == 0x00CD


def test_rotation_prefers_addresses_outside_provisioner_ranges():
    c = _coordinator(allocated_unicast_ranges=[[0x0001, 0x1000]])
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert c._pick_rotation_src() == ROTATION_SEARCH_TOP
    c._seq_state[ROTATION_SEARCH_TOP] = 10  # already used once
    assert c._pick_rotation_src() == ROTATION_SEARCH_TOP - 1


def test_rotation_finds_gaps_between_ranges():
    c = _coordinator(allocated_unicast_ranges=[[0x0001, 0x1000], [0x1100, 0x7FFF]])
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert c._pick_rotation_src() == 0x10FF
    c2 = _coordinator(allocated_unicast_ranges=[[0x7000, 0x7EFF]])
    c2._active_src = 0x00C8
    c2._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert c2._pick_rotation_src() == 0x6FFF


def test_rotation_falls_back_inside_ranges_when_nothing_else_is_free():
    c = _coordinator(allocated_unicast_ranges=[[0x0001, 0x7FFF]])
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert c._pick_rotation_src() == 0x00C9


def test_rotation_ignores_malformed_ranges():
    c = _coordinator(allocated_unicast_ranges=[["0001", "1000"], [5], None])
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    assert c._pick_rotation_src() == 0x00C9  # legacy behaviour


def test_active_src_colliding_with_a_node_is_rotated_at_startup():
    # An old entry on 0x00C8; a Reconfigure has since recorded a light there.
    FakeStore.data[STORE_KEY] = {"200": 5000}
    c = _coordinator(reserved_unicasts=[{"unicast": 0x00C4, "elements": 8}])
    asyncio.run(_setup(c))
    assert c.session.src != 0x00C8
    assert FakeProxy.instances[0].filter_addresses
    assert 0x00C8 not in FakeProxy.instances[0].filter_addresses


def test_active_src_not_colliding_is_kept():
    FakeStore.data[STORE_KEY] = {"200": 5000}
    c = _coordinator(reserved_unicasts=[{"unicast": 0x00C9, "elements": 4}])
    asyncio.run(_setup(c))
    assert c.session.src == 0x00C8


def test_rotation_pushes_new_src_to_live_proxy_filter():
    FakeStore.data[STORE_KEY] = {"200": SEQ_ROTATE_THRESHOLD - 201}

    async def run():
        c = _coordinator()
        await c.async_setup()
        proxy = FakeProxy.instances[0]
        proxy.is_connected = True
        await c.next_seq(c.session.src)  # crosses the threshold
        await asyncio.sleep(0)  # let the fire-and-forget task run
        await c.async_shutdown()
        return c, proxy

    c, proxy = asyncio.run(run())
    assert proxy.pushed_filters == [[c.session.src]]
    assert c.session.src in proxy.filter_addresses


def test_no_free_address_logs_once_and_keeps_old_src(caplog):
    c = _coordinator()
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD}
    c._pick_rotation_src = lambda: None

    async def run():
        await c.next_seq(0x00C8)
        await c.next_seq(0x00C8)

    with caplog.at_level("ERROR"):
        asyncio.run(run())
    assert c._active_src == 0x00C8
    assert sum("no free unicast address" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# Block persistence
# ---------------------------------------------------------------------------

def test_seq_is_persisted_in_blocks_and_store_stays_ahead():
    c = _coordinator()
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: 1000}
    n = 1000

    async def run():
        last = 0
        for _ in range(n):
            last = await c.next_seq(0x00C8)
            assert FakeStore.data[STORE_KEY]["200"] >= last
        return last

    last = asyncio.run(run())
    assert last == 1000 + n
    saves = FakeStore.saves[STORE_KEY]
    assert saves <= n // SEQ_PERSIST_BLOCK + 2, saves


def _emit_then_crash(c, n):
    async def run():
        await c.async_setup()
        last = 0
        for _ in range(n):
            last = await c.next_seq(c.session.src)
        # no async_shutdown: simulate power loss
        if c._poll_task:
            c._poll_task.cancel()
        return last
    return asyncio.run(run())


def _first_seq_after_restart(c):
    async def run():
        await c.async_setup()
        seq = await c.next_seq(c.session.src)
        await c.async_shutdown()
        return seq
    return asyncio.run(run())


def test_restart_after_unclean_shutdown_resumes_above_last_emitted(monkeypatch):
    # Without the +200 startup jump, only the persisted block ceiling can
    # keep the restart above what was really sent. Emit several blocks so
    # a store that lags behind (e.g. saving the last SEQ instead of the
    # ceiling, or not refreshing it) is caught.
    monkeypatch.setattr(coord_mod, "SEQ_STARTUP_JUMP", 0)
    last = _emit_then_crash(_coordinator(), 3 * SEQ_PERSIST_BLOCK + 17)
    assert _first_seq_after_restart(_coordinator()) > last


# ---------------------------------------------------------------------------
# Save-then-commit
# ---------------------------------------------------------------------------

def _failing_saves(monkeypatch, key, fail_times):
    """Make the first `fail_times` saves to `key` raise OSError."""
    real = FakeStore.async_save
    state = {"left": fail_times, "attempts": 0}

    async def save(self, data):
        if self.key == key:
            state["attempts"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                raise OSError("disk full")
        await real(self, data)

    monkeypatch.setattr(FakeStore, "async_save", save)
    return state


def test_failed_block_save_never_hands_out_unpersisted_seq(monkeypatch):
    c = _coordinator()
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: 1000}
    state = _failing_saves(monkeypatch, STORE_KEY, fail_times=1)

    async def run():
        with pytest.raises(OSError):
            await c.next_seq(0x00C8)
        handed = []
        for _ in range(5):
            seq = await c.next_seq(0x00C8)
            handed.append(seq)
            # every SEQ handed out is covered by what is on disk
            assert FakeStore.data[STORE_KEY]["200"] >= seq
        return handed

    handed = asyncio.run(run())
    assert handed == [1001, 1002, 1003, 1004, 1005]
    assert state["attempts"] == 2  # the failed save was retried, not skipped


def test_failed_rotation_save_keeps_old_src_and_retries(monkeypatch):
    c = _coordinator()
    c._active_src = 0x00C8
    c._seq_state = {0x00C8: SEQ_ROTATE_THRESHOLD - 1}
    c._seq_reserved = {0x00C8: SEQ_ROTATE_THRESHOLD + 1000}  # no block save due
    _failing_saves(monkeypatch, STORE_KEY, fail_times=1)

    async def run():
        await c.next_seq(0x00C8)  # crosses threshold, rotation save fails
        assert c._active_src == 0x00C8
        assert STORE_KEY not in FakeStore.data
        await c.next_seq(0x00C8)  # retry succeeds
        return c._active_src

    new_src = asyncio.run(run())
    assert new_src != 0x00C8
    assert FakeStore.data[STORE_KEY][STORE_KEY_ACTIVE_SRC] == new_src


# ---------------------------------------------------------------------------
# Per-entry storage
# ---------------------------------------------------------------------------

def _entry_coordinator(entry_id, **overrides):
    return HaefeleCoordinator(object(), _config(**overrides), entry_id=entry_id)


def test_two_entries_do_not_overwrite_each_others_seq(monkeypatch):
    monkeypatch.setattr(coord_mod, "SEQ_STARTUP_JUMP", 0)
    a, b = _entry_coordinator("A"), _entry_coordinator("B")

    async def run():
        await a.async_setup()
        await b.async_setup()
        last_a = 0
        for _ in range(3 * SEQ_PERSIST_BLOCK):
            last_a = await a.next_seq(a.session.src)
        await b.next_seq(b.session.src)  # B saves after A
        await a.async_shutdown()
        await b.async_shutdown()
        return last_a

    last_a = asyncio.run(run())
    assert FakeStore.data[f"{STORE_KEY}_A"]["200"] >= last_a
    assert _first_seq_after_restart(_entry_coordinator("A")) > last_a


def test_rotated_src_of_one_entry_does_not_leak_to_another():
    a = _entry_coordinator("A")
    a._active_src = 0x00D9
    a._seq_reserved = {0x00C8: SEQ_ROTATE_THRESHOLD, 0x00D9: 5}
    asyncio.run(a._save_seq())  # A persists its rotated SRC
    # B is a network added with this release (per-entry marker set).
    b = _entry_coordinator("B", seq_store="per_entry")
    asyncio.run(_setup(b))
    assert b.session.src == 0x00C8
    assert b._seq_state.get(0x00C8, 0) < SEQ_ROTATE_THRESHOLD  # nothing inherited


def test_pre_existing_entry_migrating_never_adopts_anothers_active_src():
    # Both entries predate per-entry storage (no marker): A mirrors to the
    # shared file, then B migrates from it on its first start.
    a = _entry_coordinator("A")
    a._active_src = 0x00D9
    a._seq_reserved = {0x00C8: 1000, 0x00D9: 5}
    asyncio.run(a._save_seq())
    assert STORE_KEY_ACTIVE_SRC not in FakeStore.data[STORE_KEY]
    b = _entry_coordinator("B")
    asyncio.run(_setup(b))
    assert b.session.src == 0x00C8


def test_new_entry_never_reads_or_writes_the_shared_store():
    FakeStore.data[STORE_KEY] = {"200": 9_000_000, "_iv_index": 3}
    c = _entry_coordinator("N", seq_store="per_entry")

    async def run():
        await c.async_setup()
        await c.next_seq(c.session.src)
        await c.async_shutdown()

    asyncio.run(run())
    assert FakeStore.data[STORE_KEY] == {"200": 9_000_000, "_iv_index": 3}
    assert c.session.iv_index == 1  # config value, not the shared file's 3


def test_per_entry_store_migrates_from_shared_and_keeps_it_mirrored():
    FakeStore.data[STORE_KEY] = {"200": 123456, "_iv_index": 7}
    c = _entry_coordinator("A")

    async def run():
        await c.async_setup()
        seq = await c.next_seq(c.session.src)
        await c.async_shutdown()
        return seq

    seq = asyncio.run(run())
    assert seq > 123456
    assert c.session.iv_index == 7
    assert FakeStore.data[f"{STORE_KEY}_A"]["200"] >= seq
    # shared file still current, so a downgrade resumes safely
    assert FakeStore.data[STORE_KEY]["200"] >= seq


# ---------------------------------------------------------------------------
# Background task ownership
# ---------------------------------------------------------------------------

def test_rotation_filter_task_is_tracked_and_cancelled_on_shutdown():
    c = _coordinator()

    async def run():
        await c.async_setup()
        proxy = FakeProxy.instances[0]
        proxy.is_connected = True
        gate = asyncio.Event()

        async def slow_add(addresses):
            await gate.wait()

        proxy.add_filter_addresses = slow_add
        c._on_src_rotated(0x00C9)
        assert len(c._background_tasks) == 1
        task = next(iter(c._background_tasks))
        await c.async_shutdown()
        return task

    task = asyncio.run(run())
    assert task.cancelled()
    assert not c._background_tasks


# ---------------------------------------------------------------------------
# Polling: one Get per node
# ---------------------------------------------------------------------------

def test_poll_sends_one_get_per_node_matching_capability():
    c = _coordinator()
    c.proxy = FakeProxy()
    asyncio.run(c._poll_all_nodes())
    assert c.proxy.calls == [("ctl", 0x0017), ("onoff", 0x002F), ("onoff", 0x003A)]


# ---------------------------------------------------------------------------
# MeshSession: SRC snapshot
# ---------------------------------------------------------------------------

def test_pdu_uses_src_from_before_seq_provider_rotates():
    holder: dict[str, MeshSession] = {}

    async def rotating_provider(src):
        holder["s"].src = 0x0123  # rotation happens inside the provider
        return 42

    session = MeshSession(NET_KEY_HEX, APP_KEY_HEX, 0x00C8, 1, rotating_provider)
    holder["s"] = session
    pdu = asyncio.run(session.build_access_network_pdu(0x0017, 0x8201, b""))
    _ctl, seq, src, _plain = session._decode_network_header(pdu)
    assert (seq, src) == (42, 0x00C8)


# ---------------------------------------------------------------------------
# IV Index provenance (beacon jump limit only applies to a known IV)
# ---------------------------------------------------------------------------

def test_iv_from_connect_export_is_not_confirmed():
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c.session.iv_index == 1
    assert c.session.iv_confirmed is False


def test_saved_export_iv_stays_unconfirmed_across_restarts():
    # The export's IV is written to the store on the first start; a restart
    # (or a ConfigEntryNotReady retry) must not promote it to "confirmed".
    asyncio.run(_setup(_coordinator()))
    assert FakeStore.data[STORE_KEY]["_iv_index"] == 1
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c.session.iv_confirmed is False


def test_beacon_confirmed_iv_survives_a_restart():
    async def first_run():
        c = _coordinator()
        await c.async_setup()
        c.session.iv_index = 9
        c.session.iv_confirmed = True  # as set by an authenticated beacon
        await c.async_shutdown()

    asyncio.run(first_run())
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c.session.iv_index == 9
    assert c.session.iv_confirmed is True


def test_iv_stored_by_older_release_is_not_confirmed():
    FakeStore.data[STORE_KEY] = {"200": 1000, "_iv_index": 7}
    c = _coordinator()
    asyncio.run(_setup(c))
    assert c.session.iv_index == 7
    assert c.session.iv_confirmed is False


def test_shutdown_after_failed_store_load_keeps_the_store(monkeypatch):
    # __init__ shuts the coordinator down when setup fails; if the failure
    # was reading the store, saving the empty state would wipe the counters.
    stored = {"200": 500000, "_iv_index": 77, "_iv_confirmed": 1}
    FakeStore.data["haefele_mesh_seq_E1"] = dict(stored)
    c = _entry_coordinator("E1", seq_store="per_entry")

    async def broken_load():
        raise OSError("read error")

    async def run():
        monkeypatch.setattr(c._seq_store, "async_load", broken_load)
        with pytest.raises(OSError):
            await c.async_setup()
        await c.async_shutdown()

    asyncio.run(run())
    assert FakeStore.data["haefele_mesh_seq_E1"] == stored


def test_filter_addresses_put_src_then_groups_then_unicasts():
    # A proxy that falls back to a small accept list keeps the first ones.
    c = _coordinator()
    asyncio.run(_setup(c))
    assert FakeProxy.instances[0].filter_addresses == [0x00C8, 0xC001, 0x0017, 0x002F, 0x003A]


# ---------------------------------------------------------------------------
# Remote presses
# ---------------------------------------------------------------------------

_REMOTE = {
    "name": "Remote", "uuid": "r1", "unicast": 0x0100, "elements": 24,
    "modules": [{"index": i, "enabled": True, "name": None, "scenes": []} for i in range(6)],
}


def _connected_proxy() -> FakeProxy:
    proxy = FakeProxy()
    proxy.is_connected = True
    return proxy


def test_remote_press_reaches_listeners_once_and_polls_the_lights(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)
    c = _coordinator(remotes=[_REMOTE])
    c.proxy = _connected_proxy()
    events = []
    c.register_remote_listener(events.append)

    async def run():
        # The remote's press and its retransmission (same TID).
        c._dispatch_status(0x0104, 0x8203, bytes.fromhex("011e"))
        c._dispatch_status(0x0104, 0x8203, bytes.fromhex("011e"))
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert [(e.module, e.event_type) for e in events] == [(1, "turn_on")]
    # One immediate state read of every light, not one per frame.
    assert c.proxy.calls == [("ctl", 0x0017), ("onoff", 0x002F), ("onoff", 0x003A)]


def test_light_status_is_not_taken_for_a_remote_press():
    c = _coordinator(remotes=[_REMOTE])
    events = []
    c.register_remote_listener(events.append)
    c._dispatch_status(0x002F, 0x8204, bytes.fromhex("01"))
    assert events == []
    assert c._quick_poll_task is None


def test_unsubscribed_listener_gets_nothing():
    c = _coordinator(remotes=[_REMOTE])
    c.proxy = _connected_proxy()
    events = []
    unsub = c.register_remote_listener(events.append)
    unsub()

    async def run():
        c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0101"))
        for t in list(c._background_tasks):
            t.cancel()

    asyncio.run(run())
    assert events == []


def _remote_with_group(group):
    modules = [{"index": i, "enabled": True, "name": None, "scenes": [],
                "group_address": group if i == 0 else None} for i in range(6)]
    return {**_REMOTE, "modules": modules}


def test_quick_poll_reads_only_the_lights_of_the_pressed_group(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)
    c = _coordinator(remotes=[_remote_with_group(0xC001)])
    c.proxy = _connected_proxy()

    async def run():
        c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0101"))
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert c.proxy.calls == [("ctl", 0x0017)]  # only the light in group C001


def test_press_during_a_quick_poll_is_read_in_another_pass(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)
    c = _coordinator(remotes=[_remote_with_group(0xC001)])
    c.proxy = _connected_proxy()
    original = c.proxy.get_ctl

    async def get_ctl_and_press_again(dst):
        await original(dst)
        if len(c.proxy.calls) == 1:  # a second press while the first poll runs
            c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0002"))

    c.proxy.get_ctl = get_ctl_and_press_again

    async def run():
        c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0101"))
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert c.proxy.calls == [("ctl", 0x0017), ("ctl", 0x0017)]


def test_quick_poll_waits_for_a_running_periodic_poll(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)
    c = _coordinator(remotes=[_remote_with_group(0xC001)])
    c.proxy = _connected_proxy()

    async def run():
        await c._poll_lock.acquire()  # a periodic poll is in progress
        c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0101"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert c.proxy.calls == []
        c._poll_lock.release()
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert c.proxy.calls == [("ctl", 0x0017)]


def test_press_during_shutdown_starts_no_poll(monkeypatch):
    # Frames keep arriving until the proxy is disconnected. A quick poll
    # started then would outlive shutdown and reconnect the proxy.
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    c = _coordinator(remotes=[_REMOTE])
    c.proxy = _connected_proxy()

    async def press_while_disconnecting():
        c._dispatch_status(0x0104, 0x8203, bytes.fromhex("011e"))
        await asyncio.sleep(0)

    c.proxy.disconnect = press_while_disconnecting

    async def run():
        await c.async_shutdown()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert c._quick_poll_task is None
    assert not c._background_tasks
    assert c.proxy.calls == []


def test_quick_poll_sends_nothing_while_the_link_is_down(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    c = _coordinator(remotes=[_REMOTE])
    c.proxy = FakeProxy()  # not connected

    async def run():
        c._dispatch_status(0x0104, 0x8203, bytes.fromhex("011e"))
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert c.proxy.calls == []


# ---------------------------------------------------------------------------
# Remote position modes
# ---------------------------------------------------------------------------

def _configurable_remote():
    clients = [{"element": 0x010C, "model": 0x1001, "publish": None}]
    modules = [{"index": i, "enabled": i == 0, "name": None, "scenes": [],
                "group_address": 0xC001 if i == 0 else None,
                "clients": clients if i == 3 else []} for i in range(6)]
    return {**_REMOTE, "device_key": "00" * 16, "modules": modules}


def test_ha_only_address_is_listened_to_only_when_a_position_uses_it():
    c = _coordinator(remotes=[_configurable_remote()])
    asyncio.run(_setup(c))
    # Not taking a slot of a proxy's 4-address accept list for nothing.
    assert 0xFEFF not in FakeProxy.instances[0].filter_addresses

    async def run():
        await c.remote_config.async_set_mode(c.remotes_cfg[0], 3, "ha_only")
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert FakeProxy.instances[0].filter_addresses[:2] == [0x00C8, 0xFEFF]


def test_live_filter_is_rebuilt_only_when_ha_only_starts_or_stops():
    c = _coordinator(remotes=[_configurable_remote()])
    c.proxy = _connected_proxy()
    remote = c.remotes_cfg[0]

    async def run():
        await c.remote_config.async_set_mode(remote, 3, "ha_only")
        # Every confirmed Set notifies again: no new filter traffic.
        for _ in range(8):
            c._on_remote_config_change()
        await asyncio.gather(*c._background_tasks)
        # Back off "HA only" (the only position this remote can configure).
        c.remote_config._positions["remote_r1:3"] = {"mode": "disabled", "pending": []}
        c._on_remote_config_change()
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    refreshes = [call[1] for call in c.proxy.calls if call[0] == "refresh_filter"]
    # The whole list each time, so an accept list that is full already
    # takes 0xFEFF in priority order, and drops it again afterwards.
    assert len(refreshes) == 2
    assert refreshes[0][:2] == [0xFEFF, 0xC001]  # no session: no SRC first
    assert 0xFEFF not in refreshes[1]
    assert c.proxy.pushed_filters == []


def test_press_sends_pending_configuration_and_status_confirms_it():
    c = _coordinator(remotes=[_configurable_remote()])
    c.proxy = _connected_proxy()
    remote = c.remotes_cfg[0]

    async def run():
        await c.remote_config.async_set_mode(remote, 3, "ha_only")
        c._dispatch_status(0x0100, 0x8203, bytes.fromhex("0105"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sent = [call for call in c.proxy.calls if call[0] == "devkey"]
        assert len(sent) == 1 and sent[0][1:3] == (0x0100, 0x03)
        # The remote's answer comes from its primary element and is no press.
        events = []
        c.register_remote_listener(events.append)
        c._dispatch_status(0x0100, 0x8019, bytes([0]) + sent[0][3])
        await asyncio.gather(*c._background_tasks)
        assert events == []

    asyncio.run(run())
    assert not c.remote_config.is_pending(remote, 3)


def test_press_on_a_ha_only_position_reads_no_light(monkeypatch):
    monkeypatch.setattr(coord_mod, "QUICK_POLL_DELAY", 0)
    c = _coordinator(remotes=[_configurable_remote()])
    c.proxy = _connected_proxy()
    remote = c.remotes_cfg[0]
    c.remote_config._positions["remote_r1:3"] = {"mode": "ha_only", "pending": []}

    async def run():
        c._dispatch_status(0x010C, 0x8203, bytes.fromhex("0106"))  # position 4
        await asyncio.gather(*c._background_tasks)

    asyncio.run(run())
    assert c.proxy.calls == []
    assert c.remote_config.mode(remote, 3) == "ha_only"
