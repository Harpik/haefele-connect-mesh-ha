"""Stuck proxy link watchdog.

Real incident (26-11): the GATT link to the active proxy stayed open
(``is_connected`` True) for hours while no frame went through it. The
heartbeat only looked at ``is_connected``, so it never reconnected and
kept every light "available" while commands went nowhere. A reload,
which picked the other proxy, fixed it.

These tests drive the coordinator heartbeat with a fake proxy whose link
can be open but silent, and the real ``MeshProxyConnection`` for the
candidate ordering and the forced drop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from types import SimpleNamespace

import pytest

# isort: off
# Imported first: installs the HA stubs (Store, DataUpdateCoordinator) that
# coordinator.py needs, shared with that file so the two never disagree.
from tests.test_coordinator_seq import FakeStore, _config
# isort: on

from custom_components.haefele_mesh import coordinator as coord_mod
from custom_components.haefele_mesh import gatt as gatt_mod
from custom_components.haefele_mesh.coordinator import HaefeleCoordinator
from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession

NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"

OP_ONOFF_STATUS = 0x8204
DESAYUNOS = ("84:FC:E6:94:7F:F6", "Luz desayunos")
FUEGOS = ("48:31:B7:6B:0F:C2", "Cocina fuegos")
LIGHTS = (0x0017, 0x002F, 0x003A)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now


class MeshProxy:
    """Fake proxy link. Per-proxy behaviour, keyed by MAC:

    * ``"ok"``: lights answer every Get.
    * ``"silent"``: writes go out, nothing ever comes back.
    * ``"hung"``: the write itself never completes.
    """

    def __init__(self, hass=None, session=None, message_handler=None,
                 reconnect_callback=None):
        self.handler = message_handler
        self.behaviour: dict[str, str] = {DESAYUNOS[0]: "ok", FUEGOS[0]: "ok"}
        self.order = [DESAYUNOS, FUEGOS]
        self.is_connected = False
        self.active_mac: str | None = None
        self.active_name = ""
        self.gets: list[int] = []
        self.drops: list[str | None] = []
        self.connects: list[str | None] = []
        self.link_generation = 0
        # Set by a test to make connect_any slow (a real BLE connect is).
        self.connect_delay = 0.0
        MeshProxy.last = self

    def set_candidates(self, candidates):
        pass

    def set_filter_addresses(self, addresses):
        pass

    async def connect_any(self, timeout_per_candidate=15.0, avoid=None):
        self.connects.append(avoid)
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.is_connected:
            return True
        order = [c for c in self.order if c[0] != avoid] + [
            c for c in self.order if c[0] == avoid
        ]
        if not order:
            return False
        self.active_mac, self.active_name = order[0]
        self.is_connected = True
        self.link_generation += 1
        return True

    async def drop_link(self, timeout=10.0, expected_generation=None):
        if (
            expected_generation is not None
            and expected_generation != self.link_generation
        ):
            return None
        mac = self.active_mac
        self.drops.append(mac)
        self.is_connected = False
        self.active_mac = None
        self.active_name = ""
        return mac

    async def disconnect(self):
        self.is_connected = False

    async def _get(self, dst):
        if not self.is_connected:
            raise ConnectionError("No active mesh-proxy connection")
        self.gets.append(dst)
        mode = self.behaviour[self.active_mac]
        if mode == "hung":
            await asyncio.Event().wait()
        if mode == "ok":
            asyncio.get_running_loop().call_soon(
                self.handler, dst, OP_ONOFF_STATUS, b"\x01",
            )

    async def get_onoff(self, dst):
        await self._get(dst)

    async def get_ctl(self, dst):
        await self._get(dst)

    async def refresh_filter(self):
        pass

    async def add_filter_addresses(self, addresses):
        pass


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(coord_mod, "time", c)
    return c


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    FakeStore.data = {}
    FakeStore.saves = {}
    monkeypatch.setattr(coord_mod, "MeshProxyConnection", MeshProxy)
    monkeypatch.setattr(coord_mod, "LINK_PROBE_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(coord_mod, "STATE_POLL_PER_NODE_GAP", 0)
    # No background polling: each test decides what traffic there is.
    async def _no_poll(self):
        return None

    monkeypatch.setattr(HaefeleCoordinator, "_state_poll_loop", _no_poll)


@pytest.fixture
def notifications(monkeypatch):
    sent: list[dict] = []
    pn = types.ModuleType("homeassistant.components.persistent_notification")

    def async_create(hass, message, title=None, notification_id=None):
        sent.append({"title": title, "message": message, "id": notification_id})

    pn.async_create = async_create
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.persistent_notification", pn,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.components"], "persistent_notification", pn,
        raising=False,
    )
    return sent


class Coordinator(HaefeleCoordinator):
    """Records what the entities were told."""

    def async_set_updated_data(self, data):
        # Entities read coordinator.is_available() when told, so what
        # matters is the availability map at that moment.
        self.pushed = [*getattr(self, "pushed", []), data]
        self.seen = [*getattr(self, "seen", []), set(self.availability.values())]


async def _started() -> tuple[Coordinator, MeshProxy]:
    c = Coordinator(object(), _config(), entry_id="e1")
    await c.async_setup()
    return c, MeshProxy.last


async def _poll(c) -> None:
    """One state poll, letting the lights' replies arrive."""
    await c._poll_all_nodes()
    for _ in range(3):
        await asyncio.sleep(0)


