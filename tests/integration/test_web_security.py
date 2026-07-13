"""Edge middleware: same-origin unsafe-method denial (deny-by-default — the
headers-absent fallback used to fail open), the security-header stamp, and
the Host allow-list."""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.config.schema import AuthConfig
from jarvis.web.app import create_app
from jarvis.web.security import CONTENT_SECURITY_POLICY


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://localhost:1234/v1"
    ctx.config.jarvis.llm.model = "m"
    ctx.config.jarvis.events.webhook_token = None
    ctx.dispatcher.dispatch_manual = AsyncMock(return_value=MagicMock(final_output="ok"))
    return ctx


def _client(ctx: MagicMock | None = None) -> TestClient:
    return TestClient(create_app(app_context=ctx or _ctx()))


def test_unsafe_method_without_origin_or_referer_is_blocked():
    resp = _client().post("/manual-runs", data={"prompt": "hi"})
    assert resp.status_code == 403
    assert "cross-origin" in resp.text


def test_unsafe_method_with_matching_origin_passes():
    resp = _client().post(
        "/manual-runs",
        data={"prompt": "hi"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 200


def test_unsafe_method_with_matching_referer_passes():
    resp = _client().post(
        "/manual-runs",
        data={"prompt": "hi"},
        headers={"referer": "http://testserver/"},
    )
    assert resp.status_code == 200


def test_unsafe_method_with_foreign_origin_is_blocked():
    resp = _client().post(
        "/manual-runs",
        data={"prompt": "hi"},
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_safe_methods_unaffected():
    assert _client().get("/healthz").status_code == 200


def test_events_webhook_exempt_from_origin_check():
    # Machine endpoint, Bearer-authed: with no token configured it must reach
    # the route and 404 (feature off) — not be 403'd for a missing Origin.
    resp = _client().post("/events/webhook", content=b"{}")
    assert resp.status_code == 404


# -- security headers --------------------------------------------------


def test_every_response_carries_the_security_headers():
    resp = _client().get("/healthz")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_csp_has_no_unsafe_directives_and_blocks_framing():
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "form-action 'self'" in CONTENT_SECURITY_POLICY


def test_middleware_rejections_also_carry_the_headers():
    # The header stamp is OUTERMOST: even a same-origin 403 gets it.
    resp = _client().post("/manual-runs", data={"prompt": "hi"})
    assert resp.status_code == 403
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_only_with_secure_cookies():
    plain = _client().get("/healthz")
    assert "Strict-Transport-Security" not in plain.headers  # mocked ctx: no TLS signal

    ctx = _ctx()
    ctx.config.jarvis.auth = AuthConfig(secure_cookies=True)
    tls = _client(ctx).get("/healthz")
    assert tls.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"


# -- trusted hosts ------------------------------------------------------


def _trusted_client() -> TestClient:
    ctx = _ctx()
    ctx.config.jarvis.trusted_hosts = ["jarvis.example.com"]
    return _client(ctx)


def test_foreign_host_header_is_rejected_when_trusted_hosts_set():
    resp = _trusted_client().get("/healthz", headers={"host": "evil.example"})
    assert resp.status_code == 400


def test_configured_and_loopback_hosts_are_allowed():
    client = _trusted_client()
    assert client.get("/healthz", headers={"host": "jarvis.example.com"}).status_code == 200
    # Loopback stays reachable for the in-container Docker healthcheck.
    assert client.get("/healthz", headers={"host": "localhost"}).status_code == 200


def test_no_trusted_hosts_config_means_no_host_filtering():
    resp = _client().get("/healthz", headers={"host": "whatever.example"})
    assert resp.status_code == 200
