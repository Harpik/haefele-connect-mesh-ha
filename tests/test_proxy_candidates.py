"""
Tests for MeshProxyConnection's discovery + connect-and-promote behaviour.

We don't open real GATT links here — `_discover_proxy_candidates` and
`_try_connect_device` are monkeypatched so each test drives a deterministic
sequence of "discovered / reachable" outcomes. The actual BLE path is
exercised via integration testing against real Häfele hardware (see
`_pending/live-checklist.md`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession


NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"


async def _noop_seq(_src: int) -> int:
    return 1


def _make_session() -> MeshSession:
    return MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex=APP_KEY_HEX,
        src_address=0x7FFD,
        iv_index=1,
        seq_provider=_noop_seq,
    )


def _make_proxy() -> MeshProxyConnection:
    """Build a MeshProxyConnection with a dummy `hass` (never touched by
    these tests because the discovery + connect path is patched)."""
    return MeshProxyConnection(
        hass=SimpleNamespace(),  # unused once internals are patched
        session=_make_session(),
        message_handler=None,
    )


def _fake_device(mac: str):
    """Minimal BLEDevice stand-in exposing the .address attribute the
    production code reads from."""
    return SimpleNamespace(address=mac, name=None)


# ---------------------------------------------------------------------------
# set_candidates
# ---------------------------------------------------------------------------

def test_set_candidates_normalises_mac_to_upper():
    p = _make_proxy()
    p.set_candidates([
        ("aa:bb:cc:dd:ee:ff", "lowercase node"),
        ("11:22:33:44:55:66", "already upper"),
    ])
    macs = [mac for mac, _ in p._candidates]  # internal, ok for a unit test
    assert macs == ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]


def test_set_candidates_replaces_previous_list():
    p = _make_proxy()
    p.set_candidates([("AA:AA:AA:AA:AA:AA", "old")])
    p.set_candidates([("BB:BB:BB:BB:BB:BB", "new")])
    assert [m for m, _ in p._candidates] == ["BB:BB:BB:BB:BB:BB"]


# ---------------------------------------------------------------------------
# connect_any: discovery → iteration order → winner promotion
# ---------------------------------------------------------------------------

def _patch_discovery_and_connect(
    proxy: MeshProxyConnection,
    discovered_macs: list[str],
    successful_mac: str | None,
):
    """Install a fake discovery returning the given MACs (in order) and
    a fake connect that succeeds for at most one of them. Returns the
    list that records every connect attempt."""
    attempts: list[str] = []

    # Fake "connected" client — just needs a truthy is_connected attr.
    class _FakeClient:
        is_connected = True

        async def disconnect(self):
            type(self).is_connected = False

    def _fake_discover() -> list[tuple[object, str]]:
        # Reflect the production ordering hint: stored candidates first,
        # in their stored order; then any other discovered devices.
        candidate_macs = [mac for mac, _ in proxy._candidates]
        candidate_names = {mac: name for mac, name in proxy._candidates}
        preferred = [
            (_fake_device(m), candidate_names.get(m, m))
            for m in candidate_macs if m in discovered_macs
        ]
        others = [
            (_fake_device(m), m)
            for m in discovered_macs if m not in candidate_macs
        ]
        return preferred + others

    async def _fake_try(device, name: str, timeout: float) -> bool:
        mac = device.address
        attempts.append(mac)
        if successful_mac is not None and mac == successful_mac:
            proxy._client = _FakeClient()
            proxy._active_mac = mac
            proxy._active_name = name
            return True
        return False

    proxy._discover_proxy_candidates = _fake_discover  # type: ignore[assignment]
    proxy._try_connect_device = _fake_try  # type: ignore[assignment]
    return attempts


def test_connect_any_tries_candidates_in_given_order_until_one_succeeds():
    p = _make_proxy()
    p.set_candidates([
        ("AA:AA:AA:AA:AA:AA", "first"),
        ("BB:BB:BB:BB:BB:BB", "second"),
        ("CC:CC:CC:CC:CC:CC", "third"),
    ])
    attempts = _patch_discovery_and_connect(
        p,
        discovered_macs=[
            "AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB", "CC:CC:CC:CC:CC:CC",
        ],
        successful_mac="BB:BB:BB:BB:BB:BB",
    )

    ok = asyncio.run(p.connect_any())

    assert ok is True
    # Stopped after the winner, didn't try "third".
    assert attempts == ["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]


def test_connect_any_returns_false_when_no_candidate_works():
    p = _make_proxy()
    p.set_candidates([
        ("AA:AA:AA:AA:AA:AA", "first"),
        ("BB:BB:BB:BB:BB:BB", "second"),
    ])
    attempts = _patch_discovery_and_connect(
        p,
        discovered_macs=["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"],
        successful_mac=None,
    )

    ok = asyncio.run(p.connect_any())

    assert ok is False
    # Every discovered candidate was tried exactly once.
    assert attempts == ["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]


def test_connect_any_promotes_winner_to_front_for_next_cycle():
    """The 'smart retry' behaviour: once Luz desayunos is confirmed as a
    working proxy, it must be tried FIRST next time so we skip the
    non-functional Cocina fuegos."""
    p = _make_proxy()
    p.set_candidates([
        ("AA:AA:AA:AA:AA:AA", "cocina-fuegos-lookalike"),
        ("BB:BB:BB:BB:BB:BB", "luz-desayunos-lookalike"),
    ])
    _patch_discovery_and_connect(
        p,
        discovered_macs=["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"],
        successful_mac="BB:BB:BB:BB:BB:BB",
    )

    asyncio.run(p.connect_any())

    # Internal order must now start with the winner.
    assert p._candidates[0][0] == "BB:BB:BB:BB:BB:BB"
    assert p._candidates[-1][0] == "AA:AA:AA:AA:AA:AA"


def test_connect_any_skips_when_already_connected():
    """Second call must early-return True without touching the discovery
    path (no spurious re-probes if an entity calls ensure_connected twice)."""
    p = _make_proxy()
    p.set_candidates([("AA:AA:AA:AA:AA:AA", "only")])
    attempts = _patch_discovery_and_connect(
        p,
        discovered_macs=["AA:AA:AA:AA:AA:AA"],
        successful_mac="AA:AA:AA:AA:AA:AA",
    )

    assert asyncio.run(p.connect_any()) is True
    assert attempts == ["AA:AA:AA:AA:AA:AA"]

    # Second call should be a no-op — the fake_try must NOT run again.
    assert asyncio.run(p.connect_any()) is True
    assert attempts == ["AA:AA:AA:AA:AA:AA"]


def test_connect_any_with_no_discovery_returns_false(monkeypatch):
    """When nothing on our network is currently advertising as a proxy,
    connect_any must give up cleanly (after the brief discovery wait)."""
    # Skip the 5s discovery-retry sleep — we just want to assert the
    # "no candidate ⇒ False" branch.
    from custom_components.haefele_mesh import gatt as gatt_mod

    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(gatt_mod.asyncio, "sleep", _instant_sleep)

    p = _make_proxy()
    p.set_candidates([("AA:AA:AA:AA:AA:AA", "only")])
    attempts = _patch_discovery_and_connect(
        p,
        discovered_macs=[],  # nothing discovered
        successful_mac=None,
    )

    assert asyncio.run(p.connect_any()) is False
    assert attempts == []


def test_connect_any_uses_unstored_discovered_proxy():
    """If the .connect-stored MAC is stale/wrong but a different node on
    our network advertises itself, we must still connect through it."""
    p = _make_proxy()
    p.set_candidates([("AA:AA:AA:AA:AA:AA", "stale-stored")])
    attempts = _patch_discovery_and_connect(
        p,
        discovered_macs=["DD:DD:DD:DD:DD:DD"],  # not in stored list
        successful_mac="DD:DD:DD:DD:DD:DD",
    )

    assert asyncio.run(p.connect_any()) is True
    assert attempts == ["DD:DD:DD:DD:DD:DD"]
