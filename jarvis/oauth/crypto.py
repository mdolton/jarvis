"""Fernet wrapper used to encrypt OAuth tokens and client secrets at rest."""

import os

from cryptography.fernet import Fernet


class SecretsKeyMissing(RuntimeError):
    """JARVIS_SECRETS_KEY env var is unset."""


def generate_key() -> str:
    """Generate a fresh Fernet key as a url-safe string. For ops/setup only."""
    return Fernet.generate_key().decode()


def load_secrets_key() -> bytes:
    """Return the configured Fernet key as bytes. Raises if unset."""
    raw = os.environ.get("JARVIS_SECRETS_KEY")
    if not raw:
        raise SecretsKeyMissing(
            "JARVIS_SECRETS_KEY env var is required to encrypt OAuth credentials. "
            "Generate one with `python -c 'from jarvis.oauth.crypto import generate_key; print(generate_key())'`."
        )
    return raw.encode()


def encrypt_blob(plaintext: bytes, key: bytes) -> bytes:
    return Fernet(key).encrypt(plaintext)


def decrypt_blob(cipher: bytes, key: bytes) -> bytes:
    return Fernet(key).decrypt(cipher)
