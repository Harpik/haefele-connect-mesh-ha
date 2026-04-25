"""Tests for access-PDU opcode encoding/decoding.

Imported directly from the module path via conftest's sys.path shim so
we don't pull in HA.
"""

from __future__ import annotations

from access_codec import decode_opcode, encode_opcode


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def test_encode_1byte_opcode():
    assert encode_opcode(0x01) == b"\x01"
    assert encode_opcode(0x7E) == b"\x7E"


def test_encode_2byte_opcode():
    # Generic OnOff Set Unacknowledged = 0x8203
    assert encode_opcode(0x8203) == b"\x82\x03"
    # Light CTL Set Unacknowledged = 0x8260
    assert encode_opcode(0x8260) == b"\x82\x60"


def test_encode_3byte_opcode():
    # Vendor model opcode form: 0xCxxxxx
    assert encode_opcode(0xC12345) == b"\xC1\x23\x45"


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def test_decode_1byte_opcode_and_params():
    op, params = decode_opcode(b"\x01\x02\x03")
    assert op == 0x01
    assert params == b"\x02\x03"


def test_decode_2byte_opcode_and_params():
    # Light CTL Status = 0x8260, payload: lightness(2) + temp(2)
    op, params = decode_opcode(b"\x82\x60\xFF\x7F\x20\x03")
    assert op == 0x8260
    assert params == b"\xFF\x7F\x20\x03"


def test_decode_3byte_opcode():
    op, params = decode_opcode(b"\xC1\x23\x45\xAA")
    assert op == 0xC12345
    assert params == b"\xAA"


def test_decode_empty_returns_none():
    assert decode_opcode(b"") is None


def test_decode_truncated_2byte_returns_none():
    assert decode_opcode(b"\x82") is None


def test_decode_roundtrip_matches_encode():
    for opcode in (0x01, 0x7E, 0x8203, 0x8204, 0x824E, 0x8260, 0xC12345):
        encoded = encode_opcode(opcode)
        decoded = decode_opcode(encoded + b"\x99\x88")
        assert decoded is not None
        op, params = decoded
        assert op == opcode
        assert params == b"\x99\x88"
