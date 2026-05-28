"""Symmetric encryption for API secrets at rest using Fernet."""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from config import settings

logger = logging.getLogger("algopro.encryption")


def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from an arbitrary passphrase."""
    digest = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_key(settings.ENCRYPTION_KEY))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext API secret → base64 ciphertext string."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a ciphertext string back to the original API secret."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Cannot decrypt API secret — ENCRYPTION_KEY mismatch or corrupted data")
        raise ValueError("Cannot decrypt API secret — ENCRYPTION_KEY may have changed. Re-add the account.")
