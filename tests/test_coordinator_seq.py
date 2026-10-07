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

    async def get_onoff(self, dst):
        self.calls.append(("onoff", dst))

    async def get_ctl(self, dst):
        self.calls.append(("ctl", dst))


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
