"""
Bluetooth Mesh GATT Proxy Bearer for Häfele Connect Mesh.

Handles BLE connection, PDU segmentation, encryption and
sequence number management per BT Mesh spec. Also decodes
inbound Network → Transport → Access PDUs so status messages
emitted by mesh nodes (when they are turned on/off/changed by
a physical switch or the Häfele app) can update HA state.

Uses Home Assistant's bluetooth subsystem (supports local HCI
adapters and ESPHome Bluetooth Proxies transparently) and
bleak-retry-connector for robust connections.
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
from .mesh_crypto import aes_ccm_decrypt, aes_ccm_encrypt, aes_ecb, k2, k4

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

SeqProvider = Callable[[int], Awaitable[int]]
"""Callback `(src_address) -> next_seq` owned by the coordinator."""

MessageHandler = Callable[[int, int, bytes], None]
"""`(src_address, opcode, params) -> None` — fired for each decoded access PDU."""


# ---------------------------------------------------------------------------
# GATT Proxy node
# ---------------------------------------------------------------------------

class MeshGattNode:
    """
    Controls a single Häfele Mesh node via GATT Proxy bearer.

    Uses Home Assistant's bluetooth subsystem for device lookup
    (so ESPHome Bluetooth Proxies work out of the box) and
    bleak-retry-connector for the actual connection.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        mac: str,
        unicast: int,
        net_key_hex: str,
        app_key_hex: str,
        src_address: int,
        seq_provider: SeqProvider,
        iv_index: int = 1,
        name: str = "",
        message_handler: MessageHandler | None = None,
    ):
        self._hass = hass
        self.mac = mac.upper()
        self.unicast = unicast
        self.name = name or self.mac
        self._iv_index = iv_index
        self._src = src_address
        self._seq_provider = seq_provider
        self._message_handler = message_handler

        # Crypto — derived once from network/app keys
        self._net_key = bytes.fromhex(net_key_hex)
        self._app_key = bytes.fromhex(app_key_hex)
        self._nid, self._enc_key, self._priv_key = k2(self._net_key, b"\x00")
        self._aid = k4(self._app_key)

        # BLE state
        self._client: Optional[BleakClientWithServiceCache] = None
        self._data_in = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._reassembly_buf = bytearray()
        self._connect_lock = asyncio.Lock()
        self._rx_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _resolve_device(self) -> Optional[BLEDevice]:
        """Ask HA's bluetooth manager for the current BLEDevice."""
        return bluetooth.async_ble_device_from_address(
            self._hass, self.mac, connectable=True
        )

    async def connect(self, timeout: float = 20.0) -> bool:
        """Establish a GATT connection and subscribe to notifications."""
        async with self._connect_lock:
            if self.is_connected:
                return True

            device = self._resolve_device()
            if device is None:
                _LOGGER.warning(
                    "Device %s (%s) not seen by HA bluetooth; is the node in range "
                    "of the HA adapter or an ESPHome BT proxy?",
                    self.name, self.mac,
                )
                return False

            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=self._on_disconnected,
                    max_attempts=3,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to connect to %s: %s", self.name, err)
                return False

            self._data_in = self._client.services.get_characteristic(MESH_PROXY_DATA_IN_UUID)
            data_out = self._client.services.get_characteristic(MESH_PROXY_DATA_OUT_UUID)
            if not self._data_in or not data_out:
                _LOGGER.error("Mesh Proxy characteristics not found on %s", self.mac)
                await self._safe_disconnect()
                return False

            try:
                await self._client.start_notify(data_out, self._on_notification)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("start_notify failed on %s: %s", self.name, err)
                await self._safe_disconnect()
                return False

            # Start incoming pipeline consumer
            if self._rx_task is None or self._rx_task.done():
                self._rx_task = asyncio.create_task(
                    self._incoming_pump(), name=f"haefele-rx-{self.mac}"
                )

            _LOGGER.info("Connected to %s (%s)", self.name, self.mac)
            return True

    def _on_disconnected(self, _client) -> None:
        _LOGGER.debug("BLE disconnected from %s", self.name)

    async def _safe_disconnect(self) -> None:
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

    async def disconnect(self) -> None:
        await self._safe_disconnect()
        _LOGGER.debug("Disconnected from %s", self.name)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def ensure_connected(self, timeout: float = 20.0) -> bool:
        if self.is_connected:
            return True
        return await self.connect(timeout=timeout)

    # ------------------------------------------------------------------
    # Inbound pipeline: SAR reassembly → Network → Transport → Access
    # ------------------------------------------------------------------

    def _on_notification(self, _char, data: bytearray) -> None:
        if not data:
            return
        header = data[0]
        sar = (header >> 6) & 0x03
        pdu_type = header & 0x3F
        payload = bytes(data[1:])

        if sar in (0x00, 0x01):
            self._reassembly_buf = bytearray(payload)
        else:
            self._reassembly_buf.extend(payload)

        if sar in (0x00, 0x03):
            self._incoming.put_nowait((pdu_type, bytes(self._reassembly_buf)))

    async def _incoming_pump(self) -> None:
        """Consume the reassembled-PDU queue and route decrypted access PDUs."""
        try:
            while True:
                pdu_type, pdu = await self._incoming.get()
                if pdu_type != 0x00:
                    # 0x00 = Network PDU. 0x01=mesh beacon, 0x02=proxy config,
                    # 0x03=provisioning. We only care about network PDUs here.
                    continue
                try:
                    self._handle_network_pdu(pdu)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Dropped malformed inbound PDU from %s: %s", self.name, err)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Incoming PDU pump crashed for %s", self.name)

    def _handle_network_pdu(self, pdu: bytes) -> None:
        """Decrypt Network PDU → Transport PDU → Access PDU, fire callback."""
        if len(pdu) < 10:
            return

        # Byte 0 = IVI(1) | NID(7)
        ivi_nid = pdu[0]
        nid = ivi_nid & 0x7F
        if nid != self._nid:
            return  # Not for our network

        obfuscated = pdu[1:7]
        encrypted = pdu[7:]  # encrypted [dst(2) + transport_pdu + net_mic]

        # De-obfuscate header using Privacy Key
        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", self._iv_index) + encrypted[:7]
        pecb = aes_ecb(self._priv_key, privacy_plaintext)
        clear_hdr = bytes(a ^ b for a, b in zip(obfuscated, pecb[:6]))
        ctl_ttl = clear_hdr[0]
        ctl = (ctl_ttl >> 7) & 1
        seq = int.from_bytes(clear_hdr[1:4], "big")
        src = int.from_bytes(clear_hdr[4:6], "big")

        # Network MIC length: 4 for access (ctl=0), 8 for control (ctl=1)
        net_mic_len = 8 if ctl else 4
        if len(encrypted) < net_mic_len + 2:
            return

        net_nonce = (
            bytes([0x00, ctl_ttl])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", self._iv_index)
        )
        try:
            plaintext = aes_ccm_decrypt(
                self._enc_key, net_nonce, encrypted, tag_length=net_mic_len
            )
        except Exception:  # noqa: BLE001
            return  # MIC failure → not for us / foreign mesh / replay

        if len(plaintext) < 3:
            return
        dst = int.from_bytes(plaintext[:2], "big")
        transport_pdu = plaintext[2:]
        _ = dst  # noqa: F841 — could be used later for group filtering

        if ctl:
            # Control PDUs (heartbeat, ack, friendship…) — ignore for now.
            return

        # Lower Transport PDU (access): [SEG(1)|AKF(1)|AID(6)] ...
        seg = (transport_pdu[0] >> 7) & 1
        akf = (transport_pdu[0] >> 6) & 1
        aid = transport_pdu[0] & 0x3F

        if seg:
            # Segmented Access PDUs — for MVP we ignore. Status messages from
            # Generic/Light servers fit in one segment so this is rarely needed.
            _LOGGER.debug("Ignoring segmented PDU from %04X (not yet supported)", src)
            return
        if not akf:
            # Device-key encrypted (configuration messages) — not for us.
            return
        if aid != self._aid:
            return  # Different AppKey

        # Upper Transport PDU = transport_pdu[1:], tag 4 bytes at the end
        upper = transport_pdu[1:]
        if len(upper) < 5:
            return

        app_nonce = (
            bytes([0x01, 0x00])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", src, dst)
            + struct.pack(">I", self._iv_index)
        )
        try:
            access_pdu = aes_ccm_decrypt(self._app_key, app_nonce, upper, tag_length=4)
        except Exception:  # noqa: BLE001
            return

        decoded = decode_opcode(access_pdu)
        if decoded is None:
            return
        opcode, params = decoded

        _LOGGER.debug(
            "RX %s: src=%04X dst=%04X op=%#06x params=%s",
            self.name, src, dst, opcode, params.hex(),
        )

        if self._message_handler is not None:
            try:
                self._message_handler(src, opcode, params)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Message handler raised for %s", self.name)

    # ------------------------------------------------------------------
    # PDU segmentation (outbound BT Mesh 6.6.2)
    # ------------------------------------------------------------------

    async def _send_proxy_pdu(self, pdu: bytes, pdu_type: int = 0x00) -> None:
        max_chunk = 19
        chunks = [pdu[i:i + max_chunk] for i in range(0, len(pdu), max_chunk)]
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

    # ------------------------------------------------------------------
    # Network PDU construction (BT Mesh 3.4 + 3.6 + 3.8)
    # ------------------------------------------------------------------

    async def _build_network_pdu(self, dst: int, opcode: int, params: bytes) -> bytes:
        seq = await self._seq_provider(self._src)
        ctl, ttl = 0, 5

        access_pdu = encode_opcode(opcode) + params

        app_nonce = (
            bytes([0x01, 0x00])
            + seq.to_bytes(3, "big")
            + struct.pack(">HH", self._src, dst)
            + struct.pack(">I", self._iv_index)
        )
        upper_transport = aes_ccm_encrypt(self._app_key, app_nonce, access_pdu, tag_length=4)
        trans_pdu = bytes([(1 << 6) | (self._aid & 0x3F)]) + upper_transport

        net_nonce = (
            bytes([0x00, (ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", self._src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", self._iv_index)
        )
        plaintext = struct.pack(">H", dst) + trans_pdu
        encrypted = aes_ccm_encrypt(self._enc_key, net_nonce, plaintext, tag_length=4)

        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", self._iv_index) + encrypted[:7]
        pecb = aes_ecb(self._priv_key, privacy_plaintext)
        cleartext_header = (
            bytes([(ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", self._src)
        )
        obfuscated = bytes(a ^ b for a, b in zip(cleartext_header, pecb[:6]))

        ivi_nid = ((self._iv_index & 1) << 7) | (self._nid & 0x7F)
        return bytes([ivi_nid]) + obfuscated + encrypted

    async def _send(self, dst: int, opcode: int, params: bytes) -> None:
        if not await self.ensure_connected():
            raise ConnectionError(f"Cannot connect to {self.name}")
        pdu = await self._build_network_pdu(dst, opcode, params)
        await self._send_proxy_pdu(pdu)

    # ------------------------------------------------------------------
    # Mesh commands — Set (acknowledged-Unack variants are fire-and-forget
    # ------------------------------------------------------------------

    async def set_onoff(self, dst: int, onoff: bool) -> None:
        tid = (await self._seq_provider(self._src)) & 0xFF
        await self._send(dst, 0x8203, struct.pack("BB", int(onoff), tid))
        _LOGGER.debug("%s OnOff -> %s", self.name, "ON" if onoff else "OFF")

    async def set_level(self, dst: int, level: int) -> None:
        """Generic Level Set Unack. level: -32768 to 32767"""
        tid = (await self._seq_provider(self._src)) & 0xFF
        level = max(-32768, min(32767, level))
        await self._send(dst, 0x8207, struct.pack("<hB", level, tid))
        _LOGGER.debug("%s Level -> %d", self.name, level)

    async def set_ctl(self, dst: int, lightness: int, temperature: int) -> None:
        """Light CTL Set Unack. lightness: 0-65535, temperature: 800-20000K"""
        tid = (await self._seq_provider(self._src)) & 0xFF
        await self._send(
            dst, 0x8260,
            struct.pack("<HHHB", lightness & 0xFFFF, temperature & 0xFFFF, 0, tid),
        )
        _LOGGER.debug("%s CTL -> lightness=%d temp=%dK", self.name, lightness, temperature)

    # ------------------------------------------------------------------
    # Mesh commands — Get (used to sync initial state)
    # ------------------------------------------------------------------

    async def get_onoff(self, dst: int) -> None:
        """Generic OnOff Get (0x8201). Answered with OnOff Status (0x8204)."""
        await self._send(dst, 0x8201, b"")

    async def get_level(self, dst: int) -> None:
        """Generic Level Get (0x8205). Answered with Level Status (0x8208)."""
        await self._send(dst, 0x8205, b"")

    async def get_lightness(self, dst: int) -> None:
        """Light Lightness Get (0x824B). Answered with Lightness Status (0x824E)."""
        await self._send(dst, 0x824B, b"")

    async def get_ctl(self, dst: int) -> None:
        """Light CTL Get (0x825D). Answered with CTL Status (0x8260)."""
        await self._send(dst, 0x825D, b"")