def _available(result) -> set[bool]:
    return {v["available"] for v in result.values()}


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------

def test_open_but_silent_link_is_dropped_and_reconnected_via_other_proxy(
    clock, caplog,
):
    """The 26-11 case: connected via Luz desayunos, nothing gets through."""
    caplog.set_level(logging.WARNING, logger=coord_mod.__name__)

    async def run():
        c, proxy = await _started()
        assert proxy.active_mac == DESAYUNOS[0]
        proxy.behaviour[DESAYUNOS[0]] = "silent"
        clock.now += 3 * 3600  # hours with is_connected still True
        result = await c._async_update_data()
        await c.async_shutdown()
        return c, proxy, result

    c, proxy, result = asyncio.run(run())

    assert proxy.drops == [DESAYUNOS[0]]
    assert proxy.connects[-1] == DESAYUNOS[0]  # avoid the stuck one
    assert proxy.active_mac == FUEGOS[0]
    assert _available(result) == {True}
    assert set(c.availability.values()) == {True}
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "open but silent" in messages
    assert "restored via Cocina fuegos" in messages


def test_hung_write_is_a_failed_probe_not_a_hung_heartbeat(clock):
    """The write never completes: the heartbeat must still finish."""

    async def run():
        c, proxy = await _started()
        proxy.behaviour[DESAYUNOS[0]] = "hung"
        clock.now += 200
        result = await asyncio.wait_for(c._async_update_data(), 5)
        await c.async_shutdown()
        return proxy, result

    proxy, result = asyncio.run(run())

    assert proxy.drops == [DESAYUNOS[0]]
    assert proxy.active_mac == FUEGOS[0]
    assert _available(result) == {True}


