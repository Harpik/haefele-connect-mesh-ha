"""
Bluetooth Mesh GATT Proxy bearer for Häfele Connect Mesh.

Architecture (post-refactor):

* `MeshSession` — pure crypto + PDU construction/decoding. No BLE, no
  Home Assistant. Holds NetKey/AppKey-derived material, our single SRC
  address and the SEQ provider.

* `MeshProxyConnection` — owns a SINGLE GATT connection to ONE reachable
  Häfele node that implements the Mesh Proxy service (UUID 0x1828).
  All commands and inbound traffic for the whole mesh flow through
  this one link; the proxy node forwards to/from the mesh via its
  radio (advertising bearer). This matches BT Mesh 6.6 and Haefele's
  real deployment where typically only 1–2 nodes advertise the
  Proxy service.

Legacy `MeshGattNode` (one-connection-per-lamp) has been removed — it
broke on nodes that accept GATT but aren't functional proxies (e.g.
'Cocina fuegos' in Jose's install).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .access_codec import decode_opcode, encode_opcode
from .const import (
    FILTER_MODE_ACCEPT_LIST,
    FILTER_MODE_FORWARD_ALL,
    FILTER_STATUS_TIMEOUT,
    IV_INDEX_MAX_JUMP,
    MESH_PROXY_DATA_IN_UUID,
    MESH_PROXY_DATA_OUT_UUID,
    MESH_PROXY_SERVICE_UUID,
    PROXY_FILTER_MAX_ADDRESSES,
)
from .mesh_crypto import (
    aes_ccm_decrypt,
    aes_ccm_encrypt,
    aes_ecb,
    beacon_key,
    k2,
    k3,
    k4,
    secure_beacon_auth,
)
from .transport import (
    MAX_UNSEGMENTED_ACCESS,
    Reassembler,
    SegmentAck,
    encode_segment_ack,
    parse_segment,
    parse_segment_ack,
    segment_headers,
    split_upper_transport,
)

# Mesh Proxy advertisement service-data "Identification Type" byte values
# (Mesh Profile spec 7.2.2.2). We currently match Network ID only — it's
# what Häfele MeshBoxes advertise; Node Identity (0x01) is a future hook.
PROXY_AD_TYPE_NETWORK_ID = 0x00
PROXY_AD_TYPE_NODE_IDENTITY = 0x01

_LOGGER = logging.getLogger(__name__)


# TTL of the access and control messages we originate.
DEFAULT_TTL = 5
# Segmented messages we send: how long to wait for the receiver's Segment
# Ack before resending the missing segments, and how many times.
SAR_ACK_TIMEOUT = 1.0
SAR_RETRANSMISSIONS = 2
# Each Segment Ack we send goes out this many times, this far apart.
ACK_COPIES = 2
ACK_COPY_GAP = 0.1

SeqProvider = Callable[[int], Awaitable[int]]
MessageHandler = Callable[[int, int, bytes], None]
"""`(src_address, opcode, params) -> None`."""


# ---------------------------------------------------------------------------
# Crypto + PDU construction/decoding (no BLE)
# ---------------------------------------------------------------------------

@dataclass
class OutgoingMessage:
    """A message we send, kept so lost segments can be retransmitted."""

    dst: int
    src: int
    # None for an unsegmented message.
    seq_zero: int | None
    # Network-layer plaintext (DST + lower transport PDU) of each segment.
    segments: list[bytes]
    # First transmission.
    pdus: list[bytes]
    # IV Index the message was encrypted with; retransmissions keep it.
    iv_index: int = 0


@dataclass
class DecodedPdu:
    """What an inbound Network PDU carried."""

    # (src, dst, opcode, params) of a complete access message.
    message: tuple[int, int, int, bytes] | None = None
    # A Segment Acknowledgment someone sent.
    ack: SegmentAck | None = None
    # (dst, seq_zero, block_ack): segments addressed to us to acknowledge.
    ack_to_send: tuple[int, int, int] | None = None


class MeshSession:
    """BT Mesh network-/transport-/access-layer codec with no BLE.

    Holds the single (src_address, iv_index, NetKey-derived, AppKey-derived)
    context used for every frame we emit or receive.
    """

    def __init__(
        self,
        net_key_hex: str,
        app_key_hex: str,
        src_address: int,
        iv_index: int,
        seq_provider: SeqProvider,
    ):
        self._net_key = bytes.fromhex(net_key_hex)
        self._app_key = bytes.fromhex(app_key_hex)
        self.src = src_address & 0xFFFF
        self.iv_index = iv_index
        self._seq_provider = seq_provider

        # Derived from NetKey: NID + Encryption Key + Privacy Key
        self.nid, self._enc_key, self._priv_key = k2(self._net_key, b"\x00")
        # Derived from NetKey: 8-byte Network ID (used to recognise Secure
        # Network Beacons from our own mesh).
        self.network_id = k3(self._net_key)
        # Derived from NetKey: BeaconKey, authenticates Secure Network Beacons.
        self.beacon_key = beacon_key(self._net_key)
        # True once the IV Index comes from a beacon (now or persisted from an
        # earlier one) rather than from the .connect export. Until then a
        # larger-than-normal IV jump is accepted so a stale export still works.
        self.iv_confirmed = False
        # IV Update in Progress (Secure Network Beacon flag). While set we
        # still transmit with iv_index - 1 (Mesh Profile 1.0 §3.10.5).
        self.iv_update = False
        # Derived from AppKey: 6-bit AID
        self.aid = k4(self._app_key)
        # Device keys by node primary unicast, for Configuration messages
        # (AKF=0, device nonce).
        self.device_keys: dict[int, bytes] = {}
        self._reassembler = Reassembler()

    @property
    def tx_iv_index(self) -> int:
        """IV Index used to transmit: one less during an IV Update."""
        if self.iv_update and self.iv_index > 0:
            return self.iv_index - 1
        return self.iv_index

    def rx_iv_index(self, ivi: int) -> int:
        """IV Index of a received PDU, chosen by its IVI bit (§3.4.6.3).

        Nodes accept the current IV Index and the one before it; the IVI bit
        (least significant bit of the sender's IV Index) tells them apart.
        """
        if (self.iv_index & 1) == (ivi & 1) or self.iv_index == 0:
            return self.iv_index
        return self.iv_index - 1

    # ------------------------------------------------------------------
    # Outbound — application (access) messages
    # ------------------------------------------------------------------

    async def build_access_network_pdu(
        self, dst: int, opcode: int, params: bytes
    ) -> bytes:
        """Encode an access-layer message as a BT Mesh Network PDU."""
        # Snapshot SRC *before* awaiting the SEQ provider: the provider may
        # rotate self.src, and SRC/SEQ must come from the same space.
        src = self.src
        seq = await self._seq_provider(src)
        ctl, ttl = 0, 5

        access_pdu = encode_opcode(opcode) + params

        app_nonce = (
            bytes([0x01, 0x00])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", src, dst)
            + struct.pack(">I", self.tx_iv_index)
        )
        upper_transport = aes_ccm_encrypt(self._app_key, app_nonce, access_pdu, tag_length=4)
        # Lower Transport: SEG=0, AKF=1, AID
        trans_pdu = bytes([(1 << 6) | (self.aid & 0x3F)]) + upper_transport

        return self._wrap_network_pdu(
            plaintext=struct.pack(">H", dst) + trans_pdu,
            ctl=ctl, ttl=ttl, seq=seq, src=src,
        )

    async def build_devkey_message(
        self, dst: int, opcode: int, params: bytes
    ) -> OutgoingMessage:
        """Encode a Configuration message (dst's device key).

        Unsegmented when the access PDU fits in 11 bytes, segmented
        otherwise (Mesh Profile 1.0.1 §3.5.3). ``pdus`` is the first
        transmission, one Network PDU per segment; ``segments`` keeps the
        plaintext of each so a lost one can be sent again with a new SEQ.
        """
        dev_key = self.device_keys[dst]
        src = self.src
        iv_index = self.tx_iv_index
        access_pdu = encode_opcode(opcode) + params
        # The first SEQ is the message's SeqAuth: nonce, and SeqZero.
        seq_auth = await self._seq_provider(src)
        upper = aes_ccm_encrypt(
            dev_key, self._upper_nonce(0x02, 0, seq_auth, src, dst, iv_index),
            access_pdu, tag_length=4,
        )
        if len(access_pdu) <= MAX_UNSEGMENTED_ACCESS:
            plain = struct.pack(">H", dst) + bytes([0x00]) + upper
            return OutgoingMessage(
                dst=dst, src=src, seq_zero=None, segments=[plain],
                pdus=[self._wrap_network_pdu(
                    plain, ctl=0, ttl=DEFAULT_TTL, seq=seq_auth, src=src, iv_index=iv_index,
                )],
                iv_index=iv_index,
            )
        chunks = split_upper_transport(upper)
        headers = segment_headers(0, 0, 0, seq_auth, len(chunks))
        segments = [struct.pack(">H", dst) + h + c for h, c in zip(headers, chunks)]
        pdus = [self._wrap_network_pdu(
            segments[0], ctl=0, ttl=DEFAULT_TTL, seq=seq_auth, src=src, iv_index=iv_index,
        )]
        for plain in segments[1:]:
            seq = await self._seq_provider(src)
            pdus.append(self._wrap_network_pdu(
                plain, ctl=0, ttl=DEFAULT_TTL, seq=seq, src=src, iv_index=iv_index,
            ))
        return OutgoingMessage(
            dst=dst, src=src, seq_zero=seq_auth & 0x1FFF, segments=segments, pdus=pdus,
            iv_index=iv_index,
        )

    async def build_devkey_pdus(
        self, dst: int, opcode: int, params: bytes
    ) -> list[bytes]:
        """The Network PDUs of a Configuration message (first transmission)."""
        return (await self.build_devkey_message(dst, opcode, params)).pdus

    async def rewrap_segment(self, message: OutgoingMessage, index: int) -> bytes:
        """Segment ``index`` of ``message`` again, with a fresh SEQ (§3.5.3.3).

        Same IV Index as the first transmission: the upper transport was
        encrypted with it, and an IV Update in between must not change it.
        """
        seq = await self._seq_provider(message.src)
        return self._wrap_network_pdu(
            message.segments[index], ctl=0, ttl=DEFAULT_TTL, seq=seq, src=message.src,
            iv_index=message.iv_index,
        )

    async def build_segment_ack_pdu(
        self, dst: int, seq_zero: int, block_ack: int
    ) -> bytes:
        """Segment Acknowledgment for a segmented message dst sent us."""
        src = self.src
        seq = await self._seq_provider(src)
        return self._wrap_network_pdu(
            plaintext=struct.pack(">H", dst) + encode_segment_ack(seq_zero, block_ack),
            ctl=1, ttl=DEFAULT_TTL, seq=seq, src=src,
        )

    @staticmethod
    def _upper_nonce(
        kind: int, aszmic: int, seq: int, src: int, dst: int, iv_index: int,
    ) -> bytes:
        """Application (0x01) or Device (0x02) nonce, §3.8.5.2-3."""
        return (
            bytes([kind, (aszmic & 1) << 7])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", src, dst)
            + struct.pack(">I", iv_index)
        )

    # ------------------------------------------------------------------
    # Outbound — proxy-config messages (encrypted at net layer only)
    # ------------------------------------------------------------------

    async def build_proxy_config_pdu(self, message: bytes) -> bytes:
        """Wrap a Proxy Configuration message as a Network PDU (CTL=1, TTL=0)."""
        src = self.src
        seq = await self._seq_provider(src)
        ctl, ttl = 1, 0
        plaintext = struct.pack(">H", 0x0000) + message  # DST = 0x0000
        return self._wrap_network_pdu(
            plaintext, ctl=ctl, ttl=ttl, seq=seq, src=src, proxy=True,
        )

    def _net_nonce(
        self, ctl_ttl: int, seq: int, src: int, iv_index: int, proxy: bool = False,
    ) -> bytes:
        """Network nonce (type 0x00) or Proxy nonce (type 0x03).

        Mesh Profile 1.0 §3.8.5.1 / §3.8.5.4: Proxy Configuration
        messages use the Proxy nonce, whose second octet is a zero pad
        instead of CTL|TTL. Using the Network nonce for them makes the
        proxy fail the NetMIC and drop the message without a reply.
        """
        if proxy:
            head = bytes([0x03, 0x00])
        else:
            head = bytes([0x00, ctl_ttl & 0xFF])
        return (
            head
            + seq.to_bytes(3, "big")
            + struct.pack(">H", src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", iv_index)
        )

    def _wrap_network_pdu(
        self, plaintext: bytes, ctl: int, ttl: int, seq: int,
        src: int | None = None, proxy: bool = False, iv_index: int | None = None,
    ) -> bytes:
        """Encrypt + obfuscate a network-layer plaintext into a Network PDU."""
        if src is None:
            src = self.src
        if iv_index is None:
            iv_index = self.tx_iv_index
        net_nonce = self._net_nonce(
            (ctl << 7) | (ttl & 0x7F), seq, src, iv_index, proxy=proxy,
        )
        tag_length = 8 if ctl else 4
        encrypted = aes_ccm_encrypt(self._enc_key, net_nonce, plaintext, tag_length=tag_length)

        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", iv_index) + encrypted[:7]
        pecb = aes_ecb(self._priv_key, privacy_plaintext)
        cleartext_header = (
            bytes([(ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", src)
        )
        obfuscated = bytes(a ^ b for a, b in zip(cleartext_header, pecb[:6]))

        ivi_nid = ((iv_index & 1) << 7) | (self.nid & 0x7F)
        return bytes([ivi_nid]) + obfuscated + encrypted

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def decode_access_pdu(self, pdu: bytes) -> tuple[int, int, int, bytes] | None:
        """Decrypt an inbound unsegmented access message.

        Returns (src, dst, opcode, params), or None for anything else.
        Segmented messages need ``decode_network_pdu`` (it keeps state).
        """
        return self.decode_network_pdu(pdu, reassemble=False).message

    def decode_network_pdu(self, pdu: bytes, reassemble: bool = True) -> DecodedPdu:
        """Decrypt an inbound Network PDU.

        Handles unsegmented and (with ``reassemble``) segmented access
        messages, encrypted with the AppKey or with a known device key, and
        Segment Acknowledgments. ``ack_to_send`` asks the caller to
        acknowledge segments addressed to us.
        """
        result = DecodedPdu()
        header = self._decode_network_header(pdu)
        if header is None:
            return result
        ctl, seq, src, plaintext = header
        if len(plaintext) < 3:
            return result
        dst = int.from_bytes(plaintext[:2], "big")
        transport_pdu = plaintext[2:]
        ivi = pdu[0] >> 7

        if ctl:
            result.ack = parse_segment_ack(src, dst, transport_pdu)
            if result.ack is None:
                _LOGGER.debug("ctl PDU src=%04X seq=%d — not an access message", src, seq)
            else:
                _LOGGER.debug(
                    "RX segment ack src=%04X dst=%04X seq_zero=%04X block=%08X",
                    src, dst, result.ack.seq_zero, result.ack.block_ack,
                )
            return result

        if transport_pdu[0] & 0x80:
            if not reassemble:
                return result
            segment = parse_segment(src, dst, seq, transport_pdu)
            if segment is None:
                return result
            message, block_ack = self._reassembler.add(segment)
            complete = block_ack == (1 << (segment.seg_n + 1)) - 1
            _LOGGER.debug(
                "RX segment src=%04X dst=%04X seq_zero=%04X %d/%d akf=%d",
                src, dst, segment.seq_zero, segment.seg_o, segment.seg_n, segment.akf,
            )
            # Acknowledge once the message is complete (and again if a
            # segment of it comes back: our ack was lost). Partial acks
            # would only make the sender retransmit sooner.
            if dst == self.src and complete:
                result.ack_to_send = (src, segment.seq_zero, block_ack)
            if message is None:
                return result
            result.message = self._decrypt_upper(
                src, dst, message.seq_auth, message.akf, message.aid,
                message.szmic, message.upper_transport, ivi,
            )
            return result

        result.message = self._decrypt_upper(
            src, dst, seq, (transport_pdu[0] >> 6) & 1, transport_pdu[0] & 0x3F,
            0, transport_pdu[1:], ivi,
        )
        return result

    def _decrypt_upper(
        self, src: int, dst: int, seq_auth: int, akf: int, aid: int,
        szmic: int, upper: bytes, ivi: int,
    ) -> tuple[int, int, int, bytes] | None:
        """Decrypt an upper transport access PDU; (src, dst, opcode, params)."""
        mic_len = 8 if szmic else 4
        if len(upper) < mic_len + 1:
            return None
        if akf:
            if aid != self.aid:
                _LOGGER.debug(
                    "AID mismatch (got 0x%02X, expected 0x%02X) — different AppKey",
                    aid, self.aid,
                )
                return None
            key, kind = self._app_key, 0x01
        else:
            # Configuration messages between other nodes (for example the
            # remote answering the Häfele app) are not ours to act on.
            key = self.device_keys.get(src) if dst == self.src else None
            if key is None:
                _LOGGER.debug("RX devkey-encrypted PDU src=%04X (ignored)", src)
                return None
            kind = 0x02
        nonce = self._upper_nonce(kind, szmic, seq_auth, src, dst, self.rx_iv_index(ivi))
        try:
            access_pdu = aes_ccm_decrypt(key, nonce, upper, tag_length=mic_len)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Upper transport MIC failed src=%04X akf=%d", src, akf)
            return None
        decoded = decode_opcode(access_pdu)
        if decoded is None:
            _LOGGER.debug("Unable to decode opcode raw=%s", access_pdu.hex())
            return None
        return src, dst, decoded[0], decoded[1]

    def decode_proxy_config(self, pdu: bytes) -> tuple[int, bytes] | None:
        """Decrypt an inbound Proxy Configuration PDU, return (opcode, params)."""
        header = self._decode_network_header(pdu, expected_ctl=1, proxy=True)
        if header is None:
            return None
        _ctl, _seq, _src, plaintext = header
        if len(plaintext) < 3:
            return None
        # plaintext = dst(2) + opcode(1) + params
        opcode = plaintext[2]
        return opcode, plaintext[3:]

    def _decode_network_header(
        self, pdu: bytes, expected_ctl: int | None = None, proxy: bool = False,
    ) -> tuple[int, int, int, bytes] | None:
        """Strip obfuscation + net encryption. Returns (ctl, seq, src, plaintext)."""
        if len(pdu) < 10:
            _LOGGER.debug("Network PDU too short: %d bytes", len(pdu))
            return None

        ivi_nid = pdu[0]
        nid = ivi_nid & 0x7F
        if nid != self.nid:
            _LOGGER.debug("NID mismatch (got 0x%02X, expected 0x%02X)", nid, self.nid)
            return None

        obfuscated = pdu[1:7]
        encrypted = pdu[7:]
        iv_index = self.rx_iv_index(ivi_nid >> 7)
        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", iv_index) + encrypted[:7]
        pecb = aes_ecb(self._priv_key, privacy_plaintext)
        clear_hdr = bytes(a ^ b for a, b in zip(obfuscated, pecb[:6]))
        ctl_ttl = clear_hdr[0]
        ctl = (ctl_ttl >> 7) & 1
        seq = int.from_bytes(clear_hdr[1:4], "big")
        src = int.from_bytes(clear_hdr[4:6], "big")

        if expected_ctl is not None and ctl != expected_ctl:
            return None

        net_mic_len = 8 if ctl else 4
        if len(encrypted) < net_mic_len + 2:
            _LOGGER.debug("Net ciphertext too short (src=%04X)", src)
            return None

        net_nonce = self._net_nonce(ctl_ttl, seq, src, iv_index, proxy=proxy)
        try:
            plaintext = aes_ccm_decrypt(
                self._enc_key, net_nonce, encrypted, tag_length=net_mic_len
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Net MIC failed (src=%04X seq=%d ctl=%d) — not ours / replay / foreign",
                src, seq, ctl,
            )
            return None

        return ctl, seq, src, plaintext


# ---------------------------------------------------------------------------
# Shared GATT Proxy connection
# ---------------------------------------------------------------------------

class MeshProxyConnection:
    """Single shared GATT Proxy connection for the whole Häfele network.

    Accepts a list of candidate MACs (usually every provisioned node) and
    connects to the first one that (a) advertises the Mesh Proxy service
    and (b) behaves as a real proxy (forwards traffic). All commands to
    any destination unicast / group are routed through this one link —
    the connected proxy injects frames into the mesh via its radio, so
    nodes that don't have the Proxy feature enabled (e.g. remote-only
    lamps in the Häfele ecosystem) remain reachable via advertising
    bearer.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: MeshSession,
        message_handler: MessageHandler | None = None,
        reconnect_callback: Callable[[], Awaitable[None]] | None = None,
    ):
        self._hass = hass
        self._session = session
        self._message_handler = message_handler
        # Called (with no args) after an unsolicited reconnect attempt
        # finishes — lets the coordinator refresh availability as soon as
        # the link comes back, instead of waiting for the next heartbeat.
        self._reconnect_callback = reconnect_callback

        self._client: BleakClientWithServiceCache | None = None
        self._data_in = None
        self._active_mac: str | None = None
        self._active_name: str = ""
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._reassembly_buf = bytearray()
        self._rx_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        # Serialises outbound writes so two concurrent entity actions don't
        # interleave SAR chunks on the GATT characteristic (which caused
        # only one of two near-simultaneous turn_off calls to take effect).
        self._send_lock = asyncio.Lock()
        # Signalled whenever a Proxy Filter Status is received.
        self._filter_status_event: asyncio.Event = asyncio.Event()
        # One refresh_filter at a time, so two lists never interleave.
        self._filter_refresh_lock = asyncio.Lock()
        # Last Proxy Filter Status seen: (filter_type, list_size), or None.
        # Exposed in diagnostics to confirm the proxy accepted our filter.
        self.last_filter_status: tuple[int, int] | None = None
        # Filter we configured on the current link: FILTER_MODE_FORWARD_ALL
        # (empty reject list), FILTER_MODE_ACCEPT_LIST, or None while not
        # connected or while setup is still waiting for the Filter Status.
        # Addresses are only pushed to the proxy in accept-list mode.
        self.filter_mode: str | None = None
        # Signalled on the first Secure Network Beacon (proxy PDU type=0x01).
        # Used as the 'is this a functional proxy?' test: nodes with the
        # Proxy feature disabled accept GATT but never send one.
        self._beacon_event: asyncio.Event = asyncio.Event()
        # Track candidates ordered by last success so we retry smart.
        self._candidates: list[tuple[str, str]] = []
        # Addresses for the accept-list fallback, in priority order.
        self._filter_addresses: list[int] = []
        # Set True while a user-initiated disconnect is in flight so the
        # bleak disconnected callback doesn't trigger an auto-reconnect.
        self._user_disconnecting: bool = False
        # Tracks the in-flight auto-reconnect task (if any). Prevents a
        # storm of reconnect tasks if bleak fires _on_disconnected more
        # than once for the same drop.
        self._reconnect_task: asyncio.Task | None = None
        # True while we've already logged the "no connectable transport"
        # error, so a permanent misconfiguration doesn't spam the log on
        # every heartbeat. Reset as soon as a connectable scanner shows up.
        self._no_transport_warned: bool = False
        # In-flight Segment Acknowledgments we are sending.
        self._ack_tasks: set[asyncio.Task] = set()
        # Taking a SEQ and sending its PDU happen under this lock, so a
        # node never sees our SEQs out of order (its replay protection
        # would drop the late one).
        self._tx_lock = asyncio.Lock()
        # Latest BlockAck per (node, SeqZero) of our segmented messages.
        self._acks: dict[tuple[int, int], int] = {}
        self._ack_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_candidates(self, candidates: list[tuple[str, str]]) -> None:
        """Set the (mac, name) list we're allowed to connect to."""
        self._candidates = [(m.upper(), n) for m, n in candidates]

    def set_filter_addresses(self, addresses: list[int]) -> None:
        """Set the addresses to accept-list when the proxy needs an accept list."""
        self._filter_addresses = list(addresses)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def active_mac(self) -> str | None:
        return self._active_mac if self.is_connected else None

    @property
    def active_name(self) -> str:
        return self._active_name if self.is_connected else ""

    async def connect_any(self, timeout_per_candidate: float = 15.0) -> bool:
        """Discover proxies advertising our Network ID; connect to the first usable one.

        Discovery is done by scanning HA's currently-known BLE devices for
        advertisements carrying the Mesh Proxy Service UUID (0x1828) with
        Service Data of the form ``0x00 || NetworkID(8 bytes)``, where
        the Network ID matches ``self._session.network_id`` (k3 of our
        NetKey). This is the standard BT Mesh way to locate "our" mesh
        and avoids depending on stored MAC addresses, which may be
        absent or non-MAC-shaped depending on which platform exported
        the .connect file.

        ``self._candidates`` (set via :meth:`set_candidates`) is treated
        as an *ordering hint*: a discovered device whose address matches
        a stored candidate is tried first. Devices outside the candidate
        list still get tried — stale stored MACs no longer block recovery.
        """
        async with self._connect_lock:
            if self.is_connected:
                return True

            if self._no_connectable_transport():
                return False

            discovered = self._discover_proxy_candidates()
            if not discovered:
                # Give HA's passive scan a brief moment in case we were
                # called before any advertisement has been observed yet
                # (typical at startup or right after the BT adapter
                # came back).
                _LOGGER.debug(
                    "No mesh-proxy advertisements seen yet; waiting briefly for discovery",
                )
                await asyncio.sleep(5.0)
                discovered = self._discover_proxy_candidates()

            if not discovered:
                _LOGGER.warning(
                    "No Häfele node advertising our mesh proxy is currently visible "
                    "to Home Assistant (network_id=%s, %d configured candidate(s))",
                    self._session.network_id.hex(),
                    len(self._candidates),
                )
                return False

            for device, name in discovered:
                mac = (device.address or "").upper()
                ok = await self._try_connect_device(
                    device, name, timeout=timeout_per_candidate,
                )
                if ok:
                    # Move any matching stored candidate to the front so
                    # subsequent reconnects prefer the same proxy first.
                    if mac:
                        in_list = next(
                            (c for c in self._candidates if c[0] == mac), None,
                        )
                        if in_list is not None:
                            self._candidates = (
                                [in_list]
                                + [c for c in self._candidates if c[0] != mac]
                            )
                    return True
                _LOGGER.debug(
                    "Proxy candidate %s (%s) unusable, trying next",
                    name, mac or "?",
                )

            _LOGGER.warning(
                "No Häfele node reachable as a mesh proxy (%d advertising our network)",
                len(discovered),
            )
            return False

    def _no_connectable_transport(self) -> bool:
        """Return True when HA has no Bluetooth transport that can connect.

        Scanner-only gateways (Shelly BLE gateways, BTHome-style bridges,
        ESPHome proxies without ``active: true``) forward advertisements
        but never open outbound GATT connections, so they cannot carry
        mesh traffic no matter how well they hear the lights.

        Without this check the failure surfaces as the generic "no proxy
        visible" warning below, which reads like a range problem and
        sends users hunting for a Proxy-feature setting that isn't the
        cause (see issue #7).
        """
        connectable = _scanner_count(self._hass, connectable=True)
        if connectable is None:
            # Core too old to tell us — fall through to normal discovery.
            return False
        if connectable > 0:
            self._no_transport_warned = False
            return False

        total = _scanner_count(self._hass, connectable=False)
        if self._no_transport_warned:
            _LOGGER.debug(
                "Still no connectable Bluetooth transport (%s scanner(s) total)",
                "?" if total is None else total,
            )
            return True

        self._no_transport_warned = True
        _LOGGER.error(
            "Home Assistant has no Bluetooth transport that can open connections "
            "(%s scanner(s) visible, none connectable). Häfele Connect Mesh needs "
            "a real GATT link to a mesh proxy node: scanner-only gateways such as "
            "Shelly BLE gateways, BTHome bridges or ESPHome proxies without "
            "'active: true' only forward advertisements and cannot be used. "
            "Add a local Bluetooth adapter to the Home Assistant host, or an "
            "ESPHome Bluetooth Proxy with active connections enabled.",
            "?" if total is None else total,
        )
        return True

    def _discover_proxy_candidates(self) -> list[tuple[BLEDevice, str]]:
        """Scan HA's known BLE devices for proxies advertising our Network ID.

        Returns a list of ``(BLEDevice, friendly_name)`` ordered so that
        devices matching our stored MAC candidates come first, then any
        other proxies that match our network. Friendly name is taken
        from the stored candidate when known, otherwise from the BLE
        advertisement.
        """
        wanted_network_id = self._session.network_id  # 8 bytes
        candidate_macs = {mac for mac, _ in self._candidates}
        candidate_names = {mac: name for mac, name in self._candidates}

        preferred: list[tuple[BLEDevice, str]] = []
        others: list[tuple[BLEDevice, str]] = []
        seen_addrs: set[str] = set()

        try:
            service_infos = list(
                bluetooth.async_discovered_service_info(
                    self._hass, connectable=True,
                ),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("async_discovered_service_info failed: %s", err)
            return []

        for info in service_infos:
            if not _advertises_mesh_proxy(info):
                continue
            if not _service_data_matches_network_id(info, wanted_network_id):
                continue

            device: BLEDevice | None = getattr(info, "device", None)
            if device is None:
                # Fall back to looking up by address — some HA versions
                # may not expose .device on every service-info object.
                addr = getattr(info, "address", None)
                if not addr:
                    continue
                device = bluetooth.async_ble_device_from_address(
                    self._hass, addr, connectable=True,
                )
                if device is None:
                    continue

            mac = (device.address or "").upper()
            if not mac or mac in seen_addrs:
                continue
            seen_addrs.add(mac)

            friendly = (
                candidate_names.get(mac)
                or getattr(info, "name", None)
                or getattr(device, "name", None)
                or mac
            )
            entry = (device, friendly)
            if mac in candidate_macs:
                preferred.append(entry)
            else:
                others.append(entry)

        if preferred or others:
            _LOGGER.debug(
                "Discovered %d proxy candidate(s) on our network (%d matched stored MACs)",
                len(preferred) + len(others), len(preferred),
            )
        return preferred + others

    async def _try_connect(self, mac: str, name: str, timeout: float) -> bool:
        device = bluetooth.async_ble_device_from_address(
            self._hass, mac, connectable=True
        )
        if device is None:
            _LOGGER.debug(
                "Proxy candidate %s (%s) not seen by HA bluetooth",
                name, mac,
            )
            return False
        return await self._try_connect_device(device, name, timeout=timeout)

    async def _try_connect_device(
        self, device: BLEDevice, name: str, timeout: float,
    ) -> bool:
        """Connect to a specific BLEDevice and validate it as a mesh proxy."""
        mac = (device.address or "").upper() or "?"

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                name or mac,
                disconnected_callback=self._on_disconnected,
                max_attempts=2,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Connect to %s (%s) failed: %s", name, mac, err)
            return False

        data_in = client.services.get_characteristic(MESH_PROXY_DATA_IN_UUID)
        data_out = client.services.get_characteristic(MESH_PROXY_DATA_OUT_UUID)
        if not data_in or not data_out:
            _LOGGER.debug("Mesh Proxy characteristics missing on %s", mac)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

        try:
            await client.start_notify(data_out, self._on_notification)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("start_notify on %s failed: %s", mac, err)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._client = client
        self._data_in = data_in
        self._active_mac = mac
        self._active_name = name or mac

        if self._rx_task is None or self._rx_task.done():
            self._rx_task = asyncio.create_task(
                self._incoming_pump(), name=f"haefele-rx-{mac}"
            )

        # Real proxy-functionality test: wait for any Secure Network Beacon
        # (PDU type=0x01). Working Häfele proxies emit one within ~2s of
        # a fresh link. Nodes with the Proxy feature disabled (e.g. 'Cocina
        # fuegos') never emit any — so silence = skip this candidate.
        self._beacon_event.clear()
        self._filter_status_event.clear()
        try:
            await asyncio.wait_for(self._beacon_event.wait(), timeout=5.0)
        except TimeoutError:
            _LOGGER.debug(
                "%s (%s) accepted GATT but never emitted a Secure Network "
                "Beacon — not a functional mesh proxy, trying next candidate",
                name, mac,
            )
            await self._teardown_active()
            return False

        # Got a beacon — real proxy. Now configure the filter.
        try:
            await self._setup_proxy_filter()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Proxy filter setup failed on %s: %s", mac, err)

        _LOGGER.info(
            "Mesh proxy connected via %s (%s) [nid=0x%02X aid=0x%02X src=0x%04X]",
            name, mac, self._session.nid, self._session.aid, self._session.src,
        )
        return True

    def dump_active_gatt_tree(self) -> list[dict] | None:
        """Return the cached GATT tree of the currently connected proxy.

        Intended for diagnostics only — reads from the active bleak
        client's cached service collection, so it does not open a new
        connection or disturb the mesh proxy link at all. Returns None
        when nothing is connected.

        Shape::

            [
              {"uuid": "0000180a-...", "handle": 1,
               "characteristics": [
                 {"uuid": "...", "handle": 3,
                  "properties": ["read", "notify"],
                  "descriptors": [{"uuid": "...", "handle": 5}]},
                 ...
               ]},
              ...
            ]
        """
        if self._client is None or not self._client.is_connected:
            return None
        services = getattr(self._client, "services", None)
        if services is None:
            return None

        try:
            service_iter = list(services)
        except Exception:  # noqa: BLE001
            return None

        out: list[dict] = []
        for service in service_iter:
            chars: list[dict] = []
            for char in getattr(service, "characteristics", []) or []:
                descriptors: list[dict] = []
                for desc in getattr(char, "descriptors", []) or []:
                    descriptors.append({
                        "uuid": str(getattr(desc, "uuid", "")),
                        "handle": getattr(desc, "handle", None),
                    })
                chars.append({
                    "uuid": str(getattr(char, "uuid", "")),
                    "handle": getattr(char, "handle", None),
                    "properties": list(getattr(char, "properties", []) or []),
                    "descriptors": descriptors,
                })
            out.append({
                "uuid": str(getattr(service, "uuid", "")),
                "handle": getattr(service, "handle", None),
                "characteristics": chars,
            })
        return out

    async def _teardown_active(self) -> None:
        """Drop whichever client we just attempted, without touching rx_task's
        pump (it's shared across reconnect attempts)."""
        self.filter_mode = None
        # Teardown of a failed/unusable candidate is a user-initiated
        # disconnect from the auto-reconnect perspective — we don't want
        # it to trigger another reconnect cycle through _on_disconnected.
        was_flagging = self._user_disconnecting
        self._user_disconnecting = True
        try:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None
            self._data_in = None
            self._active_mac = None
            self._active_name = ""
        finally:
            self._user_disconnecting = was_flagging

    def _on_disconnected(self, _client) -> None:
        _LOGGER.debug("BLE disconnected from proxy %s", self._active_name)
        # A new link starts on an empty accept list (Mesh Profile §6.7).
        self.filter_mode = None
        # Skip auto-reconnect when the disconnect was user-initiated
        # (explicit disconnect() call, teardown of a failed candidate,
        # or integration unload).
        if self._user_disconnecting:
            return
        # Avoid stacking reconnect tasks if bleak fires the callback more
        # than once (can happen on some stacks during teardown).
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        # Schedule a short-delay reconnect on the HA event loop.
        self._reconnect_task = self._hass.async_create_task(
            self._auto_reconnect(),
            name="haefele-auto-reconnect",
        )

    async def _auto_reconnect(self) -> None:
        """Try to re-establish the proxy link right after an unsolicited drop.

        Called from the bleak disconnected callback. We wait briefly so
        the BLE stack / peer has time to settle, then attempt one
        `connect_any` cycle. The periodic heartbeat remains as the
        safety net if this immediate attempt fails.
        """
        try:
            # Small settle delay — immediate reconnects on some stacks
            # (BlueZ in particular) race the peer teardown and fail.
            await asyncio.sleep(2.0)
            if self._user_disconnecting:
                return
            if self.is_connected:
                return
            _LOGGER.debug("Auto-reconnect: attempting immediate proxy reconnect")
            ok = await self.connect_any()
            if ok:
                _LOGGER.info("Auto-reconnect: proxy link restored via %s",
                             self._active_name)
            else:
                _LOGGER.debug(
                    "Auto-reconnect: no proxy reachable yet, "
                    "will retry on next heartbeat",
                )
            if self._reconnect_callback is not None:
                try:
                    await self._reconnect_callback()
                except Exception:
                    _LOGGER.exception("Reconnect callback raised")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Auto-reconnect task crashed")

    async def disconnect(self) -> None:
        self._user_disconnecting = True
        self.filter_mode = None
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._reconnect_task = None
        if self._rx_task is not None and not self._rx_task.done():
            self._rx_task.cancel()
            try:
                await self._rx_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._rx_task = None
        for task in list(self._ack_tasks):
            task.cancel()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._data_in = None
        self._active_mac = None
        self._active_name = ""
        self._user_disconnecting = False

    async def ensure_connected(self) -> bool:
        if self.is_connected:
            return True
        return await self.connect_any()

    # ------------------------------------------------------------------
    # Inbound pipeline
    # ------------------------------------------------------------------

    def _on_notification(self, _char, data: bytearray) -> None:
        if not data:
            return
        header = data[0]
        sar = (header >> 6) & 0x03
        pdu_type = header & 0x3F
        payload = bytes(data[1:])
        _LOGGER.debug(
            "RX raw %s: sar=%d type=0x%02X len=%d",
            self._active_name, sar, pdu_type, len(payload),
        )

        if sar in (0x00, 0x01):
            self._reassembly_buf = bytearray(payload)
        else:
            self._reassembly_buf.extend(payload)

        if sar in (0x00, 0x03):
            self._incoming.put_nowait((pdu_type, bytes(self._reassembly_buf)))

    async def _incoming_pump(self) -> None:
        try:
            while True:
                pdu_type, pdu = await self._incoming.get()
                if pdu_type == 0x01:
                    # Secure Network Beacon — proof of a functional proxy.
                    self._beacon_event.set()
                    self._parse_secure_network_beacon(pdu)
                    continue
                if pdu_type == 0x02:
                    try:
                        self._handle_proxy_config(pdu)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Malformed proxy-config PDU: %s", err)
                    continue
                if pdu_type != 0x00:
                    _LOGGER.debug("Ignoring proxy PDU type 0x%02X", pdu_type)
                    continue
                try:
                    self._handle_network_pdu(pdu)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Dropped malformed inbound PDU: %s", err)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Incoming PDU pump crashed")

    def _handle_network_pdu(self, pdu: bytes) -> None:
        result = self._session.decode_network_pdu(pdu)
        if result.ack_to_send is not None:
            self._spawn_ack(*result.ack_to_send)
        ack = result.ack
        # Only acks for a message we are still sending: one arriving after
        # it finished would stay and, once SeqZero wraps, answer a new one.
        key = None if ack is None else (ack.src, ack.seq_zero)
        if ack is not None and ack.dst == self._session.src and key in self._acks:
            self._acks[key] = self._acks[key] | ack.block_ack
            if ack.block_ack == 0:
                self._acks[key] = -1  # the receiver cancelled the message
            self._ack_event.set()
        if result.message is None:
            return
        src, dst, opcode, params = result.message
        _LOGGER.debug(
            "RX src=%04X dst=%04X op=%#06x params=%s",
            src, dst, opcode, params.hex(),
        )
        if self._message_handler is not None:
            try:
                self._message_handler(src, opcode, params)
            except Exception:
                _LOGGER.exception("Message handler raised for src=%04X", src)

    def _parse_secure_network_beacon(self, pdu: bytes) -> None:
        """Extract flags + IV Index from a Secure Network Beacon.

        Format (Mesh Profile 1.0 §3.9.3):
          [0]    beacon type (0x01)
          [1]    flags (bit0=key refresh, bit1=IV update)
          [2:10] network ID (8 bytes)
          [10:14] IV Index (big-endian 32-bit)
          [14:22] authentication value (8 bytes, AES-CMAC with BeaconKey)

        The Network ID and IV Index travel in clear, so anyone in BLE range
        can forge them; only the authentication value proves the sender
        holds our NetKey. The IV Index and the IV Update flag are adopted
        only when the beacon is ours and authenticates. Once the IV Index is
        known it never moves backwards and moves forward by at most 42
        (§3.10.5); before that (export or legacy store) any authenticated
        value is taken. This lets inbound Status messages decrypt after an
        IV Update procedure has advanced the network since the config file
        was exported.
        """
        if len(pdu) < 22 or pdu[0] != 0x01:
            _LOGGER.debug("Non-secure beacon or truncated: %s", pdu.hex())
            return
        flags = pdu[1]
        net_id = pdu[2:10]
        iv_index = int.from_bytes(pdu[10:14], "big")

        ours_net_id = getattr(self._session, "network_id", None)
        is_ours = ours_net_id is not None and bytes(ours_net_id) == net_id
        _LOGGER.debug(
            "Secure Network Beacon: flags=0x%02X net_id=%s iv=%d ours_net=%s ours_iv=%d",
            flags, net_id.hex(), iv_index,
            "yes" if is_ours else "no", self._session.iv_index,
        )
        if not is_ours:
            return
        expected = secure_beacon_auth(
            self._session.beacon_key, flags, net_id, iv_index,
        )
        if not hmac.compare_digest(expected, bytes(pdu[14:22])):
            _LOGGER.debug(
                "Secure Network Beacon with our Network ID failed "
                "authentication (iv=%d); ignoring", iv_index,
            )
            return
        session = self._session
        iv_update = bool(flags & 0x02)
        current = session.iv_index
        if session.iv_confirmed:
            # A known IV Index only moves forward, by at most 42 (§3.10.5).
            if iv_index < current:
                _LOGGER.debug(
                    "Ignoring authenticated beacon with older IV Index %d (ours %d)",
                    iv_index, current,
                )
                return
            if iv_index > current + IV_INDEX_MAX_JUMP:
                _LOGGER.warning(
                    "Ignoring authenticated beacon: IV Index %d is more than %d "
                    "ahead of ours (%d)", iv_index, IV_INDEX_MAX_JUMP, current,
                )
                return
            # Normal -> In Progress only on a beacon for the next IV Index
            # (§3.10.5). Beacons can be replayed, so an old "in progress"
            # beacon for our current IV must not send us back to IV - 1.
            if iv_update and not session.iv_update and iv_index == current:
                _LOGGER.debug(
                    "Ignoring IV Update flag on beacon for current IV Index %d",
                    iv_index,
                )
                return
        # An IV Index that no beacon has confirmed yet (from the export, or
        # stored by a release that trusted unauthenticated beacons) may be
        # wrong in either direction: take the authenticated one as is.
        if iv_index != current:
            _LOGGER.warning(
                "Updating IV Index %d -> %d based on Secure Network Beacon",
                current, iv_index,
            )
        if iv_update != session.iv_update:
            _LOGGER.info(
                "IV Update %s (IV Index %d)",
                "in progress" if iv_update else "complete", iv_index,
            )
        session.iv_index = iv_index
        session.iv_update = iv_update
        session.iv_confirmed = True

    def _handle_proxy_config(self, pdu: bytes) -> None:
        decoded = self._session.decode_proxy_config(pdu)
        if decoded is None:
            return
        opcode, params = decoded
        if opcode == 0x03 and len(params) >= 3:
            filter_type = params[0]
            list_size = int.from_bytes(params[1:3], "big")
            _LOGGER.debug(
                "Proxy Filter Status: type=0x%02X size=%d", filter_type, list_size,
            )
            self.last_filter_status = (filter_type, list_size)
            self._filter_status_event.set()
        else:
            _LOGGER.debug(
                "Proxy-config op=0x%02X params=%s", opcode, params.hex(),
            )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def _send_proxy_pdu(self, pdu: bytes, pdu_type: int = 0x00) -> None:
        if not self._client or not self._client.is_connected or self._data_in is None:
            raise ConnectionError("No active mesh-proxy connection")
        max_chunk = 19
        chunks = [pdu[i:i + max_chunk] for i in range(0, len(pdu), max_chunk)]
        async with self._send_lock:
            for i, chunk in enumerate(chunks):
                if len(chunks) == 1:
                    sar = 0x00
                elif i == 0:
                    sar = 0x40
                elif i == len(chunks) - 1:
                    sar = 0xC0
                else:
                    sar = 0x80
                packet = bytes([sar | (pdu_type & 0x3F)]) + chunk
                await self._client.write_gatt_char(self._data_in, packet, response=False)
                await asyncio.sleep(0.05)

    async def send_access(self, dst: int, opcode: int, params: bytes) -> None:
        """Send an access-layer message (encrypted with AppKey) to dst."""
        if not await self.ensure_connected():
            raise ConnectionError("No mesh proxy available")
        async with self._tx_lock:
            pdu = await self._session.build_access_network_pdu(dst, opcode, params)
            await self._send_proxy_pdu(pdu, pdu_type=0x00)

    async def send_devkey(self, dst: int, opcode: int, params: bytes) -> bool:
        """Send a Configuration message (device key) to node dst.

        A segmented message is retransmitted segment by segment until dst
        acknowledges all of it (§3.5.3.3). Returns False if it never did;
        unsegmented messages return True once sent.
        """
        if not await self.ensure_connected():
            raise ConnectionError("No mesh proxy available")
        key = None
        try:
            async with self._tx_lock:
                message = await self._session.build_devkey_message(dst, opcode, params)
                if message.seq_zero is not None:
                    # Before sending, so its acks are recorded; a fresh entry
                    # also drops one left by an older message with this SeqZero.
                    key = (dst, message.seq_zero)
                    self._acks[key] = 0
                for pdu in message.pdus:
                    await self._send_proxy_pdu(pdu, pdu_type=0x00)
            _LOGGER.debug(
                "TX devkey dst=%04X op=%#06x in %d PDU(s)", dst, opcode, len(message.pdus),
            )
            if key is None:
                return True
            full = (1 << len(message.segments)) - 1
            for attempt in range(SAR_RETRANSMISSIONS + 1):
                block = await self._wait_ack(key, full, SAR_ACK_TIMEOUT)
                if block == full:
                    return True
                if block < 0 or attempt == SAR_RETRANSMISSIONS:
                    return False
                missing = [i for i in range(len(message.segments)) if not block & (1 << i)]
                _LOGGER.debug(
                    "TX devkey dst=%04X seq_zero=%04X: resending segments %s",
                    dst, message.seq_zero, missing,
                )
                async with self._tx_lock:
                    for index in missing:
                        pdu = await self._session.rewrap_segment(message, index)
                        await self._send_proxy_pdu(pdu, pdu_type=0x00)
            return False
        finally:
            if key is not None:
                self._acks.pop(key, None)

    async def _wait_ack(self, key: tuple[int, int], full: int, timeout: float) -> int:
        """Latest BlockAck for ``key`` once complete, or when ``timeout`` ends."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            block = self._acks.get(key, 0)
            remaining = deadline - loop.time()
            if block in (full, -1) or remaining <= 0:
                return block
            self._ack_event.clear()
            try:
                await asyncio.wait_for(self._ack_event.wait(), remaining)
            except TimeoutError:
                return self._acks.get(key, 0)

    def _spawn_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        """Acknowledge segments dst sent us, so it stops retransmitting."""
        task = asyncio.get_running_loop().create_task(
            self._send_ack(dst, seq_zero, block_ack)
        )
        self._ack_tasks.add(task)
        task.add_done_callback(self._ack_tasks.discard)

    async def _send_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        if not self.is_connected:
            return
        try:
            # Twice: a battery node listens only briefly after it sends,
            # and a lost ack makes it repeat its message for seconds.
            for copy in range(ACK_COPIES):
                if copy:
                    await asyncio.sleep(ACK_COPY_GAP)
                async with self._tx_lock:
                    pdu = await self._session.build_segment_ack_pdu(dst, seq_zero, block_ack)
                    await self._send_proxy_pdu(pdu, pdu_type=0x00)
            _LOGGER.debug(
                "TX segment ack dst=%04X seq_zero=%04X block=%08X",
                dst, seq_zero, block_ack,
            )
        except Exception:
            _LOGGER.debug("Segment ack to %04X failed", dst, exc_info=True)

    async def _setup_proxy_filter(self) -> None:
        """Make the proxy forward the traffic we care about.

        Preferred: an empty reject list, i.e. forward everything, so group
        publications from the remote and the app reach us whatever their
        address. Häfele proxies keep at most 4 addresses in their filter
        list (verified on hardware), too few for an accept list of every
        group. If the proxy does not confirm the reject list, fall back to an
        accept list with our addresses in priority order.

        While this waits for the Filter Status, filter_mode is None and
        add_filter_addresses only relies on the recorded list: an Add sent
        to a proxy that already switched to a reject list would block the
        address, and its reply could be mistaken for our confirmation.
        """
        self.filter_mode = None
        self.last_filter_status = None
        self._filter_status_event.clear()
        await self._configure_proxy_filter(reject_list=True)
        try:
            await asyncio.wait_for(
                self._filter_status_event.wait(), timeout=FILTER_STATUS_TIMEOUT,
            )
        except TimeoutError:
            pass
        status = self.last_filter_status
        if status == (0x01, 0):  # empty reject list
            self.filter_mode = FILTER_MODE_FORWARD_ALL
            _LOGGER.debug("Proxy confirmed reject list: forwarding all traffic")
            return
        _LOGGER.debug(
            "Proxy did not confirm an empty reject list (status=%s); "
            "using an accept list", status,
        )
        await self._configure_proxy_filter(reject_list=False)
        self.filter_mode = FILTER_MODE_ACCEPT_LIST
        if self._filter_addresses:
            await self.add_filter_addresses(self._filter_addresses)

    async def _configure_proxy_filter(self, reject_list: bool = True) -> None:
        filter_type = 0x01 if reject_list else 0x00
        message = bytes([0x00, filter_type])  # Opcode 0x00 = Set Filter Type
        async with self._tx_lock:
            pdu = await self._session.build_proxy_config_pdu(message)
            await self._send_proxy_pdu(pdu, pdu_type=0x02)
        _LOGGER.debug(
            "Proxy filter set to %s",
            "reject-empty (forward all)" if reject_list else "accept-empty",
        )

    async def refresh_filter(self) -> None:
        """Replace the proxy's accept list with the recorded addresses.

        Set Filter Type empties the list, then the whole list goes in
        priority order, so a proxy that keeps only a few keeps the first
        ones. Nothing to do when everything is forwarded; while setup still
        runs, its accept-list fallback adds the current list itself.
        """
        async with self._filter_refresh_lock:
            if self.filter_mode != FILTER_MODE_ACCEPT_LIST:
                return
            await self._configure_proxy_filter(reject_list=False)
            await self.add_filter_addresses(self._filter_addresses)

    async def add_filter_addresses(self, addresses: list[int]) -> None:
        """Add addresses to the proxy filter (opcode 0x01).

        Only sent while the proxy runs our accept list. With an empty
        reject list everything is forwarded already, and adding to a reject
        list would block the address. While setup is still running the
        address only needs to be in _filter_addresses (set_filter_addresses):
        the accept-list fallback adds the whole list.
        """
        if not addresses:
            return
        if self.filter_mode != FILTER_MODE_ACCEPT_LIST:
            return
        # Opcode 0x01 = Add Addresses To Filter, then N big-endian 16-bit
        # addresses. A Proxy Configuration message is a single unsegmented
        # Network PDU, so it holds at most 5 addresses; a longer one is
        # dropped by the proxy. Send one message per chunk.
        for start in range(0, len(addresses), PROXY_FILTER_MAX_ADDRESSES):
            chunk = addresses[start:start + PROXY_FILTER_MAX_ADDRESSES]
            payload = bytes([0x01])
            for addr in chunk:
                payload += struct.pack(">H", addr & 0xFFFF)
            async with self._tx_lock:
                pdu = await self._session.build_proxy_config_pdu(payload)
                await self._send_proxy_pdu(pdu, pdu_type=0x02)
        _LOGGER.debug(
            "Proxy filter: added %d addresses: %s",
            len(addresses), ", ".join(f"0x{a:04X}" for a in addresses),
        )

    # ------------------------------------------------------------------
    # High-level mesh commands (stateless — dst is provided by caller)
    # ------------------------------------------------------------------

    async def set_onoff(self, dst: int, onoff: bool) -> None:
        tid = (await self._session._seq_provider(self._session.src)) & 0xFF
        await self.send_access(
            dst, 0x8203, struct.pack("BB", int(onoff), tid),
        )
        _LOGGER.debug("OnOff -> %s dst=%04X", "ON" if onoff else "OFF", dst)

    async def set_level(self, dst: int, level: int) -> None:
        tid = (await self._session._seq_provider(self._session.src)) & 0xFF
        level = max(-32768, min(32767, level))
        await self.send_access(
            dst, 0x8207, struct.pack("<hB", level, tid),
        )
        _LOGGER.debug("Level -> %d dst=%04X", level, dst)

    async def set_lightness(self, dst: int, lightness: int) -> None:
        """Light Lightness Set Unacknowledged (0x824D)."""
        tid = (await self._session._seq_provider(self._session.src)) & 0xFF
        await self.send_access(
            dst, 0x824D, struct.pack("<HB", lightness & 0xFFFF, tid),
        )
        _LOGGER.debug("Lightness -> %d dst=%04X", lightness, dst)

    async def set_ctl(self, dst: int, lightness: int, temperature: int) -> None:
        """Light CTL Set Unacknowledged (0x825F)."""
        tid = (await self._session._seq_provider(self._session.src)) & 0xFF
        await self.send_access(
            dst, 0x825F,
            struct.pack("<HHhB", lightness & 0xFFFF, temperature & 0xFFFF, 0, tid),
        )
        _LOGGER.debug(
            "CTL -> lightness=%d temp=%dK dst=%04X", lightness, temperature, dst,
        )

    async def set_hsl(
        self, dst: int, lightness: int, hue: int, saturation: int,
    ) -> None:
        """Light HSL Set Unacknowledged (0x8277).

        Used for RGB-capable Häfele nodes. Standard BT Mesh Light HSL
        server model. hue/saturation/lightness are all 16-bit unsigned
        (0–65535). hue wraps at 65536 ≡ 360°.

        NOTE: not yet verified against physical Häfele RGB hardware —
        the opcode and framing are straight from the Mesh Model spec
        §6.3.4. If your device uses a vendor opcode instead, please file
        an issue with a diagnostics dump.
        """
        tid = (await self._session._seq_provider(self._session.src)) & 0xFF
        await self.send_access(
            dst, 0x8277,
            struct.pack(
                "<HHHB",
                lightness & 0xFFFF,
                hue & 0xFFFF,
                saturation & 0xFFFF,
                tid,
            ),
        )
        _LOGGER.debug(
            "HSL -> lightness=%d hue=%d sat=%d dst=%04X",
            lightness, hue, saturation, dst,
        )

    async def get_onoff(self, dst: int) -> None:
        await self.send_access(dst, 0x8201, b"")
        _LOGGER.debug("OnOff Get -> dst=%04X", dst)

    async def get_lightness(self, dst: int) -> None:
        await self.send_access(dst, 0x824B, b"")
        _LOGGER.debug("Lightness Get -> dst=%04X", dst)

    async def get_ctl(self, dst: int) -> None:
        await self.send_access(dst, 0x825D, b"")
        _LOGGER.debug("CTL Get -> dst=%04X", dst)

    async def get_hsl(self, dst: int) -> None:
        """Light HSL Get (0x826D)."""
        await self.send_access(dst, 0x826D, b"")
        _LOGGER.debug("HSL Get -> dst=%04X", dst)


# ---------------------------------------------------------------------------
# Proxy advertisement parsing helpers
# ---------------------------------------------------------------------------
#
# BT Mesh proxies advertise themselves with a Service Data field keyed by
# the Mesh Proxy Service UUID (0x1828). The payload starts with a 1-byte
# Identification Type, followed by:
#
#   * Type 0x00 (Network ID)    : 8 bytes  — k3(NetKey)
#   * Type 0x01 (Node Identity) : 16 bytes — Hash(8) || Random(8)
#
# We use these helpers to decide whether a discovered BLE device belongs to
# our mesh, without depending on stored MAC addresses (which may be absent
# or non-MAC-shaped depending on which platform exported the .connect file).


def _advertises_mesh_proxy(service_info) -> bool:
    """Return True if the BluetoothServiceInfo advertises the Mesh Proxy UUID."""
    target_full = MESH_PROXY_SERVICE_UUID.lower()
    target_short = "1828"
    for raw in (getattr(service_info, "service_uuids", None) or []):
        u = str(raw).lower()
        if u == target_full or u == target_short:
            return True
    return False


def _proxy_service_data(service_info) -> bytes | None:
    """Return raw service-data bytes for the Mesh Proxy UUID, or None."""
    sd = getattr(service_info, "service_data", None) or {}
    target_full = MESH_PROXY_SERVICE_UUID.lower()
    target_short = "1828"
    for k, v in sd.items():
        ks = str(k).lower()
        if ks == target_full or ks == target_short:
            return bytes(v) if v is not None else None
    return None


def _service_data_matches_network_id(service_info, network_id: bytes) -> bool:
    """Return True if proxy service-data is `0x00 || network_id` (8 bytes)."""
    payload = _proxy_service_data(service_info)
    if payload is None or len(payload) < 1 + 8:
        return False
    if payload[0] != PROXY_AD_TYPE_NETWORK_ID:
        return False
    return payload[1:9] == network_id


def _scanner_count(hass: HomeAssistant, connectable: bool) -> int | None:
    """Count BLE scanners known to HA, or None if core doesn't expose the API.

    ``connectable=True`` counts only scanners able to open outbound GATT
    connections; ``connectable=False`` counts every scanner, including
    advertisement-only ones.
    """
    try:
        return bluetooth.async_scanner_count(hass, connectable=connectable)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("async_scanner_count(connectable=%s) failed: %s", connectable, err)
        return None
