"""Fernet wrapper: load key from env, roundtrip, key-change invalidates."""

import pytest
from cryptography.fernet import InvalidToken

from jarvis.oauth.crypto import (
    SecretsKeyMissing,
    decrypt_blob,
    encrypt_blob,
    generate_key,
    load_secrets_key,
)


def test_generate_key_returns_url_safe_44_byte_string():
    key = generate_key()
    assert isinstance(key, str)
    assert len(key) == 44  # Fernet keys are 32 raw bytes = 44 url-safe chars


def test_load_secrets_key_reads_env(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("JARVIS_SECRETS_KEY", key)
    assert load_secrets_key() == key.encode()


def test_load_secrets_key_missing_raises(monkeypatch):
    monkeypatch.delenv("JARVIS_SECRETS_KEY", raising=False)
    with pytest.raises(SecretsKeyMissing):
        load_secrets_key()


def test_encrypt_decrypt_roundtrip():
    key = generate_key().encode()
    plaintext = b"my-access-token-abc"
    cipher = encrypt_blob(plaintext, key)
    assert cipher != plaintext
    assert decrypt_blob(cipher, key) == plaintext


def test_decrypt_with_different_key_fails():
    key_a = generate_key().encode()
    key_b = generate_key().encode()
    cipher = encrypt_blob(b"secret", key_a)
    with pytest.raises(InvalidToken):
        decrypt_blob(cipher, key_b)
