"""Proxy Configuration framing, filter chunking and beacon authentication."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from custom_components.haefele_mesh.const import (
    IV_INDEX_MAX_JUMP,
    PROXY_FILTER_MAX_ADDRESSES,
)
from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession
from custom_components.haefele_mesh.mesh_crypto import (
    aes_ccm_decrypt,
    aes_ecb,
    beacon_key,
    k3,
    secure_beacon_auth,
)

NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"
SRC = 0x00C8
IV_INDEX = 1000

# Mesh Profile 1.0.1 sample data §8.4.3 (Secure Network Beacon), also used
# by BlueZ unit/test-mesh-crypto.c.
SPEC_NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
SPEC_IV_INDEX = 0x12345678
SPEC_BEACON_KEY = bytes.fromhex("5423d967da639a99cb02231a83f7d254")
SPEC_NETWORK_ID = bytes.fromhex("3ecaff672f673370")
SPEC_BEACON = bytes.fromhex("01003ecaff672f673370123456788ea261582f364f6f")


def _session(iv_index: int = IV_INDEX) -> MeshSession:
    counter = {"seq": 0x100}

    async def _next(_src: int) -> int:
        counter["seq"] += 1
        return counter["seq"]

    return MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex=APP_KEY_HEX,
        src_address=SRC,
        iv_index=iv_index,
        seq_provider=_next,
    )


def _proxy(session: MeshSession) -> MeshProxyConnection:
    return MeshProxyConnection(
        hass=SimpleNamespace(), session=session, message_handler=None,
    )


def _deobfuscate(session: MeshSession, pdu: bytes) -> tuple[int, int, int, bytes]:
    encrypted = pdu[7:]
    privacy_plaintext = b"\x00" * 5 + struct.pack(">I", session.iv_index) + encrypted[:7]
    pecb = aes_ecb(session._priv_key, privacy_plaintext)
    hdr = bytes(a ^ b for a, b in zip(pdu[1:7], pecb[:6]))
    return hdr[0], int.from_bytes(hdr[1:4], "big"), int.from_bytes(hdr[4:6], "big"), encrypted


def _nonce(kind: int, second: int, seq: int, src: int, iv: int) -> bytes:
    return (
        bytes([kind, second]) + seq.to_bytes(3, "big") + struct.pack(">H", src)
        + b"\x00\x00" + struct.pack(">I", iv)
    )


# ---------------------------------------------------------------------------
# Proxy Configuration messages use the Proxy nonce (Mesh Profile §3.8.5.4)
# ---------------------------------------------------------------------------


def test_proxy_config_pdu_is_encrypted_with_proxy_nonce():
    s = _session()
    pdu = asyncio.run(s.build_proxy_config_pdu(b"\x00\x00"))
    ctl_ttl, seq, src, encrypted = _deobfuscate(s, pdu)
    assert ctl_ttl == 0x80 and src == SRC
    proxy_nonce = _nonce(0x03, 0x00, seq, src, IV_INDEX)
    plain = aes_ccm_decrypt(s._enc_key, proxy_nonce, encrypted, tag_length=8)
    assert plain == b"\x00\x00" + b"\x00\x00"  # DST 0x0000 + Set Filter Type
    network_nonce = _nonce(0x00, ctl_ttl, seq, src, IV_INDEX)
    with pytest.raises(Exception):
        aes_ccm_decrypt(s._enc_key, network_nonce, encrypted, tag_length=8)


def test_proxy_config_roundtrip_decodes_filter_status():
    s = _session()
    pdu = asyncio.run(s.build_proxy_config_pdu(b"\x03\x00\x00\x05"))
    assert s.decode_proxy_config(pdu) == (0x03, b"\x00\x00\x05")


def test_network_nonce_frame_is_not_taken_as_proxy_config():
    s = _session()
    plaintext = b"\x00\x00" + b"\x03\x00\x00\x05"
    pdu = s._wrap_network_pdu(plaintext, ctl=1, ttl=0, seq=0x200, src=0x0017)
    assert s.decode_proxy_config(pdu) is None


def test_access_pdus_still_use_network_nonce():
    s = _session()
    pdu = asyncio.run(s.build_access_network_pdu(0xC00F, 0x8201, b""))
    ctl_ttl, seq, src, encrypted = _deobfuscate(s, pdu)
    network_nonce = _nonce(0x00, ctl_ttl, seq, src, IV_INDEX)
    aes_ccm_decrypt(s._enc_key, network_nonce, encrypted, tag_length=4)


def test_filter_status_is_recorded():
    s = _session()
    proxy = _proxy(s)
    pdu = asyncio.run(s.build_proxy_config_pdu(b"\x03\x00\x00\x0d"))
    proxy._handle_proxy_config(pdu)
    assert proxy.last_filter_status == (0x00, 13)


# ---------------------------------------------------------------------------
# Add Addresses To Filter is chunked to fit one Network PDU
# ---------------------------------------------------------------------------


def _capture_filter_messages(addresses: list[int]) -> tuple[MeshSession, list[bytes]]:
    s = _session()
    proxy = _proxy(s)
    sent: list[bytes] = []

    async def _send(pdu: bytes, pdu_type: int = 0x00) -> None:
        assert pdu_type == 0x02
        sent.append(pdu)

    proxy._send_proxy_pdu = _send
    proxy.filter_mode = "accept_list"
    asyncio.run(proxy.add_filter_addresses(addresses))
    return s, sent


def test_filter_addresses_are_split_into_messages_of_five():
    addresses = [0x00C8, 0x0017, 0x002F, 0x003A] + [0xC000 + i for i in range(9)]
    s, sent = _capture_filter_messages(addresses)
    assert len(sent) == 3
    seen: list[int] = []
    for pdu in sent:
        assert len(pdu) <= 29
        opcode, params = s.decode_proxy_config(pdu)
        assert opcode == 0x01
        assert len(params) // 2 <= PROXY_FILTER_MAX_ADDRESSES
        seen += [int.from_bytes(params[i:i + 2], "big") for i in range(0, len(params), 2)]
    assert seen == addresses


def test_five_addresses_fit_in_one_message():
    _s, sent = _capture_filter_messages([1, 2, 3, 4, 5])
    assert len(sent) == 1
    # 1 IVI/NID + 6 header + 2 DST + 1 opcode + 10 addresses + 8 NetMIC.
    # A sixth address would make it 30 bytes, over the 29-byte limit.
    assert len(sent[0]) == 28


def test_no_message_for_empty_filter():
    _s, sent = _capture_filter_messages([])
    assert sent == []


# ---------------------------------------------------------------------------
# Secure Network Beacon authentication
# ---------------------------------------------------------------------------


def test_beacon_key_matches_spec_sample():
    assert beacon_key(SPEC_NET_KEY) == SPEC_BEACON_KEY
    assert k3(SPEC_NET_KEY) == SPEC_NETWORK_ID


def test_beacon_auth_matches_spec_sample():
    auth = secure_beacon_auth(SPEC_BEACON_KEY, 0x00, SPEC_NETWORK_ID, SPEC_IV_INDEX)
    assert bytes([0x01, 0x00]) + SPEC_NETWORK_ID + SPEC_IV_INDEX.to_bytes(4, "big") + auth == SPEC_BEACON


def _beacon(s: MeshSession, iv_index: int, flags: int = 0x00, auth: bytes | None = None) -> bytes:
    if auth is None:
        auth = secure_beacon_auth(s.beacon_key, flags, s.network_id, iv_index)
    return bytes([0x01, flags]) + s.network_id + iv_index.to_bytes(4, "big") + auth


def test_authenticated_beacon_moves_iv_forward():
    s = _session()
    s.iv_confirmed = True
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1))
    assert s.iv_index == IV_INDEX + 1
    assert s.iv_update is False
    assert s.tx_iv_index == IV_INDEX + 1


def test_forged_beacon_is_ignored():
    s = _session()
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1, auth=b"\x00" * 8))
    assert s.iv_index == IV_INDEX
    assert s.iv_confirmed is False


def test_beacon_without_auth_value_is_ignored():
    s = _session()
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1)[:14])
    assert s.iv_index == IV_INDEX


def test_confirmed_iv_never_moves_backwards():
    s = _session()
    s.iv_confirmed = True
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX - 1))
    assert s.iv_index == IV_INDEX


def test_unconfirmed_iv_that_is_too_high_is_corrected_downwards():
    # e.g. a 0.4.6 store whose IV a forged, unauthenticated beacon pushed up
    s = _session(iv_index=5000)
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX))
    assert s.iv_index == IV_INDEX
    assert s.iv_confirmed is True


# ---------------------------------------------------------------------------
# IV Update procedure (Mesh Profile §3.10.5)
# ---------------------------------------------------------------------------


def test_iv_update_in_progress_keeps_transmitting_with_previous_iv():
    s = _session()
    s.iv_confirmed = True
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1, flags=0x02))
    assert s.iv_index == IV_INDEX + 1
    assert s.iv_update is True
    assert s.tx_iv_index == IV_INDEX
    pdu = asyncio.run(s.build_access_network_pdu(0xC00F, 0x8201, b""))
    assert pdu[0] >> 7 == IV_INDEX & 1  # IVI bit of the IV actually used
    # A node that has not seen the update yet decrypts it end to end
    # (network and application nonces both use the previous IV).
    node = _session(iv_index=IV_INDEX)
    assert node.decode_access_pdu(pdu) == (SRC, 0xC00F, 0x8201, b"")


def test_iv_update_completes_with_same_iv_and_flag_cleared():
    s = _session()
    s.iv_confirmed = True
    proxy = _proxy(s)
    proxy._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1, flags=0x02))
    proxy._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1))
    assert s.iv_update is False
    assert s.tx_iv_index == IV_INDEX + 1


def _frame_from_node(iv_index: int) -> bytes:
    """An access PDU a node sends with the given IV Index."""
    sender = _session(iv_index=iv_index)
    sender.src = 0x0017
    return asyncio.run(sender.build_access_network_pdu(SRC, 0x8204, b"\x01"))


def test_receive_picks_iv_by_ivi_bit():
    s = _session(iv_index=IV_INDEX + 1)
    # A node still on the previous IV (other IVI bit) and one on the current.
    assert s.decode_access_pdu(_frame_from_node(IV_INDEX))[0] == 0x0017
    assert s.decode_access_pdu(_frame_from_node(IV_INDEX + 1))[0] == 0x0017


def test_receive_during_iv_update_accepts_old_and_new_iv():
    s = _session()
    s.iv_confirmed = True
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX + 1, flags=0x02))
    assert s.decode_access_pdu(_frame_from_node(IV_INDEX)) is not None
    assert s.decode_access_pdu(_frame_from_node(IV_INDEX + 1)) is not None


def test_confirmed_iv_rejects_jumps_beyond_42():
    s = _session()
    s.iv_confirmed = True
    proxy = _proxy(s)
    proxy._parse_secure_network_beacon(_beacon(s, IV_INDEX + IV_INDEX_MAX_JUMP + 1))
    assert s.iv_index == IV_INDEX
    proxy._parse_secure_network_beacon(_beacon(s, IV_INDEX + IV_INDEX_MAX_JUMP))
    assert s.iv_index == IV_INDEX + IV_INDEX_MAX_JUMP


def test_unconfirmed_iv_from_stale_export_accepts_a_big_jump():
    s = _session(iv_index=1)
    _proxy(s)._parse_secure_network_beacon(_beacon(s, 5000))
    assert s.iv_index == 5000
    assert s.iv_confirmed is True


def test_matching_beacon_confirms_iv():
    s = _session()
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX))
    assert s.iv_index == IV_INDEX
    assert s.iv_confirmed is True


def test_foreign_network_beacon_is_ignored():
    s = _session()
    other = bytes(8)
    auth = secure_beacon_auth(s.beacon_key, 0, other, IV_INDEX + 1)
    pdu = bytes([0x01, 0x00]) + other + (IV_INDEX + 1).to_bytes(4, "big") + auth
    _proxy(s)._parse_secure_network_beacon(pdu)
    assert s.iv_index == IV_INDEX


def test_replayed_in_progress_beacon_for_current_iv_is_ignored():
    # Normal -> In Progress needs IV Index + 1 (§3.10.5); a replayed old
    # "in progress" beacon must not make us send with IV - 1 again.
    s = _session()
    s.iv_confirmed = True
    _proxy(s)._parse_secure_network_beacon(_beacon(s, IV_INDEX, flags=0x02))
    assert s.iv_update is False
    assert s.tx_iv_index == IV_INDEX


# ---------------------------------------------------------------------------
# Filter setup: prefer an empty reject list, fall back to an accept list
# ---------------------------------------------------------------------------


def _scripted_proxy(reply_filter_type: int | None, monkeypatch):
    """A proxy whose Set Filter Type answers with the given type (None = silent)."""
    import custom_components.haefele_mesh.gatt as gatt_mod

    monkeypatch.setattr(gatt_mod, "FILTER_STATUS_TIMEOUT", 0.05)
    s = _session()
    proxy = _proxy(s)
    proxy.set_filter_addresses([SRC, 0xC00F, 0xC00B, 0xC017, 0x002F, 0x003A])
    sent: list[tuple[int, bytes]] = []

    async def _send(pdu: bytes, pdu_type: int = 0x00) -> None:
        opcode, params = s.decode_proxy_config(pdu)
        sent.append((opcode, params))
        if opcode == 0x00 and reply_filter_type is not None:
            # Real firmware answers a Set Filter Type with a Filter Status.
            ftype = params[0] if reply_filter_type == -1 else reply_filter_type
            status = await s.build_proxy_config_pdu(bytes([0x03, ftype, 0x00, 0x00]))
            proxy._handle_proxy_config(status)

    proxy._send_proxy_pdu = _send
    return s, proxy, sent


def test_filter_setup_prefers_forward_all(monkeypatch):
    _s, proxy, sent = _scripted_proxy(-1, monkeypatch)  # echoes the type asked
    asyncio.run(proxy._setup_proxy_filter())
    assert sent == [(0x00, b"\x01")]  # Set Filter Type = reject list, nothing else
    assert proxy.filter_mode == "forward_all"
    assert proxy.last_filter_status == (0x01, 0)
    # A later SRC rotation must not add itself to the reject list (= block it).
    asyncio.run(proxy.add_filter_addresses([0x7EFF]))
    assert sent == [(0x00, b"\x01")]


def test_filter_setup_falls_back_to_accept_list_when_reject_is_refused(monkeypatch):
    _s, proxy, sent = _scripted_proxy(0x00, monkeypatch)  # stays accept list
    asyncio.run(proxy._setup_proxy_filter())
    assert proxy.filter_mode == "accept_list"
    assert sent[0] == (0x00, b"\x01")
    assert sent[1] == (0x00, b"\x00")
    added = b"".join(p for op, p in sent[2:] if op == 0x01)
    assert [int.from_bytes(added[i:i + 2], "big") for i in range(0, len(added), 2)] == [
        SRC, 0xC00F, 0xC00B, 0xC017, 0x002F, 0x003A,
    ]


def test_filter_setup_falls_back_when_proxy_is_silent(monkeypatch):
    _s, proxy, sent = _scripted_proxy(None, monkeypatch)
    asyncio.run(proxy._setup_proxy_filter())
    assert proxy.filter_mode == "accept_list"
    assert [op for op, _p in sent] == [0x00, 0x00, 0x01, 0x01]


def _controlled_proxy(monkeypatch):
    """A proxy whose Filter Status replies are delivered by the test."""
    import custom_components.haefele_mesh.gatt as gatt_mod

    monkeypatch.setattr(gatt_mod, "FILTER_STATUS_TIMEOUT", 0.5)
    s = _session()
    proxy = _proxy(s)
    proxy.set_filter_addresses([SRC, 0xC00F])
    sent: list[tuple[int, bytes]] = []

    async def _send(pdu: bytes, pdu_type: int = 0x00) -> None:
        sent.append(s.decode_proxy_config(pdu))

    async def reply(filter_type: int, size: int = 0) -> None:
        status = bytes([0x03, filter_type]) + size.to_bytes(2, "big")
        proxy._handle_proxy_config(await s.build_proxy_config_pdu(status))

    proxy._send_proxy_pdu = _send
    return proxy, sent, reply


async def _wait_for_set_filter_type(sent):
    for _ in range(100):
        if sent and sent[-1][0] == 0x00:
            return
        await asyncio.sleep(0)
    raise AssertionError("setup never sent Set Filter Type")


def test_src_push_while_waiting_for_status_is_not_sent(monkeypatch):
    # An Add sent while the proxy may already run the reject list would
    # block our new SRC, and its Filter Status would pass for our reply.
    proxy, sent, reply = _controlled_proxy(monkeypatch)

    async def run():
        setup = asyncio.create_task(proxy._setup_proxy_filter())
        await _wait_for_set_filter_type(sent)
        await proxy.add_filter_addresses([0x7EFF])
        assert sent == [(0x00, b"\x01")]
        await reply(0x01)
        await setup

    asyncio.run(run())
    assert sent == [(0x00, b"\x01")]
    assert proxy.filter_mode == "forward_all"


def test_src_rotated_while_waiting_is_added_by_the_fallback(monkeypatch):
    proxy, sent, reply = _controlled_proxy(monkeypatch)

    async def run():
        setup = asyncio.create_task(proxy._setup_proxy_filter())
        await _wait_for_set_filter_type(sent)
        # What the coordinator does on rotation: record, then push.
        proxy.set_filter_addresses([0x7EFF, SRC, 0xC00F])
        await proxy.add_filter_addresses([0x7EFF])
        await reply(0x00)  # proxy stays on an accept list
        await setup

    asyncio.run(run())
    assert sent == [(0x00, b"\x01"), (0x00, b"\x00"), (0x01, bytes.fromhex("7eff00c8c00f"))]
    assert proxy.filter_mode == "accept_list"


def test_second_setup_does_not_reuse_the_previous_mode(monkeypatch):
    # First link fell back to an accept list; on the next link a push
    # during the status wait must not go out as an Add.
    proxy, sent, reply = _controlled_proxy(monkeypatch)

    async def run():
        setup = asyncio.create_task(proxy._setup_proxy_filter())
        await _wait_for_set_filter_type(sent)
        await reply(0x00)
        await setup
        assert proxy.filter_mode == "accept_list"
        sent.clear()
        setup = asyncio.create_task(proxy._setup_proxy_filter())
        await _wait_for_set_filter_type(sent)
        await proxy.add_filter_addresses([0x7EFF])
        await reply(0x01)
        await setup

    asyncio.run(run())
    assert sent == [(0x00, b"\x01")]
    assert proxy.filter_mode == "forward_all"


def test_non_empty_reject_list_is_not_forward_all(monkeypatch):
    proxy, sent, reply = _controlled_proxy(monkeypatch)

    async def run():
        setup = asyncio.create_task(proxy._setup_proxy_filter())
        await _wait_for_set_filter_type(sent)
        await reply(0x01, size=1)
        await setup

    asyncio.run(run())
    assert proxy.filter_mode == "accept_list"
    assert [op for op, _p in sent] == [0x00, 0x00, 0x01]


@pytest.mark.parametrize("drop", ["callback", "teardown", "disconnect"])
def test_filter_mode_is_cleared_when_the_link_drops(drop):
    # A new link starts on an empty accept list (§6.7): whatever we set on
    # the old one no longer applies.
    proxy = _proxy(_session())
    proxy.filter_mode = "forward_all"
    if drop == "callback":
        proxy._user_disconnecting = True  # no auto-reconnect task
        proxy._on_disconnected(None)
    elif drop == "teardown":
        asyncio.run(proxy._teardown_active())
    else:
        asyncio.run(proxy.disconnect())
    assert proxy.filter_mode is None


class _FakeClient:
    is_connected = True

    def __init__(self, proxy, beacon: bool = True):
        self._proxy = proxy
        self._beacon = beacon
        self.services = SimpleNamespace(get_characteristic=lambda uuid: uuid)

    async def start_notify(self, _char, _cb):
        if self._beacon:
            async def _beacon_soon():
                await asyncio.sleep(0.01)
                self._proxy._beacon_event.set()

            asyncio.get_running_loop().create_task(_beacon_soon())

    async def disconnect(self):
        self.is_connected = False


@pytest.mark.parametrize("setup_fails", [False, True])
def test_connect_runs_filter_setup_after_the_beacon(monkeypatch, setup_fails):
    import custom_components.haefele_mesh.gatt as gatt_mod

    proxy = _proxy(_session())
    calls: list[bool] = []

    async def _establish(*_args, **_kwargs):
        return _FakeClient(proxy)

    async def _setup():
        calls.append(proxy._beacon_event.is_set())
        if setup_fails:
            raise ConnectionError("write failed")

    monkeypatch.setattr(gatt_mod, "establish_connection", _establish)
    proxy._setup_proxy_filter = _setup

    async def run():
        device = SimpleNamespace(address="aa:bb:cc:dd:ee:ff")
        ok = await proxy._try_connect_device(device, "Proxy", timeout=1.0)
        await proxy.disconnect()
        return ok

    assert asyncio.run(run()) is True  # a failed setup does not drop the link
    assert calls == [True]
