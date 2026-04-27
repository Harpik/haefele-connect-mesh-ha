"""Tests for the diagnostics helpers.

Focuses on the pure bits that can run without Home Assistant:
MeshProxyConnection.dump_active_gatt_tree(). The full
`async_get_config_entry_diagnostics` path needs a real HA context and
is exercised by HA's own diagnostics CI against the custom component
(hassfest already loads the file).
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession


NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"


async def _noop_seq(_src: int) -> int:
    return 1


def _make_proxy() -> MeshProxyConnection:
    session = MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex=APP_KEY_HEX,
        src_address=0x7FFD,
        iv_index=1,
        seq_provider=_noop_seq,
    )
    return MeshProxyConnection(hass=SimpleNamespace(), session=session)


class _FakeDescriptor:
    def __init__(self, uuid: str, handle: int):
        self.uuid = uuid
        self.handle = handle


class _FakeCharacteristic:
    def __init__(self, uuid: str, handle: int, properties, descriptors=None):
        self.uuid = uuid
        self.handle = handle
        self.properties = properties
        self.descriptors = descriptors or []


class _FakeService:
    def __init__(self, uuid: str, handle: int, characteristics):
        self.uuid = uuid
        self.handle = handle
        self.characteristics = characteristics


class _FakeServices:
    """Iterable mimic of bleak's BleakGATTServiceCollection."""

    def __init__(self, services):
        self._services = services

    def __iter__(self):
        return iter(self._services)


class _FakeConnectedClient:
    is_connected = True

    def __init__(self, services):
        self.services = services


def test_dump_active_gatt_tree_returns_none_when_disconnected():
    p = _make_proxy()
    assert p.dump_active_gatt_tree() is None


def test_dump_active_gatt_tree_extracts_services_and_characteristics():
    p = _make_proxy()
    services = _FakeServices([
        _FakeService(
            uuid="00001828-0000-1000-8000-00805f9b34fb",
            handle=1,
            characteristics=[
                _FakeCharacteristic(
                    uuid="00002add-0000-1000-8000-00805f9b34fb",  # Mesh Proxy Data In
                    handle=3,
                    properties=["write-without-response"],
                ),
                _FakeCharacteristic(
                    uuid="00002ade-0000-1000-8000-00805f9b34fb",  # Mesh Proxy Data Out
                    handle=5,
                    properties=["notify"],
                    descriptors=[
                        _FakeDescriptor(
                            uuid="00002902-0000-1000-8000-00805f9b34fb",  # CCCD
                            handle=6,
                        ),
                    ],
                ),
            ],
        ),
        _FakeService(
            uuid="0000180a-0000-1000-8000-00805f9b34fb",
            handle=10,
            characteristics=[
                _FakeCharacteristic(
                    uuid="00002a29-0000-1000-8000-00805f9b34fb",  # Manufacturer Name
                    handle=12,
                    properties=["read"],
                ),
            ],
        ),
    ])
    p._client = _FakeConnectedClient(services)  # noqa: SLF001

    tree = p.dump_active_gatt_tree()

    assert tree is not None
    assert len(tree) == 2

    proxy_service = tree[0]
    assert proxy_service["uuid"].endswith("-00805f9b34fb")
    assert proxy_service["handle"] == 1
    assert len(proxy_service["characteristics"]) == 2

    data_in = proxy_service["characteristics"][0]
    assert data_in["properties"] == ["write-without-response"]
    assert data_in["descriptors"] == []

    data_out = proxy_service["characteristics"][1]
    assert data_out["properties"] == ["notify"]
    assert len(data_out["descriptors"]) == 1
    assert data_out["descriptors"][0]["handle"] == 6

    devinfo = tree[1]
    assert devinfo["handle"] == 10
    assert devinfo["characteristics"][0]["properties"] == ["read"]


def test_dump_active_gatt_tree_handles_missing_services_attr():
    p = _make_proxy()

    class _NoServices:
        is_connected = True
        services = None

    p._client = _NoServices()  # noqa: SLF001
    assert p.dump_active_gatt_tree() is None


def test_dump_active_gatt_tree_returns_none_when_client_disconnected():
    p = _make_proxy()

    class _Disconnected:
        is_connected = False
        services = _FakeServices([])

    p._client = _Disconnected()  # noqa: SLF001
    assert p.dump_active_gatt_tree() is None
