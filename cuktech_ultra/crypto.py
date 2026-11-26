"""MiBLE Standard Auth (auth_type=4) key derivation and AES-128-CCM session cipher."""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

LOGIN_HKDF_INFO = b"mible-login-info"
MIC_TAG_LENGTH = 4


class CryptoError(Exception):
    """Raised on authentication confirmation or AES-CCM decryption failure."""


@dataclass(frozen=True)
class SessionKeys:
    """64-byte HKDF-SHA256 output sliced per MiBLE Standard Auth spec."""

    dev_key: bytes  # okm[0:16]  - device -> app
    app_key: bytes  # okm[16:32] - app -> device
    dev_iv: bytes   # okm[32:36] - device -> app 4-byte IV prefix
    app_iv: bytes   # okm[36:40] - app -> device 4-byte IV prefix
    extra: bytes    # okm[40:64] - reserved 24 bytes


def parse_token_hex(token_hex: str) -> bytes:
    """Parse a hex-encoded MiBLE device token (12 or 16 bytes)."""
    cleaned = token_hex.strip().replace(":", "").replace("-", "").replace(" ", "")
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise CryptoError(f"Invalid hex token: {exc}") from exc
    if len(raw) not in (12, 16):
        raise CryptoError(f"Expected 12-byte or 16-byte device token, got {len(raw)} bytes")
    return raw


def generate_app_rand() -> bytes:
    """Generate 16 cryptographically random bytes for Standard Auth handshake."""
    return os.urandom(16)


def derive_session_keys(token: bytes, app_rand: bytes, dev_rand: bytes) -> SessionKeys:
    """Derive 64-byte session key material via HKDF-SHA256(ikm=token, salt=app_rand||dev_rand, info='mible-login-info')."""
    if len(app_rand) != 16 or len(dev_rand) != 16:
        raise CryptoError(f"app_rand and dev_rand must be 16 bytes (got {len(app_rand)}, {len(dev_rand)})")

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=app_rand + dev_rand,
        info=LOGIN_HKDF_INFO,
    )
    okm = hkdf.derive(token)
    return SessionKeys(
        dev_key=okm[0:16],
        app_key=okm[16:32],
        dev_iv=okm[32:36],
        app_iv=okm[36:40],
        extra=okm[40:64],
    )


def compute_dev_confirm(dev_key: bytes, dev_rand: bytes, app_rand: bytes) -> bytes:
    """Expected 32-byte HMAC-SHA256 sent by the charger (parcel type 0x0C)."""
    return hmac.new(dev_key, dev_rand + app_rand, hashlib.sha256).digest()


def compute_app_confirm(app_key: bytes, app_rand: bytes, dev_rand: bytes) -> bytes:
    """32-byte HMAC-SHA256 sent by the client to the charger (parcel type 0x0A)."""
    return hmac.new(app_key, app_rand + dev_rand, hashlib.sha256).digest()


def verify_dev_confirm(dev_key: bytes, dev_rand: bytes, app_rand: bytes, dev_confirm: bytes) -> bool:
    """Constant-time verification of the charger's confirmation HMAC."""
    expected = compute_dev_confirm(dev_key, dev_rand, app_rand)
    return hmac.compare_digest(expected, dev_confirm)


class MscCipher:
    """Stateful AES-128-CCM session cipher for MiBLE Standard Auth sessions."""

    def __init__(self, keys: SessionKeys, initial_app_counter: int = 0) -> None:
        self.keys = keys
        self._app_counter = initial_app_counter
        self._app_ccm = AESCCM(keys.app_key, tag_length=MIC_TAG_LENGTH)
        self._dev_ccm = AESCCM(keys.dev_key, tag_length=MIC_TAG_LENGTH)

    @property
    def app_counter(self) -> int:
        return self._app_counter

    def encrypt_to_device(self, plaintext: bytes) -> bytes:
        """Encrypt an outbound MIoT-BLE Spec frame.

        Nonce (12B): app_iv(4B) || 0x00000000(4B) || uint32_LE(counter)(4B)
        Output: uint16_LE(counter & 0xFFFF)(2B) || ciphertext || tag(4B)
        """
        counter = self._app_counter
        self._app_counter = (self._app_counter + 1) & 0xFFFFFFFF

        nonce = self.keys.app_iv + b"\x00\x00\x00\x00" + struct.pack("<I", counter)
        ct_with_tag = self._app_ccm.encrypt(nonce, plaintext, associated_data=None)
        return struct.pack("<H", counter & 0xFFFF) + ct_with_tag

    def decrypt_from_device(self, payload: bytes) -> bytes:
        """Decrypt an inbound MIoT-BLE Spec frame from characteristic 001A or 001B.

        Input: counter_lo2(2B LE) || ciphertext || tag(4B)
        Nonce (12B): dev_iv(4B) || 0x00000000(4B) || counter_lo2(2B) || 0x0000(2B)
        """
        if len(payload) <= 2 + MIC_TAG_LENGTH:
            raise CryptoError(f"Encrypted payload too short ({len(payload)} bytes)")

        counter_lo2 = payload[0:2]
        ct_with_tag = payload[2:]
        nonce = self.keys.dev_iv + b"\x00\x00\x00\x00" + counter_lo2 + b"\x00\x00"
        try:
            return self._dev_ccm.decrypt(nonce, ct_with_tag, associated_data=None)
        except Exception as exc:
            raise CryptoError(f"AES-CCM decryption failed: {exc}") from exc
