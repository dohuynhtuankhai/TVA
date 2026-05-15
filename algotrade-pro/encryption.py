"""Symmetric encryption for API secrets at rest using Fernet."""

import base64
import hashlib

from cryptography.fernet import Fernet

from config import settings


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
    return _fernet.decrypt(ciphertext.encode()).decode()
