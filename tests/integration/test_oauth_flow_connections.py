"""Two connections on one provider authorize independently."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest_asyncio

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory

GOOGLE_META = {
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
    "code_challenge_methods_supported": ["S256"],
}


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_two_calendar_connections_authorize_with_their_own_client(factory):
    key = generate_key().encode()

    def handler(request):
        if request.url.path.endswith(".well-known/openid-configuration"):
            return httpx.Response(200, json=GOOGLE_META)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                             "expires_in": 3600, "scope": "a"})
        return httpx.Response(404)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(factory))

    async with factory() as s:
        repo = MCPConnectionRepo(s)
        work = await repo.create(provider_key="calendar", label="Work",
                                 runtime_name="calendar:work",
                                 client_id_enc=encrypt_blob(b"work-cid", key),
                                 client_secret_enc=encrypt_blob(b"work-sec", key),
                                 scopes=["a"])
        home = await repo.create(provider_key="calendar", label="Home",
                                 runtime_name="calendar:home",
                                 client_id_enc=encrypt_blob(b"home-cid", key),
                                 client_secret_enc=encrypt_blob(b"home-sec", key),
                                 scopes=["a", "b"])

    url_work = await flow.start_authorization(work.id)
    q_work = parse_qs(urlparse(url_work).query)
    assert q_work["client_id"] == ["work-cid"]
    assert q_work["scope"] == ["a"]

    url_home = await flow.start_authorization(home.id)
    q_home = parse_qs(urlparse(url_home).query)
    assert q_home["client_id"] == ["home-cid"]
    assert q_home["scope"] == ["a b"]
    assert q_work["state"] != q_home["state"]
