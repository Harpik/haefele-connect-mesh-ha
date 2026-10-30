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
