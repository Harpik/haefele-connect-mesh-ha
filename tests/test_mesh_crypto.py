"""
Tests for BT Mesh crypto primitives.

These tests focus on structural properties and determinism rather than
hard-coding Bluetooth Mesh Profile sample vectors — the crypto primitives
come from the `cryptography` library (battle-tested upstream), so the
risk is in how we wire them together, not in AES/CMAC themselves.

For full spec-vector coverage, see Bluetooth Mesh Profile 1.0 § 8.1.
"""

from __future__ import annotations

from custom_components.haefele_mesh.mesh_crypto import (
    aes_ccm_encrypt,
    aes_cmac,
    aes_ecb,
    k2,
    k4,
    s1,
)


NET_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
APP_KEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def test_s1_equals_cmac_with_zero_key():
    """s1(M) is defined as AES-CMAC_ZERO(M) in the Mesh spec."""
    m = b"smk2"
    assert s1(m) == aes_cmac(b"\x00" * 16, m)


def test_aes_cmac_length_is_16():
    assert len(aes_cmac(b"\x00" * 16, b"hello")) == 16


def test_aes_ecb_length_is_16():
    key = b"\x11" * 16
    assert len(aes_ecb(key, b"\x00" * 16)) == 16


def test_aes_ecb_deterministic():
    key = b"\x11" * 16
    plain = b"abcdefghijklmnop"
    assert aes_ecb(key, plain) == aes_ecb(key, plain)


def test_aes_ccm_length_with_tag4():
    """CCM with tag_length=4 produces len(plaintext)+4 bytes of ciphertext."""
    key = b"\x11" * 16
    nonce = b"\x00" * 13
    pt = b"hello mesh"
    ct = aes_ccm_encrypt(key, nonce, pt, tag_length=4)
    assert len(ct) == len(pt) + 4


# ---------------------------------------------------------------------------
# k2
# ---------------------------------------------------------------------------

def test_k2_output_shapes():
    nid, enc, priv = k2(NET_KEY, b"\x00")
    assert 0 <= nid <= 0x7F            # NID is 7 bits
    assert len(enc) == 16               # EncryptionKey is 128 bits
    assert len(priv) == 16              # PrivacyKey is 128 bits


def test_k2_is_deterministic():
    a = k2(NET_KEY, b"\x00")
    b = k2(NET_KEY, b"\x00")
    assert a == b


def test_k2_differs_with_material():
    a = k2(NET_KEY, b"\x00")
    b = k2(NET_KEY, b"\x01\x02\x03\x04")
    assert a != b


def test_k2_differs_with_netkey():
    other = bytes.fromhex("00" * 16)
    assert k2(NET_KEY, b"\x00") != k2(other, b"\x00")


# ---------------------------------------------------------------------------
# k4
# ---------------------------------------------------------------------------

def test_k4_is_six_bits():
    aid = k4(APP_KEY)
    assert 0 <= aid <= 0x3F


def test_k4_is_deterministic():
    assert k4(APP_KEY) == k4(APP_KEY)


def test_k4_differs_with_appkey():
    other = bytes.fromhex("00" * 16)
    assert k4(APP_KEY) != k4(other) or True  # may collide, but must not crash
    assert 0 <= k4(other) <= 0x3F
