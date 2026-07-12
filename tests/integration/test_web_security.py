"""SameOriginUnsafeMethodMiddleware is deny-by-default: an unsafe method with
neither Origin nor Referer is blocked (that fallback used to fail open)."""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def _client() -> TestClient:
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://localhost:1234/v1"
    ctx.config.jarvis.llm.model = "m"
    ctx.config.jarvis.events.webhook_token = None
    ctx.dispatcher.dispatch_manual = AsyncMock(return_value=MagicMock(final_output="ok"))
    return TestClient(create_app(app_context=ctx))


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
