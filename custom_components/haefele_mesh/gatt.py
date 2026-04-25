"""
Bluetooth Mesh GATT Proxy Bearer for Häfele Connect Mesh.

Handles BLE connection, PDU segmentation, encryption and
sequence number management per BT Mesh spec.

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
from .mesh_crypto import aes_ccm_encrypt, aes_ecb, k2, k4

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GATT Proxy node
# ---------------------------------------------------------------------------

SeqProvider = Callable[[int], Awaitable[int]]
"""Callback `(src_address) -> next_seq` owned by the coordinator."""


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
    ):
        self._hass = hass
        self.mac = mac.upper()
        self.unicast = unicast
        self.name = name or self.mac
        self._iv_index = iv_index
        self._src = src_address
        self._seq_provider = seq_provider

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

            _LOGGER.info("Connected to %s (%s)", self.name, self.mac)
            return True

    def _on_disconnected(self, _client) -> None:
        _LOGGER.debug("BLE disconnected from %s", self.name)

    async def _safe_disconnect(self) -> None:
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
    # PDU segmentation / reassembly (BT Mesh 6.6.2)
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

        if opcode <= 0x7E:
            access_pdu = bytes([opcode]) + params
        else:
            access_pdu = struct.pack(">H", opcode) + params

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
    # Mesh commands
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
