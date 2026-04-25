"""
Bluetooth Mesh GATT Proxy Bearer for Häfele Connect Mesh.

Handles BLE connection, PDU segmentation, encryption and
sequence number management per BT Mesh spec.
"""

import asyncio
import logging
import os
import struct
import time
from typing import Optional

from bleak import BleakClient, BleakScanner

from .const import (
    MESH_PROXY_DATA_IN_UUID,
    MESH_PROXY_DATA_OUT_UUID,
)

_LOGGER = logging.getLogger(__name__)

SEQ_FILE = "/tmp/haefele_mesh_seq"


def _aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, tag_length: int = 4) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    cipher = AESCCM(key, tag_length=tag_length)
    return cipher.encrypt(nonce, plaintext, None)


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    e = cipher.encryptor()
    return e.update(data[:16]) + e.finalize()


def _aes_cmac(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.cmac import CMAC
    from cryptography.hazmat.primitives.ciphers import algorithms
    from cryptography.hazmat.backends import default_backend
    c = CMAC(algorithms.AES(key), backend=default_backend())
    c.update(data)
    return c.finalize()


def _s1(m: bytes) -> bytes:
    return _aes_cmac(b'\x00' * 16, m)


def _k2(n: bytes, p: bytes):
    """Derive NID, EncryptionKey, PrivacyKey from NetworkKey."""
    import bitstring
    salt = _s1(b"smk2")
    t = _aes_cmac(salt, n)
    t0 = b""
    t1 = _aes_cmac(t, t0 + p + b"\x01")
    t2 = _aes_cmac(t, t1 + p + b"\x02")
    t3 = _aes_cmac(t, t2 + p + b"\x03")
    k = (t1 + t2 + t3)[-33:]
    nid, enc, priv = bitstring.BitString(k).unpack("pad:1, uint:7, bits:128, bits:128")
    return nid, enc.bytes, priv.bytes


def _k4(n: bytes) -> int:
    """Derive AID from ApplicationKey."""
    import bitstring
    salt = _s1(b"smk4")
    t = _aes_cmac(salt, n)
    k = _aes_cmac(t, b"id6\x01")[-1:]
    (aid,) = bitstring.BitString(k).unpack("pad:2, uint:6")
    return aid


class MeshGattNode:
    """
    Controls a single Häfele Mesh node via GATT Proxy bearer.

    Handles BLE connection, PDU encryption/segmentation,
    and monotonic sequence numbers for anti-replay protection.
    """

    def __init__(
        self,
        mac: str,
        unicast: int,
        net_key_hex: str,
        app_key_hex: str,
        iv_index: int = 1,
        src_address: int = 0x0060,
        adapter: Optional[str] = None,
        name: str = "",
    ):
        self.mac = mac
        self.unicast = unicast
        self.name = name
        self._iv_index = iv_index
        self._src = src_address
        self._adapter = adapter  # e.g. "hci0", "hci1"

        # Crypto
        self._net_key = bytes.fromhex(net_key_hex)
        self._app_key = bytes.fromhex(app_key_hex)
        self._nid, self._enc_key, self._priv_key = _k2(self._net_key, b"\x00")
        self._aid = _k4(self._app_key)

        # SEQ number — load from file or use timestamp
        self._seq = self._load_seq()

        # BLE state
        self._client: Optional[BleakClient] = None
        self._data_in = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._reassembly_buf = bytearray()

    # ------------------------------------------------------------------
    # Sequence number
    # ------------------------------------------------------------------

    def _load_seq(self) -> int:
        try:
            with open(SEQ_FILE) as f:
                return (int(f.read().strip()) + 200) & 0xFFFFFF
        except Exception:
            return int(time.time()) & 0xFFFFFF

    def _save_seq(self):
        try:
            with open(SEQ_FILE, 'w') as f:
                f.write(str(self._seq))
        except Exception:
            pass

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFF
        self._save_seq()
        return self._seq

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, timeout: float = 20.0) -> bool:
        try:
            kwargs = {}
            if self._adapter:
                kwargs['adapter'] = self._adapter

            _LOGGER.debug("Scanning for %s (%s)...", self.name, self.mac)
            device = await BleakScanner.find_device_by_address(
                self.mac, timeout=timeout, **kwargs
            )
            if device is None:
                _LOGGER.warning("Device %s (%s) not found", self.name, self.mac)
                return False

            self._client = BleakClient(device, timeout=timeout)
            await self._client.connect()

            self._data_in = self._client.services.get_characteristic(MESH_PROXY_DATA_IN_UUID)
            data_out = self._client.services.get_characteristic(MESH_PROXY_DATA_OUT_UUID)

            if not self._data_in or not data_out:
                _LOGGER.error("Mesh Proxy characteristics not found on %s", self.mac)
                return False

            await self._client.start_notify(data_out, self._on_notification)
            _LOGGER.info("Connected to %s (%s)", self.name, self.mac)
            return True

        except Exception as err:
            _LOGGER.error("Failed to connect to %s: %s", self.name, err)
            return False

    async def disconnect(self):
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        _LOGGER.debug("Disconnected from %s", self.name)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def ensure_connected(self, timeout: float = 20.0) -> bool:
        if not self.is_connected:
            return await self.connect(timeout=timeout)
        return True

    # ------------------------------------------------------------------
    # PDU segmentation / reassembly (BT Mesh 6.6.2)
    # ------------------------------------------------------------------

    def _on_notification(self, _, data: bytearray):
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

    async def _send_proxy_pdu(self, pdu: bytes, pdu_type: int = 0x00):
        max_chunk = 19
        chunks = [pdu[i:i+max_chunk] for i in range(0, len(pdu), max_chunk)]
        for i, chunk in enumerate(chunks):
            if len(chunks) == 1:       sar = 0x00
            elif i == 0:               sar = 0x40
            elif i == len(chunks)-1:   sar = 0xC0
            else:                      sar = 0x80
            packet = bytes([sar | (pdu_type & 0x3F)]) + chunk
            await self._client.write_gatt_char(self._data_in, packet, response=False)
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Network PDU construction (BT Mesh 3.4 + 3.6 + 3.8)
    # ------------------------------------------------------------------

    def _build_network_pdu(self, dst: int, opcode: int, params: bytes) -> bytes:
        seq = self._next_seq()
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
        upper_transport = _aes_ccm_encrypt(self._app_key, app_nonce, access_pdu, tag_length=4)
        trans_pdu = bytes([(1 << 6) | (self._aid & 0x3F)]) + upper_transport

        net_nonce = (
            bytes([0x00, (ctl << 7) | (ttl & 0x7F)])
            + seq.to_bytes(3, "big")
            + struct.pack(">H", self._src)
            + bytes([0x00, 0x00])
            + struct.pack(">I", self._iv_index)
        )
        plaintext = struct.pack(">H", dst) + trans_pdu
        encrypted = _aes_ccm_encrypt(self._enc_key, net_nonce, plaintext, tag_length=4)

        privacy_plaintext = b"\x00" * 5 + struct.pack(">I", self._iv_index) + encrypted[:7]
        pecb = _aes_ecb(self._priv_key, privacy_plaintext)
        cleartext_header = bytes([(ctl << 7) | (ttl & 0x7F)]) + seq.to_bytes(3, "big") + struct.pack(">H", self._src)
        obfuscated = bytes(a ^ b for a, b in zip(cleartext_header, pecb[:6]))

        ivi_nid = ((self._iv_index & 1) << 7) | (self._nid & 0x7F)
        return bytes([ivi_nid]) + obfuscated + encrypted

    async def _send(self, dst: int, opcode: int, params: bytes):
        if not await self.ensure_connected():
            raise ConnectionError(f"Cannot connect to {self.name}")
        pdu = self._build_network_pdu(dst, opcode, params)
        await self._send_proxy_pdu(pdu)

    # ------------------------------------------------------------------
    # Mesh commands
    # ------------------------------------------------------------------

    async def set_onoff(self, dst: int, onoff: bool):
        tid = self._next_seq() & 0xFF
        await self._send(dst, 0x8203, struct.pack("BB", int(onoff), tid))
        _LOGGER.debug("%s OnOff -> %s", self.name, "ON" if onoff else "OFF")

    async def set_level(self, dst: int, level: int):
        """Generic Level Set Unack. level: -32768 to 32767"""
        tid = self._next_seq() & 0xFF
        await self._send(dst, 0x8207, struct.pack("<hB", max(-32768, min(32767, level)), tid))
        _LOGGER.debug("%s Level -> %d", self.name, level)

    async def set_ctl(self, dst: int, lightness: int, temperature: int):
        """Light CTL Set Unack. lightness: 0-65535, temperature: 800-20000K"""
        tid = self._next_seq() & 0xFF
        await self._send(dst, 0x8260, struct.pack("<HHHB", lightness & 0xFFFF, temperature & 0xFFFF, 0, tid))
        _LOGGER.debug("%s CTL -> lightness=%d temp=%dK", self.name, lightness, temperature)
