"""
Tests for MeshProxyConnection's candidate selection + reorder-on-success
behaviour.

We don't open real GATT links here — `_try_connect` is monkeypatched
so each test drives a deterministic sequence of "reachable / not
reachable" outcomes. The actual BLE path is exercised via integration
testing against real Häfele hardware (see `_pending/live-checklist.md`).
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
    these tests because `_try_connect` is patched)."""
    return MeshProxyConnection(
        hass=SimpleNamespace(),  # unused once _try_connect is patched
        session=_make_session(),
        message_handler=None,
    )


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
# connect_any: iteration order + winner promotion
# ---------------------------------------------------------------------------

def _patch_try_connect(proxy: MeshProxyConnection, successful_mac: str | None):
    """Install a fake `_try_connect` that flips the connection state on
    for exactly one MAC (or never, if successful_mac is None) and records
    every attempt."""
    attempts: list[str] = []

    # Fake "connected" client — just needs a truthy is_connected attr.
    class _FakeClient:
        is_connected = True

        async def disconnect(self):
            type(self).is_connected = False

    async def _fake_try(mac: str, name: str, timeout: float) -> bool:
        attempts.append(mac)
        if successful_mac is not None and mac == successful_mac:
            proxy._client = _FakeClient()
            proxy._active_mac = mac
            proxy._active_name = name
            return True
        return False

    proxy._try_connect = _fake_try  # type: ignore[assignment]
    return attempts


def test_connect_any_tries_candidates_in_given_order_until_one_succeeds():
    p = _make_proxy()
    p.set_candidates([
        ("AA:AA:AA:AA:AA:AA", "first"),
        ("BB:BB:BB:BB:BB:BB", "second"),
        ("CC:CC:CC:CC:CC:CC", "third"),
    ])
    attempts = _patch_try_connect(p, successful_mac="BB:BB:BB:BB:BB:BB")

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
    attempts = _patch_try_connect(p, successful_mac=None)

    ok = asyncio.run(p.connect_any())

    assert ok is False
    # Every candidate was tried exactly once.
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
    _patch_try_connect(p, successful_mac="BB:BB:BB:BB:BB:BB")

    asyncio.run(p.connect_any())

    # Internal order must now start with the winner.
    assert p._candidates[0][0] == "BB:BB:BB:BB:BB:BB"
    assert p._candidates[-1][0] == "AA:AA:AA:AA:AA:AA"


def test_connect_any_skips_when_already_connected():
    """Second call must early-return True without touching the candidate
    list (no spurious re-probes if an entity calls ensure_connected twice)."""
    p = _make_proxy()
    p.set_candidates([("AA:AA:AA:AA:AA:AA", "only")])
    attempts = _patch_try_connect(p, successful_mac="AA:AA:AA:AA:AA:AA")

    assert asyncio.run(p.connect_any()) is True
    assert attempts == ["AA:AA:AA:AA:AA:AA"]

    # Second call should be a no-op — the fake_try must NOT run again.
    assert asyncio.run(p.connect_any()) is True
    assert attempts == ["AA:AA:AA:AA:AA:AA"]


def test_connect_any_with_empty_candidate_list_returns_false():
    p = _make_proxy()
    p.set_candidates([])
    # Don't patch — no candidates means _try_connect is never called anyway.
    assert asyncio.run(p.connect_any()) is False
