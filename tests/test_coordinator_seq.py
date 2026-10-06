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


def test_restart_after_unclean_shutdown_resumes_above_last_emitted():
    c1 = _coordinator()

    async def first_life():
        await c1.async_setup()
        last = 0
        for _ in range(10):
            last = await c1.next_seq(c1.session.src)
        # no async_shutdown: simulate power loss
        if c1._poll_task:
            c1._poll_task.cancel()
        return last

    last = asyncio.run(first_life())
    c2 = _coordinator()

    async def second_life():
        await c2.async_setup()
        seq = await c2.next_seq(c2.session.src)
        await c2.async_shutdown()
        return seq

    assert asyncio.run(second_life()) > last


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
