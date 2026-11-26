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
        MeshProxy.last = self

    def set_candidates(self, candidates):
        pass

    def set_filter_addresses(self, addresses):
        pass

    async def connect_any(self, timeout_per_candidate=15.0, avoid=None):
        self.connects.append(avoid)
        if self.is_connected:
            return True
        order = [c for c in self.order if c[0] != avoid] + [
            c for c in self.order if c[0] == avoid
        ]
        if not order:
            return False
        self.active_mac, self.active_name = order[0]
        self.is_connected = True
        return True

    async def drop_link(self, timeout=10.0):
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
        # A light speaks (e.g. answer to the periodic poll).
        c._dispatch_status(LIGHTS[1], OP_ONOFF_STATUS, b"\x00")
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
            c._dispatch_status(LIGHTS[0], OP_ONOFF_STATUS, b"\x01")
            await c._async_update_data()
        await c.async_shutdown()
        return proxy

    proxy = asyncio.run(run())

    assert proxy.gets == []
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