def test_lights_unavailable_while_no_proxy_answers(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        result = await c._async_update_data()
        await c.async_shutdown()
        return c, result

    c, result = asyncio.run(run())

    assert _available(result) == {False}
    assert set(c.availability.values()) == {False}
    # Entities were told before the (slow) reconnect, not only at the end.
    assert _available(c.pushed[0]) == {False}
    assert c.seen[0] == {False}


def test_link_stays_unavailable_on_next_heartbeat_until_a_light_answers(clock):
    """A fresh reconnect that no light answers is not proof of a working link."""

    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        await c._async_update_data()
        clock.now += 30  # well inside LINK_STALE_AFTER of the reconnect
        second = await c._async_update_data()
        # The lights answer the periodic poll again.
        proxy.behaviour[proxy.active_mac] = "ok"
        await _poll(c)
        after_reply = dict(c.availability)
        await c.async_shutdown()
        return c, second, after_reply

    c, second, after_reply = asyncio.run(run())

    assert _available(second) == {False}
    assert set(after_reply.values()) == {True}
    assert _available(c.pushed[-1]) == {True}
    assert c.seen[-1] == {True}


# ---------------------------------------------------------------------------
# No false alarms
# ---------------------------------------------------------------------------

def test_link_answering_the_poll_is_never_probed(clock):
    async def run():
        c, proxy = await _started()
        for _ in range(10):
            clock.now += 60
            await _poll(c)
            await c._async_update_data()
        await c.async_shutdown()
        return proxy

    proxy = asyncio.run(run())

    assert len(proxy.gets) == 10 * len(LIGHTS)  # the polls, no probe
    assert proxy.drops == []


def test_idle_link_that_answers_the_probe_is_kept(clock):
    async def run():
        c, proxy = await _started()
        clock.now += 200
        result = await c._async_update_data()
        await c.async_shutdown()
        return proxy, result

    proxy, result = asyncio.run(run())

    assert proxy.gets == [LIGHTS[0]]
    assert proxy.drops == []
    assert _available(result) == {True}


def test_one_light_off_at_the_wall_does_not_drop_the_link(clock):
    async def run():
        c, proxy = await _started()

        async def first_light_dead(dst):
            proxy.gets.append(dst)
            if dst != LIGHTS[0]:
                asyncio.get_running_loop().call_soon(
                    c._dispatch_status, dst, OP_ONOFF_STATUS, b"\x01",
                )

        proxy.get_onoff = first_light_dead
        clock.now += 200
        result = await c._async_update_data()
        await c.async_shutdown()
        return proxy, result

    proxy, result = asyncio.run(run())

    assert proxy.gets == [LIGHTS[0], LIGHTS[1]]
    assert proxy.drops == []
    assert _available(result) == {True}


def test_remote_frames_do_not_count_as_a_working_link(clock):
    """Hearing a remote proves we receive, not that the mesh hears us."""

    async def run():
        c, proxy = await _started()
        proxy.behaviour[DESAYUNOS[0]] = "silent"
        clock.now += 200
        c._dispatch_status(0x0100, 0x8202, b"\x01\x05")  # not a light
        await c._async_update_data()
        await c.async_shutdown()
        return proxy

    proxy = asyncio.run(run())

    assert proxy.drops == [DESAYUNOS[0]]


# ---------------------------------------------------------------------------
# Backoff and notification
# ---------------------------------------------------------------------------

def test_failed_forced_reconnects_back_off(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        drops = []
        for step in (200, 30, 31, 60, 60):
            clock.now += step
            await c._async_update_data()
            drops.append(len(proxy.drops))
        await c.async_shutdown()
        return drops

    # Forced reconnect at once, then after 60 s, then after 120 s.
    assert asyncio.run(run()) == [1, 1, 2, 2, 3]


def test_repeated_stuck_links_raise_one_persistent_notification(
    clock, notifications,
):
    async def run():
        c, proxy = await _started()
        for _ in range(4):
            # Whichever proxy we are on gets stuck again.
            proxy.behaviour[proxy.active_mac] = "silent"
            other = FUEGOS[0] if proxy.active_mac == DESAYUNOS[0] else DESAYUNOS[0]
            proxy.behaviour[other] = "ok"
            clock.now += 200
            await c._async_update_data()
        health = c.link_health()
        await c.async_shutdown()
        return health

    health = asyncio.run(run())

    assert len(notifications) == 1
    assert notifications[0]["id"] == "haefele_mesh_e1_link"
    assert "3 times" in notifications[0]["message"]
    assert health["forced_reconnects_total"] == 4
    assert health["suspect"] is False


# ---------------------------------------------------------------------------
# MeshProxyConnection: candidate order and forced drop
# ---------------------------------------------------------------------------

async def _noop_seq(_src: int) -> int:
    return 1


def _real_proxy() -> MeshProxyConnection:
    session = MeshSession(
        net_key_hex=NET_KEY_HEX, app_key_hex=APP_KEY_HEX,
        src_address=0x00C8, iv_index=1, seq_provider=_noop_seq,
    )
    return MeshProxyConnection(hass=SimpleNamespace(), session=session)


def _advert(mac, network_id):
    uuid = "00001828-0000-1000-8000-00805f9b34fb"
    return SimpleNamespace(
        address=mac, name=None, service_uuids=[uuid],
        service_data={uuid: bytes([gatt_mod.PROXY_AD_TYPE_NETWORK_ID]) + network_id},
        device=SimpleNamespace(address=mac, name=None),
    )


def _attempts(monkeypatch, proxy, adverts, works=lambda mac: True):
    monkeypatch.setattr(
        gatt_mod.bluetooth, "async_discovered_service_info",
        lambda _h, connectable=True: adverts, raising=False,
    )
    attempts: list[str] = []

    class Client:
        is_connected = True

        async def disconnect(self):
            self.is_connected = False

    async def try_connect(device, name, timeout):
        attempts.append(device.address)
        if not works(device.address):
            return False
        proxy._client = Client()
        proxy._active_mac = device.address
        proxy._active_name = name
        return True

    monkeypatch.setattr(proxy, "_try_connect_device", try_connect)
    return attempts


def test_connect_any_tries_stored_candidates_in_stored_order(monkeypatch):
    proxy = _real_proxy()
    nid = proxy._session.network_id
    proxy.set_candidates([FUEGOS, DESAYUNOS])
    # HA lists the adverts in another order.
    attempts = _attempts(
        monkeypatch, proxy, [_advert(DESAYUNOS[0], nid), _advert(FUEGOS[0], nid)],
    )

    assert asyncio.run(proxy.connect_any()) is True
    assert attempts == [FUEGOS[0]]


def test_connect_any_tries_the_avoided_proxy_last(monkeypatch):
    proxy = _real_proxy()
    nid = proxy._session.network_id
    proxy.set_candidates([DESAYUNOS, FUEGOS])
    attempts = _attempts(
        monkeypatch, proxy, [_advert(DESAYUNOS[0], nid), _advert(FUEGOS[0], nid)],
        works=lambda mac: mac == DESAYUNOS[0],
    )

    assert asyncio.run(proxy.connect_any(avoid=DESAYUNOS[0].lower())) is True
    # The other one first; the avoided one still used when nothing else works.
    assert attempts == [FUEGOS[0], DESAYUNOS[0]]


def test_drop_link_closes_an_open_link_without_auto_reconnect():
    proxy = _real_proxy()
    calls: list[str] = []

    class Client:
        is_connected = True

        async def disconnect(self):
            calls.append("disconnect")
            proxy._on_disconnected(self)  # bleak fires the callback

    proxy._client = Client()
    proxy._active_mac = DESAYUNOS[0]
    proxy._active_name = DESAYUNOS[1]

    assert asyncio.run(proxy.drop_link()) == DESAYUNOS[0]
    assert calls == ["disconnect"]
    assert proxy.is_connected is False
    assert proxy._reconnect_task is None


def test_drop_link_gives_up_on_a_hung_disconnect(caplog):
    proxy = _real_proxy()

    class Client:
        is_connected = True

        async def disconnect(self):
            await asyncio.Event().wait()

    proxy._client = Client()
    proxy._active_mac = DESAYUNOS[0]
    proxy._active_name = DESAYUNOS[1]

    async def run():
        return await asyncio.wait_for(proxy.drop_link(timeout=0.05), 5)

    assert asyncio.run(run()) == DESAYUNOS[0]
    assert proxy.is_connected is False
    assert proxy._user_disconnecting is False
    assert "did not confirm the disconnect" in caplog.text


# ---------------------------------------------------------------------------
# Review round 1: unload, stale callbacks, replaced links, backoff
# ---------------------------------------------------------------------------

def test_unload_during_forced_reconnect_leaves_no_link(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour[DESAYUNOS[0]] = "silent"
        proxy.connect_delay = 0.3  # a BLE connect takes time
        clock.now += 200
        heartbeat = asyncio.create_task(c._async_update_data())
        await asyncio.sleep(0.25)  # probe done, now inside connect_any
        assert proxy.drops == [DESAYUNOS[0]]
        await c.async_shutdown()  # entry unloaded
        await heartbeat
        return proxy

    proxy = asyncio.run(run())

    assert proxy.is_connected is False


def test_proxy_unloaded_while_connecting_closes_the_new_link(monkeypatch):
    proxy = _real_proxy()
    proxy.set_candidates([FUEGOS])
    monkeypatch.setattr(
        gatt_mod.bluetooth, "async_discovered_service_info",
        lambda _h, connectable=True: [_advert(FUEGOS[0], proxy._session.network_id)],
        raising=False,
    )
    closed: list[str] = []
    connected = asyncio.Event()
    release = asyncio.Event()

    class Client:
        is_connected = True

        async def disconnect(self):
            closed.append("new link")
            self.is_connected = False

    async def try_connect(device, name, timeout):
        connected.set()
        await release.wait()
        proxy._client = Client()
        proxy._active_mac = device.address
        return True

    monkeypatch.setattr(proxy, "_try_connect_device", try_connect)

    async def run():
        connecting = asyncio.create_task(proxy.connect_any())
        await connected.wait()
        await proxy.disconnect()  # integration unload
        release.set()
        first = await connecting
        again = await proxy.connect_any()
        return first, again

    first, again = asyncio.run(run())

    assert (first, again) == (False, False)
    assert closed == ["new link"]
    assert proxy.is_connected is False


def test_late_disconnect_of_a_dropped_link_leaves_the_new_link_alone():
    proxy = _real_proxy()
    scheduled: list[str] = []
    proxy._hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: (coro.close(), scheduled.append(name)),
    )

    class Hung:
        is_connected = True

        async def disconnect(self):
            await asyncio.Event().wait()

    old = Hung()
    proxy._client = old
    proxy._active_mac = DESAYUNOS[0]

    async def run():
        await proxy.drop_link(timeout=0.05)
        proxy._client = SimpleNamespace(is_connected=True)  # new link
        proxy._active_mac = FUEGOS[0]
        proxy.filter_mode = "forward_all"
        proxy._on_disconnected(old)  # BlueZ finally reports the old one

    asyncio.run(run())

    assert proxy.filter_mode == "forward_all"
    assert scheduled == []


def test_drop_link_leaves_a_link_that_replaced_the_stuck_one():
    proxy = _real_proxy()
    closed: list[str] = []

    class Client:
        is_connected = True

        async def disconnect(self):
            closed.append("dropped")

    proxy._client = Client()
    proxy._active_mac = FUEGOS[0]
    proxy.link_generation = 2  # auto-reconnect replaced link 1

    dropped = asyncio.run(proxy.drop_link(expected_generation=1))

    assert dropped is None
    assert closed == []
    assert proxy.is_connected is True


def test_watchdog_keeps_a_link_that_auto_reconnect_brought_up_meanwhile(
    clock, caplog,
):
    caplog.set_level(logging.WARNING, logger=coord_mod.__name__)
    result_health: list[dict] = []

    async def run():
        c, proxy = await _started()
        sent: list[int] = []

        async def get_onoff(dst):
            sent.append(dst)
            if len(sent) == 1:
                # The stuck link really drops; auto-reconnect brings up a
                # new one while the probe is still waiting for replies.
                proxy.active_mac, proxy.active_name = FUEGOS
                proxy.link_generation += 1
            if len(sent) > len(LIGHTS):  # the new link works
                asyncio.get_running_loop().call_soon(
                    c._dispatch_status, dst, OP_ONOFF_STATUS, b"\x01",
                )

        proxy.get_onoff = get_onoff
        clock.now += 200
        result = await c._async_update_data()
        result_health.append(c.link_health())
        result_health.append(list(sent))
        await c.async_shutdown()
        return proxy, result

    proxy, result = asyncio.run(run())

    # The probe stopped with the link it was judging; the new link gets
    # its grace period.
    assert result_health[1] == [LIGHTS[0]]
    assert proxy.drops == []
    assert proxy.active_mac == FUEGOS[0]
    assert _available(result) == {True}
    health = result_health[0]
    assert health["forced_reconnects_total"] == 0
    assert "Dropping it" not in caplog.text


def test_backoff_and_gate_reset_when_a_light_answers(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        for _ in range(3):  # backoff 60, 120, 240
            clock.now += 300
            await c._async_update_data()
        assert c.link_health()["backoff_seconds"] == 240
        # The mesh comes back: the lights answer the poll.
        proxy.behaviour[proxy.active_mac] = "ok"
        await _poll(c)
        health = c.link_health()
        # 100 s later (inside the old 240 s gate) the link sticks again.
        proxy.behaviour[proxy.active_mac] = "silent"
        clock.now += 100
        drops = len(proxy.drops)
        await c._async_update_data()
        await c.async_shutdown()
        return health, len(proxy.drops) - drops

    health, new_drops = asyncio.run(run())

    assert health["backoff_seconds"] == 0
    assert new_drops == 1


def test_backoff_is_capped(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        for _ in range(8):
            clock.now += 2000
            await c._async_update_data()
        health = c.link_health()
        await c.async_shutdown()
        return health

    assert asyncio.run(run())["backoff_seconds"] == 900


def test_idle_time_counts_from_the_last_link_up(clock):
    """A link that just came up is not judged by the previous link's silence."""

    async def run():
        c, proxy = await _started()
        c._last_light_rx = clock.now - 1000
        c._link_up_at = clock.now - 30
        await c._async_update_data()
        await c.async_shutdown()
        return proxy

    assert asyncio.run(run()).gets == []


def test_probe_needs_a_reply_from_the_light_it_asked(clock):
    """A late reply from another light (e.g. to an old poll) proves nothing."""

    async def run():
        c, proxy = await _started()

        async def other_light_answers(dst):
            proxy.gets.append(dst)
            if proxy.active_mac == DESAYUNOS[0]:
                other = LIGHTS[(LIGHTS.index(dst) + 1) % len(LIGHTS)]
                asyncio.get_running_loop().call_soon(
                    c._dispatch_status, other, OP_ONOFF_STATUS, b"\x01",
                )
            else:
                asyncio.get_running_loop().call_soon(
                    c._dispatch_status, dst, OP_ONOFF_STATUS, b"\x01",
                )

        proxy.get_onoff = other_light_answers
        clock.now += 200
        await c._async_update_data()
        await c.async_shutdown()
        return proxy

    assert asyncio.run(run()).drops == [DESAYUNOS[0]]


def test_old_forced_reconnects_leave_the_flap_window(clock, notifications):
    async def run():
        c, proxy = await _started()
        for _ in range(4):
            proxy.behaviour[proxy.active_mac] = "silent"
            other = FUEGOS[0] if proxy.active_mac == DESAYUNOS[0] else DESAYUNOS[0]
            proxy.behaviour[other] = "ok"
            clock.now += 1000  # one stuck link every ~17 min
            await c._async_update_data()
        health = c.link_health()
        await c.async_shutdown()
        return health

    health = asyncio.run(run())

    assert notifications == []
    assert health["forced_reconnects_total"] == 4
    assert health["forced_reconnects_recent"] == 2


def test_drop_link_waits_for_a_connect_in_progress():
    proxy = _real_proxy()
    closed: list[str] = []

    class Client:
        is_connected = True

        async def disconnect(self):
            closed.append("dropped")

    proxy._client = Client()
    proxy._active_mac = DESAYUNOS[0]

    async def run():
        async with proxy._connect_lock:  # a connect_any is running
            dropping = asyncio.create_task(proxy.drop_link())
            await asyncio.sleep(0.05)
            during = list(closed)
        await dropping
        return during

    during = asyncio.run(run())

    assert during == []
    assert closed == ["dropped"]


def test_drop_link_discards_a_half_received_frame():
    proxy = _real_proxy()

    class Client:
        is_connected = True

        async def disconnect(self):
            pass

    proxy._client = Client()
    proxy._reassembly_buf = bytearray(b"\x01\x02\x03")

    asyncio.run(proxy.drop_link())

    assert proxy._reassembly_buf == bytearray()


def test_diagnostics_include_link_health(clock, monkeypatch):
    import importlib

    diag_stub = types.ModuleType("homeassistant.components.diagnostics")
    diag_stub.async_redact_data = lambda data, keys: data
    monkeypatch.setitem(sys.modules, "homeassistant.components.diagnostics", diag_stub)
    entries = sys.modules.get("homeassistant.config_entries")
    if entries is None:
        entries = types.ModuleType("homeassistant.config_entries")
        monkeypatch.setitem(sys.modules, "homeassistant.config_entries", entries)
    monkeypatch.setattr(entries, "ConfigEntry", object, raising=False)
    monkeypatch.delitem(
        sys.modules, "custom_components.haefele_mesh.diagnostics", raising=False,
    )
    diagnostics = importlib.import_module("custom_components.haefele_mesh.diagnostics")

    async def run():
        c, proxy = await _started()
        proxy._candidates = []
        proxy._filter_addresses = []
        clock.now += 20
        await _poll(c)
        clock.now += 5
        snapshot = diagnostics._coordinator_snapshot(c)
        await c.async_shutdown()
        return snapshot

    health = asyncio.run(run())["link_health"]

    assert health["seconds_since_light_reply"] == 5.0
    assert health["seconds_since_link_up"] == 25.0
    assert health["suspect"] is False


# ---------------------------------------------------------------------------
# Review round 2: half-open links, unsolicited frames, link identity
# ---------------------------------------------------------------------------

def _validating_proxy(monkeypatch):
    """Real proxy whose BLE connect succeeds; beacon and filter are up to the test."""
    proxy = _real_proxy()
    closed: list[str] = []

    class Client:
        is_connected = True

        def __init__(self):
            self.services = SimpleNamespace(get_characteristic=lambda uuid: object())

        async def start_notify(self, *a, **k):
            pass

        async def disconnect(self):
            closed.append("disconnect")
            self.is_connected = False

    async def establish(*a, **k):
        return Client()

    async def no_filter():
        pass

    monkeypatch.setattr(gatt_mod, "establish_connection", establish)
    monkeypatch.setattr(proxy, "_setup_proxy_filter", no_filter)
    return proxy, closed


def test_each_validated_link_gets_a_new_generation(monkeypatch):
    proxy, _ = _validating_proxy(monkeypatch)
    device = SimpleNamespace(address=FUEGOS[0])

    async def connect_once():
        task = asyncio.create_task(proxy._try_connect_device(device, "x", 15))
        await asyncio.sleep(0.05)
        proxy._beacon_event.set()  # the proxy sends its Secure Network Beacon
        ok = await task
        await proxy._stop_rx_pump()
        return ok

    async def run():
        first = await connect_once()
        proxy._client = None
        second = await connect_once()
        return first, second

    assert asyncio.run(run()) == (True, True)
    assert proxy.link_generation == 2


def test_connect_cancelled_before_the_beacon_leaves_no_link(monkeypatch):
    """E.g. a caller's timeout fires while the new link waits for its beacon."""
    proxy, closed = _validating_proxy(monkeypatch)

    async def run():
        task = asyncio.create_task(
            proxy._try_connect_device(SimpleNamespace(address=FUEGOS[0]), "x", 15),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await proxy._stop_rx_pump()

    asyncio.run(run())

    assert proxy.is_connected is False
    assert proxy._client is None
    assert proxy.active_mac is None
    assert proxy.link_generation == 0
    assert closed == ["disconnect"]


def test_probe_stops_when_the_link_goes_away(clock):
    """A Get on a dropped link would reconnect, and the probe timeout would
    cancel that connect half-way."""

    async def run():
        c, proxy = await _started()

        async def link_drops(dst):
            proxy.gets.append(dst)
            proxy.is_connected = False  # real drop while probing

        proxy.get_onoff = link_drops
        clock.now += 200
        ok = await c._probe_link()
        await c.async_shutdown()
        return ok, proxy

    ok, proxy = asyncio.run(run())

    assert ok is False
    assert proxy.gets == [LIGHTS[0]]


def test_late_disconnect_after_a_drop_schedules_no_reconnect():
    proxy = _real_proxy()
    scheduled: list[str] = []
    proxy._hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: (coro.close(), scheduled.append(name)),
    )

    class Hung:
        is_connected = True

        async def disconnect(self):
            await asyncio.Event().wait()

    old = Hung()
    proxy._client = old

    async def run():
        await proxy.drop_link(timeout=0.05)
        proxy._on_disconnected(old)  # no new link yet

    asyncio.run(run())

    assert scheduled == []


def test_real_drop_of_the_current_link_still_reconnects():
    proxy = _real_proxy()
    scheduled: list[str] = []
    proxy._hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: (coro.close(), scheduled.append(name)),
    )
    current = SimpleNamespace(is_connected=False)
    proxy._client = current

    proxy._on_disconnected(current)

    assert scheduled == ["haefele-auto-reconnect"]


def test_unload_during_connect_stops_the_rx_pump(monkeypatch):
    proxy = _real_proxy()
    proxy.set_candidates([FUEGOS])
    monkeypatch.setattr(
        gatt_mod.bluetooth, "async_discovered_service_info",
        lambda _h, connectable=True: [_advert(FUEGOS[0], proxy._session.network_id)],
        raising=False,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def try_connect(device, name, timeout):
        started.set()
        await release.wait()  # BLE connect in progress
        # Like _validate_proxy: the link is up and starts its rx pump.
        proxy._client = SimpleNamespace(is_connected=True, disconnect=_async_noop)
        proxy._rx_task = asyncio.create_task(asyncio.Event().wait())
        return True

    monkeypatch.setattr(proxy, "_try_connect_device", try_connect)

    async def run():
        connecting = asyncio.create_task(proxy.connect_any())
        await started.wait()
        await proxy.disconnect()
        release.set()
        await connecting
        return proxy._rx_task

    assert asyncio.run(run()) is None


async def _async_noop(*a, **k):
    return None


def test_unsolicited_light_status_is_not_proof_of_a_working_link(clock):
    """A wall switch or the Häfele app makes a light publish its state."""

    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 80
        c._dispatch_status(LIGHTS[0], OP_ONOFF_STATUS, b"\x01")  # nobody asked
        clock.now += 20
        await c._async_update_data()  # silent for 100 s: probed, dropped
        clock.now += 11  # well after the probe's Gets
        c._dispatch_status(LIGHTS[1], OP_ONOFF_STATUS, b"\x01")
        still_unavailable = set(c.availability.values())
        await c.async_shutdown()
        return proxy, still_unavailable

    proxy, still_unavailable = asyncio.run(run())

    assert proxy.drops == [DESAYUNOS[0]]
    assert still_unavailable == {False}


def test_a_reply_long_after_the_get_does_not_count(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour[DESAYUNOS[0]] = "silent"
        await _poll(c)  # Gets go out, nothing comes back
        clock.now += 11  # past LINK_REPLY_WINDOW
        c._dispatch_status(LIGHTS[0], OP_ONOFF_STATUS, b"\x01")
        health = c.link_health()
        await c.async_shutdown()
        return health

    assert asyncio.run(run())["seconds_since_light_reply"] is None


def test_link_opened_outside_the_watchdog_is_not_judged_by_the_old_one(clock):
    """E.g. a light command found the link down and reconnected it."""

    async def run():
        c, proxy = await _started()
        await c._async_update_data()  # link 1 seen
        clock.now += 1000  # link 1 silent for long
        proxy.link_generation += 1  # link 2 opened by ensure_connected()
        before = len(proxy.gets)
        await c._async_update_data()
        await c.async_shutdown()
        return len(proxy.gets) - before

    assert asyncio.run(run()) == 0


def test_auto_reconnect_keeps_unproven_lights_unavailable(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        await c._async_update_data()  # suspect
        proxy.link_generation += 1  # real drop + auto-reconnect
        await c._on_proxy_reconnect()
        after = set(c.availability.values())
        await c.async_shutdown()
        return after

    assert asyncio.run(run()) == {False}


def test_notification_rearms_after_the_window(clock, notifications):
    async def run():
        c, proxy = await _started()

        async def stuck_burst():
            for _ in range(3):
                proxy.behaviour[proxy.active_mac] = "silent"
                other = FUEGOS[0] if proxy.active_mac == DESAYUNOS[0] else DESAYUNOS[0]
                proxy.behaviour[other] = "ok"
                clock.now += 200
                await c._async_update_data()

        await stuck_burst()
        clock.now += 3600  # calm hour: window drains
        await stuck_burst()
        await c.async_shutdown()

    asyncio.run(run())

    assert len(notifications) == 2


def test_backoff_gate_tolerates_heartbeat_jitter(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        await c._async_update_data()  # forced reconnect 1, backoff 60
        clock.now += 59.5  # next heartbeat, a little early
        await c._async_update_data()
        await c.async_shutdown()
        return proxy

    assert len(asyncio.run(run()).drops) == 2


def test_probe_asks_at_most_three_lights(clock):
    nodes = [
        {"name": f"L{i}", "mac": f"AA:BB:CC:DD:EE:{i:02X}", "unicast": 0x0100 + i,
         "groups": [], "device_type": "onoff"}
        for i in range(5)
    ]

    async def run():
        c = Coordinator(object(), _config(nodes=nodes), entry_id="e1")
        await c.async_setup()
        proxy = MeshProxy.last
        proxy.behaviour[proxy.active_mac] = "silent"
        clock.now += 200
        ok = await c._probe_link()
        await c.async_shutdown()
        return ok, proxy

    ok, proxy = asyncio.run(run())

    assert ok is False
    assert proxy.gets == [0x0100, 0x0101, 0x0102]


def test_install_without_light_addresses_is_never_dropped(clock):
    nodes = [{"name": "L", "mac": "AA:BB:CC:DD:EE:01", "groups": []}]

    async def run():
        c = Coordinator(object(), _config(nodes=nodes), entry_id="e1")
        await c.async_setup()
        proxy = MeshProxy.last
        clock.now += 200
        result = await c._async_update_data()
        await c.async_shutdown()
        return proxy, result

    proxy, result = asyncio.run(run())

    assert proxy.drops == []
    assert _available(result) == {True}


def test_unload_during_heartbeat_reconnect_reports_unavailable(clock):
    async def run():
        c, proxy = await _started()
        proxy.is_connected = False
        proxy.connect_delay = 0.2
        heartbeat = asyncio.create_task(c._async_update_data())
        await asyncio.sleep(0.1)
        await c.async_shutdown()
        return await heartbeat

    assert _available(asyncio.run(run())) == {False}


def test_link_health_reports_a_suspect_link(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        await c._async_update_data()
        health = c.link_health()
        await c.async_shutdown()
        return health

    assert asyncio.run(run())["suspect"] is True


def test_heartbeat_reconnect_keeps_unproven_lights_unavailable(clock):
    async def run():
        c, proxy = await _started()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        clock.now += 200
        await c._async_update_data()  # suspect
        proxy.is_connected = False  # the link then drops for real
        clock.now += 60
        result = await c._async_update_data()  # heartbeat reconnects
        await c.async_shutdown()
        return result

    assert _available(asyncio.run(run())) == {False}


def test_reconnected_link_is_probed_90_s_after_it_came_up(clock):
    """Its grace period starts when it came up, not when first noticed."""

    async def run():
        c, proxy = await _started()
        probes = []
        # Heartbeat reconnect.
        proxy.is_connected = False
        await c._async_update_data()
        proxy.behaviour[proxy.active_mac] = "silent"
        clock.now += 100
        before = len(proxy.gets)
        await c._async_update_data()
        probes.append(len(proxy.gets) - before)
        # Auto-reconnect after a real drop.
        proxy.link_generation += 1
        await c._on_proxy_reconnect()
        proxy.behaviour = {DESAYUNOS[0]: "silent", FUEGOS[0]: "silent"}
        c._link_suspect = False
        c._next_forced_reconnect_at = 0.0
        clock.now += 100
        before = len(proxy.gets)
        await c._async_update_data()
        probes.append(len(proxy.gets) - before)
        await c.async_shutdown()
        return probes

    probes = asyncio.run(run())

    assert all(p > 0 for p in probes), probes


# ---------------------------------------------------------------------------
# Review round 3
# ---------------------------------------------------------------------------

def test_command_reconnecting_during_a_drop_avoids_the_stuck_proxy(monkeypatch):
    """A light command can reconnect before the watchdog does."""
    proxy = _real_proxy()
    proxy.set_candidates([DESAYUNOS, FUEGOS])
    nid = proxy._session.network_id
    monkeypatch.setattr(
        gatt_mod.bluetooth, "async_discovered_service_info",
        lambda _h, connectable=True: [_advert(DESAYUNOS[0], nid), _advert(FUEGOS[0], nid)],
        raising=False,
    )
    attempts: list[str] = []

    class Client:
        is_connected = True

        async def disconnect(self):
            self.is_connected = False  # BlueZ reports the drop first...
            await asyncio.sleep(0.2)  # ...while bleak still cleans up

    async def try_connect(device, name, timeout):
        attempts.append(device.address)
        proxy._client = Client()
        proxy._active_mac = device.address
        proxy.link_generation += 1
        return True

    monkeypatch.setattr(proxy, "_try_connect_device", try_connect)

    async def run():
        await proxy.connect_any()
        attempts.clear()
        stuck = proxy.active_mac

        async def watchdog():
            await proxy.drop_link(timeout=10)
            await proxy.connect_any(avoid=stuck)

        async def light_command():
            await asyncio.sleep(0.05)
            await proxy.ensure_connected()

        await asyncio.gather(watchdog(), light_command())

    asyncio.run(run())

    assert attempts == [FUEGOS[0]]


def test_avoidance_ends_once_a_link_is_up(monkeypatch):
    proxy = _real_proxy()
    proxy.set_candidates([DESAYUNOS, FUEGOS])
    attempts = _attempts(
        monkeypatch, proxy,
        [_advert(DESAYUNOS[0], proxy._session.network_id),
         _advert(FUEGOS[0], proxy._session.network_id)],
    )

    async def run():
        proxy._client = SimpleNamespace(is_connected=True, disconnect=_async_noop)
        proxy._active_mac = DESAYUNOS[0]
        await proxy.drop_link()
        await proxy.connect_any()  # avoids Luz desayunos
        proxy._client = None
        await proxy.connect_any()  # normal order again (winner first)

    asyncio.run(run())

    assert attempts == [FUEGOS[0], FUEGOS[0]]
    assert proxy._dropped_mac is None


def test_notification_does_not_blame_the_adapter_alone(clock, notifications):
    async def run():
        c, proxy = await _started()
        for _ in range(3):
            proxy.behaviour[proxy.active_mac] = "silent"
            other = FUEGOS[0] if proxy.active_mac == DESAYUNOS[0] else DESAYUNOS[0]
            proxy.behaviour[other] = "ok"
            clock.now += 200
            await c._async_update_data()
        await c.async_shutdown()

    asyncio.run(run())

    assert "without power" in notifications[0]["message"]
