"""
Bluetooth Mesh cryptographic primitives used by the Häfele integration.

Extracted into a separate pure-Python module (no Home Assistant or Bleak
imports) so the crypto can be unit-tested in isolation against the
BT Mesh spec test vectors.
"""

from __future__ import annotations


def aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, tag_length: int = 4) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    cipher = AESCCM(key, tag_length=tag_length)
    return cipher.encrypt(nonce, plaintext, None)


def aes_ecb(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    e = cipher.encryptor()
    return e.update(data[:16]) + e.finalize()


def aes_cmac(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.cmac import CMAC
    from cryptography.hazmat.primitives.ciphers import algorithms
    from cryptography.hazmat.backends import default_backend
    c = CMAC(algorithms.AES(key), backend=default_backend())
    c.update(data)
    return c.finalize()


def s1(m: bytes) -> bytes:
    """Mesh Profile spec 3.8.2.4: s1(M) = AES-CMAC_ZERO(M)."""
    return aes_cmac(b"\x00" * 16, m)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Mesh Profile spec 3.8.2.6: derive (NID, EncryptionKey, PrivacyKey) from a NetKey."""
    import bitstring

    salt = s1(b"smk2")
    t = aes_cmac(salt, n)
    t0 = b""
    t1 = aes_cmac(t, t0 + p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    k = (t1 + t2 + t3)[-33:]
    nid, enc, priv = bitstring.BitString(k).unpack("pad:1, uint:7, bits:128, bits:128")
    return nid, enc.bytes, priv.bytes


def k4(n: bytes) -> int:
    """Mesh Profile spec 3.8.2.8: derive AID (6-bit) from an AppKey."""
    import bitstring

    salt = s1(b"smk4")
    t = aes_cmac(salt, n)
    k = aes_cmac(t, b"id6\x01")[-1:]
    (aid,) = bitstring.BitString(k).unpack("pad:2, uint:6")
    return aid
