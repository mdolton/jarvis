"""PKCE and state generators."""

import base64
import hashlib

import pytest

from jarvis.oauth.pkce import generate_code_challenge, generate_code_verifier, generate_state


def test_verifier_url_safe_and_long():
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert all(c.isalnum() or c in "-._~" for c in v)


def test_verifier_length_validation():
    with pytest.raises(ValueError):
        generate_code_verifier(length=10)
    with pytest.raises(ValueError):
        generate_code_verifier(length=200)


def test_verifier_random():
    a = generate_code_verifier()
    b = generate_code_verifier()
    assert a != b


def test_challenge_is_sha256_of_verifier_base64url_no_padding():
    v = "test-verifier"
    c = generate_code_challenge(v)
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expected


def test_state_is_url_safe_and_random():
    s1 = generate_state()
    s2 = generate_state()
    assert s1 != s2
    assert len(s1) >= 32
    assert all(c.isalnum() or c in "-_" for c in s1)
