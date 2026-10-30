"""Segmented messages and device-key encryption (lower/upper transport)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.haefele_mesh import gatt as gatt_mod
from custom_components.haefele_mesh.gatt import MeshProxyConnection, MeshSession
from custom_components.haefele_mesh.transport import (
    Reassembler,
    encode_segment_ack,
    parse_segment,
    parse_segment_ack,
    seq_auth_for,
)

NET_KEY = "00112233445566778899aabbccddeeff"
APP_KEY = "ffeeddccbbaa99887766554433221100"
DEV_KEY = bytes(range(16))
HA, REMOTE = 0x00C8, 0x0017

# Publication Status of the remote's OnOff client on element 0x0023.
STATUS = bytes.fromhex("002300fffe0000ff00090110")


def _pair() -> tuple[MeshSession, MeshSession]:
    seqs = iter(range(0x1000, 0x2000))

    async def seq(_src: int) -> int:
        return next(seqs)

    ha = MeshSession(NET_KEY, APP_KEY, HA, 1, seq)
    remote = MeshSession(NET_KEY, APP_KEY, REMOTE, 1, seq)
    # Both ends of a device-key exchange use the remote's device key.
    ha.device_keys[REMOTE] = DEV_KEY
    remote.device_keys[HA] = DEV_KEY
    return ha, remote


def test_short_config_message_is_one_unsegmented_pdu():
    ha, remote = _pair()
    pdus = asyncio.run(ha.build_devkey_pdus(REMOTE, 0x800C, b""))
    assert len(pdus) == 1
    assert remote.decode_network_pdu(pdus[0]).message == (HA, REMOTE, 0x800C, b"")


def test_publication_set_is_segmented_and_reassembled_with_acks():
    ha, remote = _pair()
    params = bytes.fromhex("2300fffe0000ff0009" "0110")
    pdus = asyncio.run(ha.build_devkey_pdus(REMOTE, 0x03, params))
    assert len(pdus) == 2

    first = remote.decode_network_pdu(pdus[0])
    assert first.message is None
    # No partial ack: it would only make the sender retransmit sooner.
    assert first.ack_to_send is None
    second = remote.decode_network_pdu(pdus[1])
    assert second.message == (HA, REMOTE, 0x03, params)
    assert second.ack_to_send[0] == HA and second.ack_to_send[2] == 0b11


def test_segments_out_of_order_and_repeats_after_completion():
    ha, remote = _pair()
    remote.src = REMOTE
    pdus = asyncio.run(remote.build_devkey_pdus(HA, 0x8019, STATUS))
    assert len(pdus) == 2

    assert ha.decode_network_pdu(pdus[1]).message is None
    done = ha.decode_network_pdu(pdus[0])
    assert done.message == (REMOTE, HA, 0x8019, STATUS)
    # The remote missed our ack and sends a segment again: no second
    # delivery, but a full ack so it stops.
    again = ha.decode_network_pdu(pdus[1])
    assert again.message is None
    assert again.ack_to_send[2] == 0b11


def test_segments_to_someone_else_are_not_acknowledged():
    ha, remote = _pair()
    # The remote answering the Häfele app (0x7FFF), overheard by us.
    remote.device_keys[0x7FFF] = DEV_KEY
    pdus = asyncio.run(remote.build_devkey_pdus(0x7FFF, 0x8019, STATUS))
    assert all(ha.decode_network_pdu(p).ack_to_send is None for p in pdus)


def test_our_segment_ack_reaches_the_sender():
    ha, remote = _pair()
    pdu = asyncio.run(ha.build_segment_ack_pdu(REMOTE, 0x052E, 0b11))
    ack = remote.decode_network_pdu(pdu).ack
    assert (ack.src, ack.dst, ack.seq_zero, ack.block_ack, ack.obo) == (HA, REMOTE, 0x052E, 3, 0)


def test_headers_seen_from_the_real_remote():
    # Two segments of a Publication Status the remote sent in a live test.
    first = parse_segment(REMOTE, HA, 0x52E, bytes.fromhex("8014b801") + bytes(12))
    second = parse_segment(REMOTE, HA, 0x52F, bytes.fromhex("8014b821") + bytes(4))
    assert (first.akf, first.szmic, first.seq_zero, first.seg_o, first.seg_n) == (0, 0, 0x52E, 0, 1)
    assert (second.seg_o, second.seg_n) == (1, 1)
    assert seq_auth_for(second.seq, second.seq_zero) == 0x52E


def test_seq_auth_across_a_seq_zero_wrap():
    assert seq_auth_for(0x2001, 0x1FFF) == 0x1FFF
    assert seq_auth_for(0x12000, 0x0000) == 0x12000


def test_segment_ack_encoding_roundtrip():
    pdu = encode_segment_ack(0x1ABC, 0x80000001, obo=1)
    ack = parse_segment_ack(1, 2, pdu)
    assert (ack.obo, ack.seq_zero, ack.block_ack) == (1, 0x1ABC, 0x80000001)
    assert parse_segment_ack(1, 2, pdu[:-1]) is None


def test_incomplete_messages_expire():
    r = Reassembler(timeout=10)
    seg = parse_segment(REMOTE, HA, 0x100, bytes.fromhex("80040001") + bytes(12))
    assert r.add(seg, now=0)[0] is None
    r.add(seg, now=20)  # the first copy expired; this one starts over
    last = parse_segment(REMOTE, HA, 0x101, bytes.fromhex("80040021") + bytes(4))
    assert r.add(last, now=21)[0] is not None


def test_config_messages_between_other_nodes_are_not_decrypted():
    ha, remote = _pair()
    remote.device_keys[0x7FFF] = DEV_KEY
    pdu = asyncio.run(remote.build_devkey_pdus(0x7FFF, 0x800E, b"\x07"))[0]
    assert ha.decode_network_pdu(pdu).message is None


# ---------------------------------------------------------------------------
# Sending segmented messages: retransmission of what the receiver lacks
# ---------------------------------------------------------------------------


def _sender(monkeypatch, deliver):
    """A proxy connection whose writes go to ``deliver(index, pdu)``."""
    monkeypatch.setattr(gatt_mod, "SAR_ACK_TIMEOUT", 0.05)
    ha, remote = _pair()
    proxy = MeshProxyConnection(hass=SimpleNamespace(), session=ha, message_handler=None)

    async def connected():
        return True

    sent: list[bytes] = []

    async def write(pdu, pdu_type=0x00):
        sent.append(pdu)
        await deliver(len(sent) - 1, pdu, remote, proxy)

    proxy.ensure_connected = connected
    proxy._send_proxy_pdu = write
    return proxy, remote, sent


async def _ack_from_remote(remote, proxy, seq_zero, block):
    pdu = await remote.build_segment_ack_pdu(HA, seq_zero, block)
    proxy._handle_network_pdu(pdu)


@pytest.mark.parametrize("lost", [0, 1])
def test_lost_segment_is_resent_until_acknowledged(monkeypatch, lost):
    received = []
    seq_zero = []

    async def deliver(index, pdu, remote, proxy):
        if index == lost:
            return  # lost on the way
        result = remote.decode_network_pdu(pdu)
        if result.message is not None:
            received.append(result.message)
        # The remote acknowledges the segments it holds.
        block = 0b11 if result.message is not None else 0b11 & ~(1 << lost)
        asyncio.get_running_loop().create_task(
            _ack_from_remote(remote, proxy, seq_zero[0], block))

    proxy, _, sent = _sender(monkeypatch, deliver)
    _record_seq_zero(proxy, seq_zero)
    params = bytes.fromhex("2700fffe0000ff00090110")
    assert asyncio.run(proxy.send_devkey(REMOTE, 0x03, params)) is True
    assert len(sent) == 3  # two segments, then only the lost one again
    assert received == [(HA, REMOTE, 0x03, params)]


def _record_seq_zero(proxy, seq_zero, then=None):
    original = proxy._session.build_devkey_message

    async def build(*args):
        message = await original(*args)
        seq_zero.append(message.seq_zero)
        if then is not None:
            asyncio.get_running_loop().create_task(then(message))
        return message

    proxy._session.build_devkey_message = build


def test_unacknowledged_message_is_given_up(monkeypatch):
    async def deliver(index, pdu, remote, proxy):
        pass  # the remote is asleep

    proxy, _, sent = _sender(monkeypatch, deliver)
    params = bytes.fromhex("2700fffe0000ff00090110")
    assert asyncio.run(proxy.send_devkey(REMOTE, 0x03, params)) is False
    assert len(sent) == 2 * (1 + gatt_mod.SAR_RETRANSMISSIONS)


def test_receiver_cancelling_stops_the_retransmissions(monkeypatch):
    async def deliver(index, pdu, remote, proxy):
        pass

    proxy, remote, sent = _sender(monkeypatch, deliver)

    async def cancel(message):
        await _ack_from_remote(remote, proxy, message.seq_zero, 0)

    _record_seq_zero(proxy, [], then=cancel)
    waits = []
    original_wait = proxy._wait_ack

    async def counted_wait(*args):
        waits.append(args)
        return await original_wait(*args)

    proxy._wait_ack = counted_wait
    assert asyncio.run(proxy.send_devkey(REMOTE, 0x03, bytes(11))) is False
    assert len(sent) == 2
    assert len(waits) == 1  # no more waiting once the receiver gave up
