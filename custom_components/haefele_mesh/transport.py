"""Bluetooth Mesh lower transport: segmentation and reassembly (SAR).

Pure Python, no Home Assistant or Bleak, so it can be unit-tested on its
own. Mesh Profile 1.0.1 §3.5.2 (PDU formats) and §3.5.3 (SAR).

Only what the integration needs: splitting an upper transport access PDU
into segments, reassembling segmented access messages addressed to us,
and the Segment Acknowledgment control message in both directions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Upper transport bytes carried by one segment of an access message.
SEGMENT_SIZE = 12
# Largest unsegmented access PDU (15-byte lower transport payload minus a
# 4-byte TransMIC).
MAX_UNSEGMENTED_ACCESS = 11
# Control opcode of a Segment Acknowledgment message.
SEGMENT_ACK_OPCODE = 0x00
SEQ_ZERO_MASK = 0x1FFF


@dataclass(frozen=True)
class Segment:
    """One received segment of a segmented access message."""

    src: int
    dst: int
    seq: int
    akf: int
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    data: bytes


@dataclass(frozen=True)
class SegmentAck:
    """A Segment Acknowledgment control message."""

    src: int
    dst: int
    obo: int
    seq_zero: int
    block_ack: int


@dataclass(frozen=True)
class Reassembled:
    """A complete segmented message, still encrypted at the upper transport."""

    src: int
    dst: int
    akf: int
    aid: int
    szmic: int
    seq_auth: int
    upper_transport: bytes


def parse_segment(src: int, dst: int, seq: int, transport_pdu: bytes) -> Segment | None:
    """Parse a segmented access lower transport PDU (SEG=1, CTL=0)."""
    if len(transport_pdu) < 5 or not transport_pdu[0] & 0x80:
        return None
    header = int.from_bytes(transport_pdu[1:4], "big")
    seg_o = (header >> 5) & 0x1F
    seg_n = header & 0x1F
    if seg_o > seg_n:
        return None
    return Segment(
        src=src,
        dst=dst,
        seq=seq,
        akf=(transport_pdu[0] >> 6) & 1,
        aid=transport_pdu[0] & 0x3F,
        szmic=header >> 23,
        seq_zero=(header >> 10) & SEQ_ZERO_MASK,
        seg_o=seg_o,
        seg_n=seg_n,
        data=transport_pdu[4:],
    )


def seq_auth_for(seq: int, seq_zero: int) -> int:
    """SeqAuth of a segment: the SEQ whose low 13 bits are SeqZero, at most seq."""
    return seq - ((seq - seq_zero) & SEQ_ZERO_MASK)


def segment_headers(akf: int, aid: int, szmic: int, seq_zero: int, count: int) -> list[bytes]:
    """The 4-byte lower transport header of each of ``count`` segments."""
    seg_n = count - 1
    first = bytes([0x80 | ((akf & 1) << 6) | (aid & 0x3F)])
    return [
        first
        + (
            ((szmic & 1) << 23)
            | ((seq_zero & SEQ_ZERO_MASK) << 10)
            | (seg_o << 5)
            | seg_n
        ).to_bytes(3, "big")
        for seg_o in range(count)
    ]


def split_upper_transport(upper_transport: bytes) -> list[bytes]:
    """Split an upper transport PDU into segment payloads (at most 32)."""
    chunks = [
        upper_transport[i:i + SEGMENT_SIZE]
        for i in range(0, len(upper_transport), SEGMENT_SIZE)
    ]
    if not chunks or len(chunks) > 32:
        raise ValueError("upper transport PDU does not fit in 1-32 segments")
    return chunks


def encode_segment_ack(seq_zero: int, block_ack: int, obo: int = 0) -> bytes:
    """Lower transport PDU of a Segment Acknowledgment (CTL=1, SEG=0)."""
    return (
        bytes([SEGMENT_ACK_OPCODE])
        + (((obo & 1) << 15) | ((seq_zero & SEQ_ZERO_MASK) << 2)).to_bytes(2, "big")
        + (block_ack & 0xFFFFFFFF).to_bytes(4, "big")
    )


def parse_segment_ack(src: int, dst: int, transport_pdu: bytes) -> SegmentAck | None:
    """Parse a control PDU as a Segment Acknowledgment, or None."""
    if len(transport_pdu) != 7 or transport_pdu[0] != SEGMENT_ACK_OPCODE:
        return None
    word = int.from_bytes(transport_pdu[1:3], "big")
    return SegmentAck(
        src=src,
        dst=dst,
        obo=word >> 15,
        seq_zero=(word >> 2) & SEQ_ZERO_MASK,
        block_ack=int.from_bytes(transport_pdu[3:7], "big"),
    )


@dataclass
class _Pending:
    seg_n: int
    akf: int
    aid: int
    szmic: int
    seq_auth: int
    parts: dict[int, bytes] = field(default_factory=dict)
    started: float = 0.0
    done: bool = False

    @property
    def block_ack(self) -> int:
        bits = 0
        for seg_o in self.parts:
            bits |= 1 << seg_o
        return bits


class Reassembler:
    """Collect the segments of messages addressed to us.

    ``add`` returns ``(message, block_ack)``: the message once it is
    complete (only the first time), and the Block Ack to send back. A
    segment of an already completed message returns ``(None, full_ack)``
    so a sender that missed our ack stops retransmitting.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._pending: dict[tuple[int, int], _Pending] = {}

    def add(
        self, segment: Segment, now: float | None = None,
    ) -> tuple[Reassembled | None, int]:
        now = time.monotonic() if now is None else now
        self._expire(now)
        seq_auth = seq_auth_for(segment.seq, segment.seq_zero)
        key = (segment.src, seq_auth)
        entry = self._pending.get(key)
        if entry is None:
            entry = _Pending(
                seg_n=segment.seg_n, akf=segment.akf, aid=segment.aid,
                szmic=segment.szmic, seq_auth=seq_auth, started=now,
            )
            self._pending[key] = entry
        elif segment.seg_n != entry.seg_n:
            # Inconsistent with what we already hold: ignore the segment.
            return None, entry.block_ack
        if entry.done:
            return None, entry.block_ack
        entry.parts[segment.seg_o] = segment.data
        if len(entry.parts) <= entry.seg_n:
            return None, entry.block_ack
        entry.done = True
        upper = b"".join(entry.parts[i] for i in range(entry.seg_n + 1))
        return (
            Reassembled(
                src=segment.src, dst=segment.dst, akf=entry.akf, aid=entry.aid,
                szmic=entry.szmic, seq_auth=seq_auth, upper_transport=upper,
            ),
            entry.block_ack,
        )

    def _expire(self, now: float) -> None:
        for key in [k for k, v in self._pending.items() if now - v.started > self._timeout]:
            del self._pending[key]
