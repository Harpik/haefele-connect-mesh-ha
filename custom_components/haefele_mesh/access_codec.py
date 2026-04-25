"""
BT Mesh access-layer encoding helpers.

Pure-Python (no Home Assistant or Bleak imports) so the codec can be
unit-tested in isolation. Opcode layout per BT Mesh Profile 3.7.3.1.
"""

from __future__ import annotations

import struct


def encode_opcode(opcode: int) -> bytes:
    """Encode a 1/2/3-byte opcode per spec 3.7.3.1."""
    if opcode <= 0x7E:
        return bytes([opcode])
    if opcode <= 0xFFFF:
        return struct.pack(">H", opcode)
    return struct.pack(">I", opcode)[1:]  # 3 bytes


def decode_opcode(access_pdu: bytes) -> tuple[int, bytes] | None:
    """Return (opcode, params) or None if malformed / truncated."""
    if not access_pdu:
        return None
    first = access_pdu[0]
    if first & 0x80 == 0:
        return first, access_pdu[1:]
    if first & 0xC0 == 0x80:
        if len(access_pdu) < 2:
            return None
        return (first << 8) | access_pdu[1], access_pdu[2:]
    if first & 0xC0 == 0xC0:
        if len(access_pdu) < 3:
            return None
        return (first << 16) | (access_pdu[1] << 8) | access_pdu[2], access_pdu[3:]
    return None
