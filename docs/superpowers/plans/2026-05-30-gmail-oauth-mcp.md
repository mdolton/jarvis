# Gmail OAuth MCP Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google's official Gmail MCP server as an OAuth provider in Jarvis by implementing the manual-mode (non-DCR) OAuth code path that the existing framework stubbed out.

**Architecture:** Reuse the existing `OAuthFlow` (discover → authorize → exchange → refresh → revoke) unchanged for the confidential-client token flow. Add a `MANUAL` branch that sources `client_id`/`client_secret` from environment variables instead of Dynamic Client Registration, reuse `discover()` against Google's `.well-known/openid-configuration`, and gate the RFC 8707 `resource` indicator behind a per-provider toggle. Fastmail (DCR) stays untouched.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy async, httpx (with `MockTransport` for tests), pytest (async), Fernet (cryptography). Spec: `docs/superpowers/specs/2026-05-30-gmail-oauth-mcp-design.md`.

**Commit convention:** This repo uses lowercase imperative subject lines with **no** `feat:`/`fix:` prefix (e.g. `include resource indicator (RFC 8707) in OAuth requests`). Every commit message ends with the trailer:
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```
Work on a feature branch (e.g. `gmail-oauth-mcp`), not `main`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `jarvis/oauth/catalog.py` | Provider registry + `ProviderEntry` shape | Add `client_id_env`, `client_secret_env`, `send_resource_indicator` fields; add `gmail` entry |
| `jarvis/oauth/flow.py` | OAuth state machine | Drop DCR guard in `discover()`; add `_resolve_manual_client()`; branch `start_authorization()` on `auth_mode`; gate `resource` param in three places |
| `jarvis/mcp/manager.py` | MCP server lifecycle | Relax `_bootstrap_oauth_catalog()` to attach manual providers at boot |
| `tests/unit/test_oauth_catalog.py` | Catalog assertions | Add Gmail-entry tests |
| `tests/integration/test_oauth_flow.py` | Flow behavior | Add manual-mode discover/start/resource tests |
| `tests/integration/test_mcp_manager_oauth.py` | Boot attach | Add manual-provider boot-attach test |
| `tests/integration/test_web_oauth.py` | Routes | Add `/oauth/connect/gmail` test |
| `README.md` | Operator docs | Rewrite OAuth section for Gmail + new env vars |

---

## Task 1: Extend `ProviderEntry` and add the Gmail catalog entry

**Files:**
- Modify: `jarvis/oauth/catalog.py`
- Test: `tests/unit/test_oauth_catalog.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_oauth_catalog.py`:

```python
def test_gmail_entry_present():
    entry = OAUTH_CATALOG["gmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.MANUAL
    assert entry.mcp_url == "https://gmailmcp.googleapis.com/mcp/v1"
    assert entry.oauth_metadata_url == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert entry.client_id_env == "GOOGLE_OAUTH_CLIENT_ID"
    assert entry.client_secret_env == "GOOGLE_OAUTH_CLIENT_SECRET"
    assert entry.scopes == (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    )
    assert entry.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert entry.send_resource_indicator is True


def test_provider_entry_defaults_for_manual_fields():
    # Fastmail leaves the new manual-mode fields at their defaults.
    entry = OAUTH_CATALOG["fastmail"]
    assert entry.client_id_env is None
    assert entry.client_secret_env is None
    assert entry.send_resource_indicator is True


def test_fastmail_entry_still_present():
    entry = OAUTH_CATALOG["fastmail"]
    assert entry.auth_mode == AuthMode.DCR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth_catalog.py -v`
Expected: FAIL — `KeyError: 'gmail'` and `AttributeError` for the new fields.

- [ ] **Step 3: Add the fields and the entry**

In `jarvis/oauth/catalog.py`, add three fields to `ProviderEntry` (after `pkce`):

```python
@dataclass(frozen=True, slots=True)
class ProviderEntry:
    key: str
    display_name: str
    mcp_url: str
    auth_mode: AuthMode
    oauth_metadata_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()
    pkce: bool = True
    # MANUAL mode: env vars holding operator-created client credentials.
    client_id_env: str | None = None
    client_secret_env: str | None = None
    # RFC 8707 resource indicator. Default on; flip off if a provider rejects it.
    send_resource_indicator: bool = True
```

Add the `gmail` entry to `OAUTH_CATALOG` (keep `fastmail`):

```python
    "gmail": ProviderEntry(
        key="gmail",
        display_name="Gmail",
        mcp_url="https://gmailmcp.googleapis.com/mcp/v1",
        auth_mode=AuthMode.MANUAL,
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth_catalog.py -v`
Expected: PASS (all, including the existing Fastmail/collision tests).

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/catalog.py tests/unit/test_oauth_catalog.py
git commit -m "add Gmail manual-mode OAuth provider to catalog"
```

---

## Task 2: Let `discover()` run for manual-mode providers

**Files:**
- Modify: `jarvis/oauth/flow.py:84-88` (the `discover` guard)
- Test: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_oauth_flow.py` (the `OAUTH_CATALOG`, `OAuthFlow`, `make_client` imports already exist at the top of the file):

```python
@pytest.fixture
def google_metadata_payload():
    # Mirrors accounts.google.com/.well-known/openid-configuration: no registration_endpoint.
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "code_challenge_methods_supported": ["plain", "S256"],
    }


async def test_discover_manual_mode_parses_google_metadata(google_metadata_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/openid-configuration"
        return httpx.Response(200, json=google_metadata_payload)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["gmail"])
    assert metadata.authorization_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
    assert metadata.token_endpoint == "https://oauth2.googleapis.com/token"
    assert metadata.registration_endpoint is None
    assert metadata.revocation_endpoint == "https://oauth2.googleapis.com/revoke"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_oauth_flow.py::test_discover_manual_mode_parses_google_metadata -v`
Expected: FAIL — `NotImplementedError: Manual-mode OAuth not yet supported for provider 'gmail'`.

- [ ] **Step 3: Remove the DCR-only guard**

In `jarvis/oauth/flow.py`, delete these lines at the start of `discover()`:

```python
        if entry.auth_mode is not AuthMode.DCR:
            raise NotImplementedError(
                f"Manual-mode OAuth not yet supported for provider {entry.key!r}"
            )
```

The method body now begins with the `if entry.oauth_metadata_url is None:` check. `registration_endpoint` is already read via `data.get(...)`, so a missing one yields `None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_oauth_flow.py -v`
Expected: PASS (new test plus all existing Fastmail discover tests).

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "allow OAuth discovery for manual-mode providers"
```

---

## Task 3: Source manual client credentials from the environment

**Files:**
- Modify: `jarvis/oauth/flow.py` (add `import os`, add `_resolve_manual_client`, branch `start_authorization`)
- Test: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_oauth_flow.py` (uses the existing `db_factory` fixture and `google_metadata_payload` from Task 2; `parse_qs`, `urlparse`, `generate_key`, `OAuthCredentialsRepo` are already imported):

```python
async def test_start_authorization_manual_seeds_client_from_env(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=google_metadata_payload)
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="https://jarvis.example/", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")

    parsed = urlparse(consent_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert qs["client_id"] == ["google-cid"]
    assert qs["redirect_uri"] == ["https://jarvis.example/oauth/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["scope"] == [
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose"
    ]

    # A sentinel credentials row was seeded with the env-sourced client_id.
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("gmail")
    assert cred is not None
    assert cred.client_id_enc != b""
    assert cred.access_token_enc == b""  # not yet authorized


async def test_start_authorization_manual_missing_env_raises(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    with pytest.raises(OAuthDiscoveryError, match="GOOGLE_OAUTH_CLIENT_ID"):
        await flow.start_authorization("gmail")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_oauth_flow.py -k manual -v`
Expected: FAIL — `start_authorization` calls `register_client`, which hits the mock `404` and raises `OAuthDiscoveryError` about DCR registration (wrong error), and the seed assertions don't hold.

- [ ] **Step 3: Add `import os` and the helper**

In `jarvis/oauth/flow.py`, add `import os` near the other stdlib imports (after `import logging`).

Add this method to `OAuthFlow` (place it just after `register_client`):

```python
    def _resolve_manual_client(self, entry: ProviderEntry) -> RegisteredClient:
        """Read operator-supplied client_id/secret from the environment.

        Manual-mode providers (e.g. Google) don't support DCR; the operator
        creates the OAuth client by hand and supplies its credentials via env.
        """
        if not entry.client_id_env:
            raise OAuthDiscoveryError(
                f"{entry.key}: manual-mode provider has no client_id_env configured"
            )
        client_id = os.environ.get(entry.client_id_env)
        if not client_id:
            raise OAuthDiscoveryError(
                f"{entry.key}: environment variable {entry.client_id_env} is not set"
            )
        client_secret = (
            os.environ.get(entry.client_secret_env) if entry.client_secret_env else None
        )
        return RegisteredClient(client_id=client_id, client_secret=client_secret)
```

- [ ] **Step 4: Branch `start_authorization` on auth mode**

In `start_authorization`, replace the single registration call inside the `if existing is None or not existing.client_id_enc:` block. Change:

```python
        if existing is None or not existing.client_id_enc:
            client = await self.register_client(entry, metadata)
```

to:

```python
        if existing is None or not existing.client_id_enc:
            if entry.auth_mode is AuthMode.MANUAL:
                client = self._resolve_manual_client(entry)
            else:
                client = await self.register_client(entry, metadata)
```

Everything else in the block (the encrypt + `upsert` of the sentinel row, `client_id = client.client_id`) stays exactly as-is — `RegisteredClient` is the shared return type.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_oauth_flow.py -v`
Expected: PASS (manual tests plus all existing Fastmail tests, which still take the DCR branch).

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "source manual-mode OAuth client credentials from env"
```

---

## Task 4: Gate the RFC 8707 resource indicator behind the catalog toggle

**Files:**
- Modify: `jarvis/oauth/flow.py` (three `resource` sites: `start_authorization`, `handle_callback`, `refresh`)
- Test: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_oauth_flow.py`:

```python
async def test_resource_indicator_omitted_when_disabled(
    db_factory, google_metadata_payload, monkeypatch
):
    import dataclasses
    from jarvis.oauth import catalog as catalog_mod

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    # Swap in a gmail entry with the toggle off, without mutating the frozen global.
    patched = dataclasses.replace(
        catalog_mod.OAUTH_CATALOG["gmail"], send_resource_indicator=False
    )
    monkeypatch.setitem(catalog_mod.OAUTH_CATALOG, "gmail", patched)

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")
    qs = parse_qs(urlparse(consent_url).query)
    assert "resource" not in qs


async def test_resource_indicator_present_by_default(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")
    qs = parse_qs(urlparse(consent_url).query)
    assert qs["resource"] == ["https://gmailmcp.googleapis.com/mcp/v1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_oauth_flow.py -k resource_indicator -v`
Expected: FAIL on `test_resource_indicator_omitted_when_disabled` — `resource` is still present because it is set unconditionally. (`test_resource_indicator_present_by_default` already passes.)

- [ ] **Step 3: Gate the three resource sites**

In `start_authorization`, the `params` dict currently includes `"resource": entry.mcp_url,` in its literal. Remove that key from the literal and instead, after `params.update(entry.extra_auth_params)`, add:

```python
        if entry.send_resource_indicator:
            params["resource"] = entry.mcp_url
```

In `handle_callback`, the `form` dict literal includes `"resource": entry.mcp_url,`. Remove it from the literal and, immediately after the `form = {...}` assignment, add:

```python
        if entry.send_resource_indicator:
            form["resource"] = entry.mcp_url
```

In `refresh`, the `form` dict literal includes `"resource": entry.mcp_url,`. Remove it from the literal and, immediately after the `form = {...}` assignment, add:

```python
        if entry.send_resource_indicator:
            form["resource"] = entry.mcp_url
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_oauth_flow.py -v`
Expected: PASS. Existing Fastmail tests still see `resource` (Fastmail keeps the default `True`).

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "gate RFC 8707 resource indicator behind per-provider toggle"
```

---

## Task 5: Attach manual-mode providers at boot

**Files:**
- Modify: `jarvis/mcp/manager.py:71-73` (the `_bootstrap_oauth_catalog` loop)
- Test: `tests/integration/test_mcp_manager_oauth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_mcp_manager_oauth.py` (imports `datetime`, `timedelta`, `UTC`, `encrypt_blob`, `generate_key`, `OAuthCredentialsRepo`, `MCPManager`, `MCPServersConfig`, `FakeSDKServer` all already present):

```python
async def test_start_attaches_connected_manual_provider(factory, monkeypatch):
    """A connected manual-mode (gmail) row must be attached at boot, not skipped."""
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="gmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=encrypt_blob(b"sec", key),
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )

    captured = {}
    sdk = FakeSDKServer()

    def fake_build(url, headers, *, name):
        captured["url"] = url
        return sdk

    monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", fake_build)

    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory, secrets_key=key)
    await mgr.start()
    try:
        assert sdk.entered
        assert mgr.agent_mcp_servers() == [sdk]
        assert captured["url"] == "https://gmailmcp.googleapis.com/mcp/v1"
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_manager_oauth.py::test_start_attaches_connected_manual_provider -v`
Expected: FAIL — `sdk.entered` is `False` and `agent_mcp_servers() == []`, because the boot loop skips non-DCR providers.

- [ ] **Step 3: Remove the DCR-only skip**

In `jarvis/mcp/manager.py`, inside `_bootstrap_oauth_catalog`, delete:

```python
            if entry.auth_mode is not AuthMode.DCR:
                continue
```

The loop now attaches every catalog provider whose credentials row is `status='connected'` with a non-empty access token. If `AuthMode` becomes unused in this file after the deletion, remove it from the `from jarvis.oauth.catalog import ...` line to satisfy linting; otherwise leave the import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_manager_oauth.py -v`
Expected: PASS (new test plus the existing Fastmail boot-attach and skip-without-credentials tests).

- [ ] **Step 5: Commit**

```bash
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager_oauth.py
git commit -m "attach manual-mode OAuth providers at boot"
```

---

## Task 6: Web route coverage for `/oauth/connect/gmail`

**Files:**
- Test only: `tests/integration/test_web_oauth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_web_oauth.py` (mirror the existing Fastmail connect test; `_Ctx`, `make_app`, `factory`, `OAuthFlow`, `generate_key`, `parse_qs`, `urlparse`, `httpx` are already imported at the top):

```python
def google_metadata():
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "code_challenge_methods_supported": ["plain", "S256"],
    }


async def test_connect_gmail_redirects_to_google(factory, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        return httpx.Response(200, json=google_metadata())

    key = generate_key().encode()
    flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="https://jarvis.example",
        secrets_key=key,
    )
    ctx = _Ctx(factory, flow)
    client = make_app(ctx)

    resp = client.get("/oauth/connect/gmail", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    qs = parse_qs(urlparse(location).query)
    assert urlparse(location).netloc == "accounts.google.com"
    assert qs["client_id"] == ["google-cid"]
    assert qs["redirect_uri"] == ["https://jarvis.example/oauth/callback"]
    assert qs["access_type"] == ["offline"]
```

If the existing Fastmail web tests follow a different `OAuthFlow`/`_Ctx` wiring (e.g. a shared fixture), match that pattern instead of constructing `OAuthFlow` inline — read the top Fastmail connect test in this file first and mirror it exactly.

- [ ] **Step 2: Run test to verify it fails (then passes)**

Run: `uv run pytest tests/integration/test_web_oauth.py::test_connect_gmail_redirects_to_google -v`
Expected: PASS once the routes resolve `gmail` from the catalog (the connect route is already generic — `if provider not in OAUTH_CATALOG`). If it fails, the failure points to a wiring mismatch to fix in the test, not in app code.

- [ ] **Step 3: Run the full OAuth/web suite**

Run: `uv run pytest tests/integration/test_web_oauth.py -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_web_oauth.py
git commit -m "test /oauth/connect for Gmail manual-mode provider"
```

---

## Task 7: Operator documentation

**Files:**
- Modify: `README.md` (the "OAuth-protected MCP servers" section, ~lines 120-151)

- [ ] **Step 1: Rewrite the OAuth section**

Replace the body of the "OAuth-protected MCP servers" section in `README.md` so it documents both providers and the Gmail setup. Use this content:

````markdown
## OAuth-protected MCP servers

Jarvis connects to OAuth-protected HTTP MCP servers from the `/mcp` dashboard. Two
providers ship in the catalog:

- **Fastmail** — Dynamic Client Registration (DCR); no manual client setup.
- **Gmail** — Google's official Gmail MCP server (`https://gmailmcp.googleapis.com/mcp/v1`).
  Google does not support DCR, so you create the OAuth client by hand and supply its
  credentials via environment variables.

### One-time setup (all providers)

1. Generate a Fernet secrets key:
   ```bash
   uv run python -c "from jarvis.oauth.crypto import generate_key; print(generate_key())"
   ```
2. Set the base env vars (in `.env` for Docker, or your shell for local dev):
   ```
   JARVIS_SECRETS_KEY=<paste-the-key>
   JARVIS_BASE_URL=https://jarvis.moltonlava.online   # or http://localhost:8080 locally
   ```

### Gmail-specific setup

In Google Cloud Console:

1. Enable the **Gmail API** and request access to the **Gmail MCP server** (early access).
2. Configure the **OAuth consent screen** with scopes
   `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.compose`. While the app is unverified, add your
   Google account as a **test user**.
3. Create an **OAuth client ID** of type **Web application**:
   - **Authorized redirect URI:** `https://jarvis.moltonlava.online/oauth/callback`
     (must equal `${JARVIS_BASE_URL}/oauth/callback` exactly).
   - **Authorized JavaScript origins:** leave empty — Jarvis uses a server-side code flow.

Then add the client credentials to Jarvis's environment:
```
GOOGLE_OAUTH_CLIENT_ID=<from Google Cloud Console>
GOOGLE_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
```

Restart Jarvis, open `/mcp`, and click **Connect** on the Gmail card. Jarvis stores the
encrypted tokens and refreshes them automatically; if a refresh permanently fails the card
shows **Needs re-auth** with a Reconnect button.
````

- [ ] **Step 2: Verify the doc renders and links are consistent**

Run: `grep -n "gmailmcp.googleapis.com\|GOOGLE_OAUTH_CLIENT" README.md`
Expected: shows the MCP URL and both env var names in the new section.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "document Gmail OAuth MCP setup"
```

---

## Task 8: Full suite + manual end-to-end gate

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions. If any Fastmail-related test broke, the manual-mode changes leaked into the DCR path — fix before proceeding.

- [ ] **Step 2: Run linters/type checks as configured**

Run the project's configured checks (e.g. `uv run ruff check .` and, if present, `uv run mypy jarvis`).
Expected: clean. Resolve any unused-import warnings (notably `AuthMode` in `manager.py` if it became unused in Task 5).

- [ ] **Step 3: Manual end-to-end against the live Gmail MCP server (pre-merge gate)**

This is the only thing that proves manual-mode OAuth + the `resource`-indicator decision works against Google. With `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`/`JARVIS_BASE_URL`/`JARVIS_SECRETS_KEY` set and Jarvis running behind `https://jarvis.moltonlava.online`:

1. Open `/mcp`, click **Connect** on the Gmail card, complete Google consent.
2. Confirm the callback succeeds and the card flips to **Connected** with the Gmail tools listed.
   - **If Google returns `invalid_request`/`invalid_target`** at the authorization or token step, the `resource` indicator is the cause: set `send_resource_indicator=False` on the `gmail` catalog entry, re-run Tasks 1 and 4's tests, and retry the Connect cycle. Record the outcome.
3. Trigger an agent run that uses a Gmail tool (e.g. search threads) to confirm `list_tools` and a real call work.
4. Wait for the `oauth_token_refresh` scheduler tick to refresh the token (or confirm a refresh in logs); verify the card stays Connected with an updated timestamp.
5. Click **Disconnect**; confirm the card returns to Disconnected and the credentials row is gone.
6. Paste the logs and a `/mcp` screenshot into the PR.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin gmail-oauth-mcp
gh pr create --title "Add Gmail OAuth MCP provider (manual-mode OAuth)" \
  --body "Implements the manual-mode OAuth path and adds Google's Gmail MCP server. See docs/superpowers/specs/2026-05-30-gmail-oauth-mcp-design.md. Includes the required live Gmail Connect→refresh→Disconnect verification (logs + screenshot below)."
```

---

## Self-Review Notes

- **Spec coverage:** catalog fields + Gmail entry (Task 1); `discover()` guard removal (Task 2); env-sourced manual client + `start_authorization` branch (Task 3); `resource` toggle across all three request builders (Task 4); manual-provider boot attach (Task 5); web route coverage (Task 6); docs + Google Console setup (Task 7); no-DB-migration confirmed (not needed); manual E2E gate replacing the Fastmail gate (Task 8). Token exchange/refresh/revoke for confidential clients need no code change — already implemented via `client_secret_basic`.
- **Fastmail untouched:** every task keeps the DCR branch and asserts existing Fastmail tests still pass.
- **Type consistency:** `_resolve_manual_client` returns the same `RegisteredClient` as `register_client`, so the shared upsert block in `start_authorization` is unchanged. Catalog field names (`client_id_env`, `client_secret_env`, `send_resource_indicator`) are used identically in flow and tests.
