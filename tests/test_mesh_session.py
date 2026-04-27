"""
Round-trip tests for MeshSession — the pure crypto + PDU layer.

These tests exercise the *wiring* of the BT Mesh network/transport/
access layers (nonce construction, segmentation flags, obfuscation,
ivi/nid byte, encrypt↔decrypt symmetry) without touching BLE or Home
Assistant. They would have caught every bug we hit during the single-
proxy refactor where the app-nonce / net-nonce / AID framing got
reshuffled.

We import `gatt` through the stubbed `custom_components.haefele_mesh`
package namespace set up by conftest, so no HA is required.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.haefele_mesh.gatt import MeshSession


NET_KEY_HEX = "00112233445566778899AABBCCDDEEFF"
APP_KEY_HEX = "FFEEDDCCBBAA99887766554433221100"
SRC = 0x7FFD  # Provisioner address, matches the Häfele app default.
IV_INDEX = 1


def _seq_provider_factory(start: int = 0x000100):
    """Return an async seq_provider that yields monotonically increasing SEQs."""
    counter = {"value": start}

    async def _next(src_address: int) -> int:
        counter["value"] = (counter["value"] + 1) & 0xFFFFFF
        return counter["value"]

    return _next


def _make_session(iv_index: int = IV_INDEX, src: int = SRC) -> MeshSession:
    return MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex=APP_KEY_HEX,
        src_address=src,
        iv_index=iv_index,
        seq_provider=_seq_provider_factory(),
    )


# ---------------------------------------------------------------------------
# Key-derivation smoke test
# ---------------------------------------------------------------------------

def test_session_derives_expected_nid_and_aid():
    """Smoke-test the k2/k4 plumbing.

    We don't hardcode specific nid/aid values because those depend on
    the (synthetic) test keys and would just be tautological. The real
    regression coverage for k2/k4 wiring is the round-trip suite below
    — if nid or aid are pulled from the wrong byte, encrypt/decrypt
    breaks and the parametric round-trip tests fail loudly.
    """
    s = _make_session()
    assert isinstance(s.nid, int) and 0 <= s.nid <= 0x7F
    assert isinstance(s.aid, int) and 0 <= s.aid <= 0x3F
    assert s.src == SRC
    assert s.iv_index == IV_INDEX


# ---------------------------------------------------------------------------
# Access message round-trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "opcode,params,dst",
    [
        (0x8202, b"\x01\x00\x00", 0x003A),  # OnOff Set Unack ON
        (0x8202, b"\x00\x00\x00", 0x003A),  # OnOff Set Unack OFF
        (0x8207, b"\xFF\x7F\x00\x00", 0x002F),  # Level Set Unack max
        (0x824D, b"\xFF\xFF\x00\x00", 0xC00B),  # Lightness Set Unack, group dst
        (0x8260, b"\xFF\x7F\x20\x03\x00\x00", 0x002F),  # CTL Set Unack
    ],
)
def test_access_pdu_round_trip(opcode, params, dst):
    """Whatever we build with the same session must decode back to
    (src=our_src, dst, opcode, params) identically."""
    s = _make_session()

    pdu = asyncio.run(s.build_access_network_pdu(dst, opcode, params))

    # Basic structural expectations
    assert isinstance(pdu, bytes)
    assert len(pdu) >= 1 + 6 + 2 + 1 + 5  # ivi_nid + obf_hdr + dst + akf/aid + upper(MIC4+ >=1B)

    # ivi bit matches IV Index low bit; NID low 7 bits match derived nid.
    ivi = (pdu[0] >> 7) & 1
    nid = pdu[0] & 0x7F
    assert ivi == (s.iv_index & 1)
    assert nid == s.nid

    # Decoding via the same session must yield the same access fields.
    decoded = s.decode_access_pdu(pdu)
    assert decoded is not None, "Self-built PDU failed to decode"
    got_src, got_dst, got_op, got_params = decoded
    assert got_src == s.src
    assert got_dst == dst
    assert got_op == opcode
    assert got_params == params


def test_access_pdu_foreign_netkey_rejected():
    """A PDU built with a different NetKey must be rejected by our session
    (NID mismatch or MIC failure — either way decode_access_pdu returns None)."""
    sender = _make_session()
    other_net_key = "A" * 32  # different key → different nid/enc/priv
    receiver = MeshSession(
        net_key_hex=other_net_key,
        app_key_hex=APP_KEY_HEX,
        src_address=SRC,
        iv_index=IV_INDEX,
        seq_provider=_seq_provider_factory(),
    )

    pdu = asyncio.run(sender.build_access_network_pdu(0x003A, 0x8202, b"\x01\x00\x00"))
    assert receiver.decode_access_pdu(pdu) is None


def test_access_pdu_foreign_appkey_rejected():
    """Different AppKey → AID mismatch, must return None instead of decrypting
    garbage or raising."""
    sender = _make_session()
    receiver = MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex="B" * 32,
        src_address=SRC,
        iv_index=IV_INDEX,
        seq_provider=_seq_provider_factory(),
    )
    pdu = asyncio.run(sender.build_access_network_pdu(0x003A, 0x8202, b"\x01\x00\x00"))
    assert receiver.decode_access_pdu(pdu) is None


def test_access_pdu_iv_mismatch_rejected():
    """IV Index drift → MIC check fails; decoder returns None instead of
    silently accepting a wrong-era frame (replay-adjacent risk)."""
    sender = _make_session(iv_index=1)
    receiver = _make_session(iv_index=2)
    pdu = asyncio.run(sender.build_access_network_pdu(0x003A, 0x8202, b"\x01\x00\x00"))
    assert receiver.decode_access_pdu(pdu) is None


# ---------------------------------------------------------------------------
# Proxy-config message round-trip
# ---------------------------------------------------------------------------

def test_proxy_config_pdu_round_trip():
    """build_proxy_config_pdu / decode_proxy_config must be symmetric
    (CTL=1, DST=0x0000, opcode byte carried intact)."""
    s = _make_session()
    # Set Filter Type = accept-list (opcode 0x00, param 0x00) per Mesh 6.5.2.1
    message = bytes([0x00, 0x00])
    pdu = asyncio.run(s.build_proxy_config_pdu(message))

    decoded = s.decode_proxy_config(pdu)
    assert decoded is not None
    opcode, params = decoded
    assert opcode == 0x00
    assert params == b"\x00"


def test_proxy_config_not_decodable_as_access():
    """A Proxy Config PDU (CTL=1) must NOT come back through the access
    decoder — the two framings share the network layer but are disjoint
    above it."""
    s = _make_session()
    pdu = asyncio.run(s.build_proxy_config_pdu(bytes([0x00, 0x00])))
    # decode_access_pdu should refuse CTL=1 frames.
    assert s.decode_access_pdu(pdu) is None


# ---------------------------------------------------------------------------
# SEQ plumbing
# ---------------------------------------------------------------------------

def test_build_access_pdu_advances_seq():
    """Every build must consume exactly one SEQ from the provider."""
    seen: list[int] = []

    async def _provider(src_address: int) -> int:
        seen.append(len(seen) + 1)
        return seen[-1]

    s = MeshSession(
        net_key_hex=NET_KEY_HEX,
        app_key_hex=APP_KEY_HEX,
        src_address=SRC,
        iv_index=IV_INDEX,
        seq_provider=_provider,
    )

    asyncio.run(s.build_access_network_pdu(0x003A, 0x8202, b"\x01\x00\x00"))
    asyncio.run(s.build_access_network_pdu(0x003A, 0x8202, b"\x00\x00\x00"))
    asyncio.run(s.build_proxy_config_pdu(bytes([0x00, 0x00])))

    # 2 access + 1 proxy-config = 3 seq emissions.
    assert seen == [1, 2, 3]
