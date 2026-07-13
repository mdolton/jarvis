"""request.client.host through a real uvicorn with X-Forwarded-For in play.

PR 3's per-IP rate limiting keys off request.client.host, which is only real
if uvicorn trusts X-Forwarded-For from the reverse proxy's IP ALONE
(jarvis.forwarded_allow_ips). These tests run an actual uvicorn server and
prove both directions:

- peer is NOT the trusted proxy → a spoofed X-Forwarded-For is ignored and
  the socket peer address wins (the attacker cannot launder their IP);
- peer IS the trusted proxy → X-Forwarded-For is honored and handlers see
  the true client address.

uvicorn's DEFAULT forwarded_allow_ips is loopback — which trusts every
locally-proxied request. That's exactly why the config schema rejects "*"
and the docs say "the proxy's IP only"; a regression that stops passing the
config through (see test_cli.py::test_serve_passes_forwarded_allow_ips)
or loosens it re-opens IP spoofing.
"""

import asyncio

import httpx
import uvicorn
from fastapi import FastAPI, Request

SPOOFED = "203.0.113.99"


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ip")
    async def ip(request: Request):
        return {"client_host": request.client.host if request.client else None}

    return app


async def _client_host_seen(forwarded_allow_ips: str) -> str:
    config = uvicorn.Config(
        _echo_app(),
        host="127.0.0.1",
        port=0,  # ephemeral
        log_level="error",
        lifespan="off",
        forwarded_allow_ips=forwarded_allow_ips,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 — uvicorn exposes no startup event
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/ip",
                headers={"X-Forwarded-For": SPOOFED},
            )
        return resp.json()["client_host"]
    finally:
        server.should_exit = True
        await task


async def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored():
    # The trusted proxy is some other machine; we connect from loopback and
    # try to spoof. The socket peer address must win.
    seen = await _client_host_seen(forwarded_allow_ips="198.51.100.7")
    assert seen == "127.0.0.1"


async def test_forwarded_for_from_the_trusted_proxy_is_honored():
    # We ARE the proxy (loopback is trusted): the forwarded client is real.
    seen = await _client_host_seen(forwarded_allow_ips="127.0.0.1")
    assert seen == SPOOFED
