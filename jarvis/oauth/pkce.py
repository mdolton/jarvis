"""PKCE (RFC 7636) and OAuth state helpers."""

import base64
import hashlib
import secrets


def generate_code_verifier(*, length: int = 64) -> str:
    """Return a high-entropy url-safe verifier string of `length` chars."""
    if not (43 <= length <= 128):
        raise ValueError("PKCE verifier length must be 43..128 per RFC 7636")
    raw = secrets.token_urlsafe((length * 3) // 4 + 1)
    return raw[:length]


def generate_code_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state(*, n_bytes: int = 32) -> str:
    """Url-safe random state token, ~43 chars for n_bytes=32."""
    return secrets.token_urlsafe(n_bytes)
