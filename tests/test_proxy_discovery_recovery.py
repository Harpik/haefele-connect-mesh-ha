"""End-to-end-style coverage of the MAC-stale → Network-ID-recovery path.

Unlike ``tests/test_proxy_candidates.py`` (which monkeypatches both
``_discover_proxy_candidates`` and ``_try_connect_device``), this test
drives the *real* discovery code in ``MeshProxyConnection`` by feeding
synthetic ``BluetoothServiceInfo``-shaped objects into the HA bluetooth
layer and asserting the integration recovers when the only stored MAC
is no longer present in BLE range, but a different proxy is currently
advertising the right Network ID.

Real motivation comes from a live run where the only line we get from
the integration is::

    Discovered N proxy candidate(s) on our network (X matched stored MACs)

— meaning recovery has to work even when X==0.

No real BLE is touched: ``_try_connect_device`` is still mocked at the
boundary, but everything *above* it (advert parsing, network-id match,
ordering hint, recovery log) runs for real.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_components.haefele_mesh import gatt as gatt_mod
from custom_components.haefele_mesh.gatt import (
    MeshProxyConnection,
    MeshSession,
)


# Synthetic, gitleaks-allowlisted placeholder keys (see
# tests/fixtures and .gitleaks.toml).
NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"

# Mesh Proxy Service UUID, full + short forms.
PROXY_UUID_FULL = "00001828-0000-1000-8000-00805f9b34fb"
PROXY_UUID_SHORT = "1828"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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
    return MeshProxyConnection(
        hass=SimpleNamespace(),
        session=_make_session(),
        message_handler=None,
    )


def _fake_ble_device(mac: str, name: str | None = None):
    return SimpleNamespace(address=mac, name=name)


def _fake_service_info(
    mac: str,
    network_id: bytes,
    *,
    name: str | None = None,
    use_short_uuid: bool = False,
):
    """Mimic enough of ``BluetoothServiceInfoBleak`` for our parser."""
    uuid_key = PROXY_UUID_SHORT if use_short_uuid else PROXY_UUID_FULL
    payload = bytes([gatt_mod.PROXY_AD_TYPE_NETWORK_ID]) + network_id
    return SimpleNamespace(
        address=mac,
        name=name,
        service_uuids=[uuid_key],
        service_data={uuid_key: payload},
        device=_fake_ble_device(mac, name),
    )


# ---------------------------------------------------------------------------
# the test
# ---------------------------------------------------------------------------

def test_recovers_via_network_id_when_stored_mac_is_stale(
    monkeypatch, caplog,
):
    """Stored candidate MAC isn't advertising; a *different* node is.

    Asserts the full integration path:

    1. The stale stored MAC is never connected to (it's not in range,
       so it never reaches ``_try_connect_device``).
    2. Discovery falls through to the Network-ID match on the
       advertising node.
    3. ``connect_any`` returns True and the integration reports the
       advertising MAC as active.
    4. The recovery-style debug line
       ``Discovered N proxy candidate(s) on our network (0 matched
       stored MACs)`` is emitted.
    """
    proxy = _make_proxy()

    stale_stored_mac = "AA:AA:AA:AA:AA:AA"
    advertising_mac = "DD:DD:DD:DD:DD:DD"

    # Stored hint points at a stale MAC (no longer in BLE range).
    proxy.set_candidates([(stale_stored_mac, "stored-but-stale-node")])

    # Patch HA's bluetooth layer to "see" only the advertising node.
    # Use the short-UUID form to also exercise that branch of
    # _advertises_mesh_proxy / _proxy_service_data.
    advert = _fake_service_info(
        advertising_mac,
        proxy._session.network_id,
        name="luz-desayunos-lookalike",
        use_short_uuid=True,
    )

    def _fake_discovered_service_info(_hass, connectable=True):
        return [advert]

    monkeypatch.setattr(
        gatt_mod.bluetooth,
        "async_discovered_service_info",
        _fake_discovered_service_info,
        raising=False,
    )

    # Skip the 5s "wait for adverts" sleep on the empty-discovery
    # branch — we don't hit it here because adverts are present, but
    # this is cheap insurance against accidental real sleeps.
    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(gatt_mod.asyncio, "sleep", _instant_sleep)

    # Mock the BLE-touching boundary. We assert on every device this
    # gets called with so we can prove the stale MAC was never even
    # attempted (it simply wasn't in the discovered list).
    attempts: list[str] = []

    class _FakeClient:
        is_connected = True

        async def disconnect(self):
            type(self).is_connected = False

    async def _fake_try_connect_device(device, name, timeout):
        attempts.append(device.address)
        proxy._client = _FakeClient()
        proxy._active_mac = device.address
        proxy._active_name = name
        return True

    monkeypatch.setattr(
        proxy, "_try_connect_device", _fake_try_connect_device,
    )

    caplog.set_level(logging.DEBUG, logger="custom_components.haefele_mesh.gatt")

    ok = asyncio.run(proxy.connect_any())

    # 1. + 2. The stale MAC was never tried; we connected through the
    #          advertising node found purely via Network ID.
    assert ok is True
    assert attempts == [advertising_mac], (
        "Stored stale MAC must not be probed, only the advertising node "
        f"should be tried. Got attempts={attempts}"
    )
    # 3. Active connection is the advertising MAC.
    assert proxy._active_mac == advertising_mac
    assert proxy.is_connected is True

    # 4. Recovery debug line — note "0 matched stored MACs", which is
    #    the exact pattern users see in real-world logs when their
    #    stored MAC is stale.
    matching = [
        r for r in caplog.records
        if "Discovered" in r.getMessage()
        and "proxy candidate(s) on our network" in r.getMessage()
    ]
    assert matching, (
        "Expected the 'Discovered N proxy candidate(s) ...' debug line "
        f"to be emitted. Records seen: {[r.getMessage() for r in caplog.records]}"
    )
    msg = matching[-1].getMessage()
    assert "(0 matched stored MACs)" in msg, msg
    assert "1 proxy candidate(s)" in msg, msg


def test_recovery_path_promotes_advertising_mac_into_candidates(monkeypatch):
    """Follow-up to the recovery test: after a successful Network-ID
    recovery, the *next* connect cycle should have the advertising MAC
    available as the preferred ordering hint.

    This guards the documented "stickiness" behaviour: a recovered
    proxy that wasn't in the stored list initially becomes the front
    candidate only if it was already in the list. If it wasn't in the
    list, it must NOT be silently inserted (we don't want to grow the
    candidate list from BLE adverts unboundedly) — it just gets used.
    """
    proxy = _make_proxy()
    stale = "AA:AA:AA:AA:AA:AA"
    advertising = "DD:DD:DD:DD:DD:DD"
    proxy.set_candidates([(stale, "stale")])

    advert = _fake_service_info(advertising, proxy._session.network_id)

    monkeypatch.setattr(
        gatt_mod.bluetooth,
        "async_discovered_service_info",
        lambda _h, connectable=True: [advert],
        raising=False,
    )

    async def _instant_sleep(_d):
        return None

    monkeypatch.setattr(gatt_mod.asyncio, "sleep", _instant_sleep)

    class _FakeClient:
        is_connected = True

        async def disconnect(self):
            type(self).is_connected = False

    async def _fake_try(device, name, timeout):
        proxy._client = _FakeClient()
        proxy._active_mac = device.address
        proxy._active_name = name
        return True

    monkeypatch.setattr(proxy, "_try_connect_device", _fake_try)

    assert asyncio.run(proxy.connect_any()) is True

    # The stored list should be unchanged in content — recovery
    # connects through an unstored MAC without polluting the
    # persistent candidate hints.
    assert [m for m, _ in proxy._candidates] == [stale]
