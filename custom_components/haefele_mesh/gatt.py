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
import logging
import struct
from typing import Awaitable, Callable, Optional

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    MESH_PROXY_DATA_IN_UUID,
    MESH_PROXY_DATA_OUT_UUID,
)
from .access_codec import decode_opcode, encode_opcode
from .mesh_crypto import aes_ccm_decrypt, aes_ccm_encrypt, aes_ecb, k2, k3, k4

_LOGGER = logging.getLogger(__name__)


SeqProvider = Callable[[int], Awaitable[int]]
MessageHandler = Callable[[int, int, bytes], None]
"""`(src_address, opcode, params) -> None`."""


# ---------------------------------------------------------------------------
# Crypto + PDU construction/decoding (no BLE)
# ---------------------------------------------------------------------------

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
        # Derived from AppKey: 6-bit AID
        self.aid = k4(self._app_key)

    # ------------------------------------------------------------------
    # Outbound — application (access) messages
    # ------------------------------------------------------------------

    async def build_access_network_pdu(
        self, dst: int, opcode: int, params: bytes
    ) -> bytes:
        """Encode an access-layer message as a BT Mesh Network PDU."""
        seq = await self._seq_provider(self.src)
        ctl, ttl = 0, 5

        access_pdu = encode_opcode(opcode) + params

        app_nonce = (
            bytes([0x01, 0x00])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", self.src, dst)
            + struct.pack(">I", self.iv_index)
        )
        upper_transport = aes_ccm_encrypt(self._app_key, app_nonce, access_pdu, tag_length=4)
        # Lower Transport: SEG=0, AKF=1, AID
        trans_pdu = bytes([(1 << 6) | (self.aid & 0x3F)]) + upper_transport

        return self._wrap_network_pdu(
            plaintext=struct.pack(">H", dst) + trans_pdu,
            ctl=ctl, ttl=ttl, seq=seq,
        )

    # ------------------------------------------------------------------
    # Outbound — proxy-config messages (encrypted at net layer only)
    # ------------------------------------------------------------------

    async def build_proxy_config_pdu(self, message: bytes) -> bytes:
        """Wrap a Proxy Configuration message as a Network PDU (CTL=1, TTL=0)."""
        seq = await self._seq_provider(self.src)
        ctl, ttl = 1, 0
        plaintext = struct.pack(">H", 0x0000) + message  # DST = 0x0000
        return self._wrap_network_pdu(plaintext, ctl=ctl, ttl=ttl, seq=seq)

    def _wrap_network_pdu(
        self, plaintext: bytes, ctl: int, ttl: int, seq: int
    ) -> bytes:
        """Encrypt + obfuscate a network-layer plaintext into a Network PDU."""
        net_nonce = (
            bytes([0x00, (ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", self.src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", self.iv_index)
        )
        tag_length = 8 if ctl else 4
        encrypted = aes_ccm_encrypt(self._enc_key, net_nonce, plaintext, tag_length=tag_length)

        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", self.iv_index) + encrypted[:7]
        pecb = aes_ecb(self._priv_key, privacy_plaintext)
        cleartext_header = (
            bytes([(ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", self.src)
        )
        obfuscated = bytes(a ^ b for a, b in zip(cleartext_header, pecb[:6]))

        ivi_nid = ((self.iv_index & 1) << 7) | (self.nid & 0x7F)
        return bytes([ivi_nid]) + obfuscated + encrypted

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def decode_access_pdu(self, pdu: bytes) -> Optional[tuple[int, int, int, bytes]]:
        """Decrypt an inbound Network PDU carrying an access message.

        Returns (src, dst, opcode, params) or None if the frame is not ours,
        not an access message, malformed, segmented (unsupported), etc.
        """
        header = self._decode_network_header(pdu)
        if header is None:
            return None
        ctl, seq, src, plaintext = header
        if ctl:
            # Control PDUs handled elsewhere if needed.
            _LOGGER.debug("ctl PDU src=%04X seq=%d — not an access message", src, seq)
            return None

        if len(plaintext) < 3:
            return None
        dst = int.from_bytes(plaintext[:2], "big")
        transport_pdu = plaintext[2:]

        seg = (transport_pdu[0] >> 7) & 1
        akf = (transport_pdu[0] >> 6) & 1
        aid = transport_pdu[0] & 0x3F
        if seg:
            _LOGGER.debug("Ignoring segmented PDU from %04X (unsupported)", src)
            return None
        if not akf:
            _LOGGER.debug("RX devkey-encrypted PDU src=%04X (ignored)", src)
            return None
        if aid != self.aid:
            _LOGGER.debug(
                "AID mismatch (got 0x%02X, expected 0x%02X) — different AppKey",
                aid, self.aid,
            )
            return None

        upper = transport_pdu[1:]
        if len(upper) < 5:
            return None
        app_nonce = (
            bytes([0x01, 0x00])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", src, dst)
            + struct.pack(">I", self.iv_index)
        )
        try:
            access_pdu = aes_ccm_decrypt(self._app_key, app_nonce, upper, tag_length=4)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("App MIC failed src=%04X seq=%d", src, seq)
            return None

        decoded = decode_opcode(access_pdu)
        if decoded is None:
            _LOGGER.debug("Unable to decode opcode raw=%s", access_pdu.hex())
            return None
        opcode, params = decoded
        return src, dst, opcode, params

    def decode_proxy_config(self, pdu: bytes) -> Optional[tuple[int, bytes]]:
        """Decrypt an inbound Proxy Configuration PDU, return (opcode, params)."""
        header = self._decode_network_header(pdu, expected_ctl=1)
        if header is None:
            return None
        _ctl, _seq, _src, plaintext = header
        if len(plaintext) < 3:
            return None
        # plaintext = dst(2) + opcode(1) + params
        opcode = plaintext[2]
        return opcode, plaintext[3:]

    def _decode_network_header(
        self, pdu: bytes, expected_ctl: Optional[int] = None
    ) -> Optional[tuple[int, int, int, bytes]]:
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
        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", self.iv_index) + encrypted[:7]
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

        net_nonce = (
            bytes([0x00, ctl_ttl])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", self.iv_index)
        )
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

        self._client: Optional[BleakClientWithServiceCache] = None
        self._data_in = None
        self._active_mac: Optional[str] = None
        self._active_name: str = ""
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._reassembly_buf = bytearray()
        self._rx_task: Optional[asyncio.Task] = None
        self._connect_lock = asyncio.Lock()
        # Serialises outbound writes so two concurrent entity actions don't
        # interleave SAR chunks on the GATT characteristic (which caused
        # only one of two near-simultaneous turn_off calls to take effect).
        self._send_lock = asyncio.Lock()
        # Signalled whenever a Proxy Filter Status is received.
        self._filter_status_event: asyncio.Event = asyncio.Event()
        # Signalled on the first Secure Network Beacon (proxy PDU type=0x01).
        # Used as the real 'is this a functional proxy?' test — the Filter
        # Status ACK isn't reliable (Häfele firmware doesn't send it).
        self._beacon_event: asyncio.Event = asyncio.Event()
        # Track candidates ordered by last success so we retry smart.
        self._candidates: list[tuple[str, str]] = []
        # Addresses to explicitly accept-list on the proxy after connect.
        self._filter_addresses: list[int] = []
        # Set True while a user-initiated disconnect is in flight so the
        # bleak disconnected callback doesn't trigger an auto-reconnect.
        self._user_disconnecting: bool = False
        # Tracks the in-flight auto-reconnect task (if any). Prevents a
        # storm of reconnect tasks if bleak fires _on_disconnected more
        # than once for the same drop.
        self._reconnect_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_candidates(self, candidates: list[tuple[str, str]]) -> None:
        """Set the (mac, name) list we're allowed to connect to."""
        self._candidates = [(m.upper(), n) for m, n in candidates]

    def set_filter_addresses(self, addresses: list[int]) -> None:
        """Set the addresses to accept-list on every new proxy connection."""
        self._filter_addresses = list(addresses)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def active_mac(self) -> Optional[str]:
        return self._active_mac if self.is_connected else None

    @property
    def active_name(self) -> str:
        return self._active_name if self.is_connected else ""

    async def connect_any(self, timeout_per_candidate: float = 15.0) -> bool:
        """Try each candidate in order; return True on first success."""
        async with self._connect_lock:
            if self.is_connected:
                return True
            for mac, name in self._candidates:
                ok = await self._try_connect(mac, name, timeout=timeout_per_candidate)
                if ok:
                    # Move the winner to the front for next time.
                    self._candidates = (
                        [(mac, name)]
                        + [c for c in self._candidates if c[0] != mac]
                    )
                    return True
                _LOGGER.debug("Proxy candidate %s (%s) unusable, trying next", name, mac)
            _LOGGER.warning(
                "No Häfele node reachable as a mesh proxy (%d candidates tried)",
                len(self._candidates),
            )
            return False

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
        except asyncio.TimeoutError:
            _LOGGER.debug(
                "%s (%s) accepted GATT but never emitted a Secure Network "
                "Beacon — not a functional mesh proxy, trying next candidate",
                name, mac,
            )
            await self._teardown_active()
            return False

        # Got a beacon — real proxy. Now configure the filter. Häfele
        # firmware is flaky about Set Filter Type, so we send Set Filter
        # Type = accept-list AND explicitly add all addresses we care
        # about. No reliance on Filter Status ACK (we've confirmed this
        # firmware never sends it).
        try:
            await self._configure_proxy_filter(reject_list=False)
            if self._filter_addresses:
                await self.add_filter_addresses(self._filter_addresses)
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
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Reconnect callback raised")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Auto-reconnect task crashed")

    async def disconnect(self) -> None:
        self._user_disconnecting = True
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
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Incoming PDU pump crashed")

    def _handle_network_pdu(self, pdu: bytes) -> None:
        decoded = self._session.decode_access_pdu(pdu)
        if decoded is None:
            return
        src, dst, opcode, params = decoded
        _LOGGER.debug(
            "RX src=%04X dst=%04X op=%#06x params=%s",
            src, dst, opcode, params.hex(),
        )
        if self._message_handler is not None:
            try:
                self._message_handler(src, opcode, params)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Message handler raised for src=%04X", src)

    def _parse_secure_network_beacon(self, pdu: bytes) -> None:
        """Extract flags + IV Index from a Secure Network Beacon.

        Format (Mesh Profile 1.0 §3.9.3):
          [0]    beacon type (0x01)
          [1]    flags (bit0=key refresh, bit1=IV update)
          [2:10] network ID (8 bytes)
          [10:14] IV Index (big-endian 32-bit)
          [14:22] authentication value (8 bytes; skipped, we don't
                  verify BeaconKey auth here)

        If the beacon comes from our own network (matching network ID
        derived from our NetKey via k3) AND its IV Index differs from
        our current one, update the session IV Index. This is the
        heuristic that lets inbound Status messages decrypt after an
        IV Update procedure has advanced the network since the config
        file was exported.
        """
        if len(pdu) < 14 or pdu[0] != 0x01:
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
        if is_ours and iv_index != self._session.iv_index:
            _LOGGER.warning(
                "Updating IV Index %d -> %d based on Secure Network Beacon",
                self._session.iv_index, iv_index,
            )
            self._session.iv_index = iv_index

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
        pdu = await self._session.build_access_network_pdu(dst, opcode, params)
        await self._send_proxy_pdu(pdu, pdu_type=0x00)

    async def _configure_proxy_filter(self, reject_list: bool = True) -> None:
        filter_type = 0x01 if reject_list else 0x00
        message = bytes([0x00, filter_type])  # Opcode 0x00 = Set Filter Type
        pdu = await self._session.build_proxy_config_pdu(message)
        await self._send_proxy_pdu(pdu, pdu_type=0x02)
        _LOGGER.debug(
            "Proxy filter set to %s",
            "reject-empty (forward all)" if reject_list else "accept-empty",
        )

    async def add_filter_addresses(self, addresses: list[int]) -> None:
        """Add addresses to the proxy filter (opcode 0x01).

        Häfele firmware silently ignores 'Set Filter Type' (no Filter
        Status reply), so we can't rely on reject-list mode. Instead we
        switch the filter to accept-list and explicitly add the DSTs we
        care about: our own SRC (for unicast replies), every lamp
        unicast (for reply overhear), and every known group (for
        publications from the physical remote / app).
        """
        if not addresses:
            return
        # Opcode 0x01 = Add Addresses To Filter, then N big-endian 16-bit addresses.
        payload = bytes([0x01])
        for addr in addresses:
            payload += struct.pack(">H", addr & 0xFFFF)
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

    async def get_onoff(self, dst: int) -> None:
        await self.send_access(dst, 0x8201, b"")
        _LOGGER.debug("OnOff Get -> dst=%04X", dst)

    async def get_lightness(self, dst: int) -> None:
        await self.send_access(dst, 0x824B, b"")
        _LOGGER.debug("Lightness Get -> dst=%04X", dst)

    async def get_ctl(self, dst: int) -> None:
        await self.send_access(dst, 0x825D, b"")
        _LOGGER.debug("CTL Get -> dst=%04X", dst)
