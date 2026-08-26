"""
Tests for the "no connectable Bluetooth transport" guard.

Scanner-only gateways (Shelly BLE gateways, BTHome bridges, passive
ESPHome proxies) hand advertisements to Home Assistant but never open
outbound GATT connections, so the mesh proxy link can't be established.
Before the guard this surfaced as the generic "no proxy visible"
warning, which reads like a range problem (issue #7).

The guard must:
  * short-circuit ``connect_any`` without even attempting discovery,
  * say so once at ERROR level and stay quiet afterwards,
  * stay out of the way when a connectable adapter/proxy does exist,
  * degrade to a no-op if the running HA core lacks the counting API.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from homeassistant.components import bluetooth

from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession

NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"

GATT_LOGGER = "custom_components.haefele_mesh.gatt"


async def _noop_seq(_src: int) -> int:
    return 1


def _make_proxy() -> MeshProxyConnection:
    return MeshProxyConnection(
        hass=SimpleNamespace(),  # never touched: every BLE call is stubbed
        session=MeshSession(
            net_key_hex=NET_KEY_HEX,
            app_key_hex=APP_KEY_HEX,
            src_address=0x7FFD,
            iv_index=1,
            seq_provider=_noop_seq,
        ),
        message_handler=None,
    )


def _patch_scanner_count(monkeypatch, n_connectable: int, n_total: int) -> None:
    """Make HA report a given scanner inventory."""

    def _count(_hass, connectable=True):
        return n_connectable if connectable else n_total

    monkeypatch.setattr(bluetooth, "async_scanner_count", _count)


def _patch_discovery(proxy: MeshProxyConnection) -> list[str]:
    """Record discovery attempts; always yields one unusable candidate.

    Returning a candidate (rather than nothing) keeps ``connect_any`` off
    its 5 s "wait for adverts" path, so the tests stay fast.
    """
    calls: list[str] = []

    def _discover():
        calls.append("discover")
        return [(SimpleNamespace(address="AA:AA:AA:AA:AA:AA", name=None), "cand")]

    async def _try_connect_device(_device, _name, timeout=15.0):
        return False

    proxy._discover_proxy_candidates = _discover  # type: ignore[assignment]
    proxy._try_connect_device = _try_connect_device  # type: ignore[assignment]
    return calls


def test_connect_any_aborts_before_discovery_when_nothing_is_connectable(monkeypatch):
    """A Shelly-only setup: adverts arrive, connections are impossible."""
    p = _make_proxy()
    _patch_scanner_count(monkeypatch, n_connectable=0, n_total=1)
    calls = _patch_discovery(p)

    assert asyncio.run(p.connect_any()) is False
    assert calls == []  # short-circuited before touching discovery


def test_guard_logs_error_once_then_drops_to_debug(monkeypatch, caplog):
    p = _make_proxy()
    _patch_scanner_count(monkeypatch, n_connectable=0, n_total=1)
    _patch_discovery(p)

    with caplog.at_level(logging.DEBUG, logger=GATT_LOGGER):
        asyncio.run(p.connect_any())
        asyncio.run(p.connect_any())
        asyncio.run(p.connect_any())

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "none connectable" in message
    # The error has to name the actual culprit, not just "no proxy visible".
    assert "Shelly" in message


def test_guard_stays_out_of_the_way_when_a_connectable_scanner_exists(monkeypatch):
    p = _make_proxy()
    _patch_scanner_count(monkeypatch, n_connectable=1, n_total=2)
    calls = _patch_discovery(p)

    assert asyncio.run(p.connect_any()) is False  # candidate found but unusable
    assert calls == ["discover"]


def test_guard_rearms_once_a_connectable_scanner_comes_back(monkeypatch):
    """An ESPHome proxy that drops off and returns must be able to warn again."""
    p = _make_proxy()
    _patch_discovery(p)

    _patch_scanner_count(monkeypatch, n_connectable=0, n_total=1)
    asyncio.run(p.connect_any())
    assert p._no_transport_warned is True

    _patch_scanner_count(monkeypatch, n_connectable=1, n_total=2)
    asyncio.run(p.connect_any())
    assert p._no_transport_warned is False


def test_guard_is_a_noop_when_core_lacks_the_counting_api(monkeypatch):
    """Older HA cores: fall through to normal discovery rather than block."""
    p = _make_proxy()
    calls = _patch_discovery(p)

    def _boom(_hass, connectable=True):
        raise AttributeError("async_scanner_count")

    monkeypatch.setattr(bluetooth, "async_scanner_count", _boom)

    assert asyncio.run(p.connect_any()) is False
    assert calls == ["discover"]
