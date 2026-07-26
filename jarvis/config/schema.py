"""Pydantic schemas for YAML configs. Source of truth for file layout."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------
# jarvis.yaml
# --------------------------------------------------------------------

OutputMode = Literal["discord", "dashboard_only", "discord_if_noteworthy"]


class LLMConfig(_StrictModel):
    base_url: str
    api_key: str
    model: str
    # Budget for one whole non-streamed LLM turn (scheduled runs never stream),
    # so it must cover reasoning tokens and a cold model load, not just
    # time-to-first-token. JarvisConfig.idle_timeout_sec bounds the full run.
    request_timeout_sec: float = 600.0


class MemoryConfig(_StrictModel):
    enabled: bool = True
    recall_enabled: bool = True
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1536, ge=1)
    max_recalled_memories: int = Field(default=5, ge=0, le=20)
    min_relevance_score: float = Field(default=0.25, ge=0.0)
    preference_dedup_enabled: bool = True
    preference_dup_high_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    preference_dup_low_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    preference_dedup_max_judge_calls: int = Field(default=5, ge=0)
    # Document corpus (notes, PDFs) ingestion + retrieval.
    documents_folder: str | None = None
    document_chunk_chars: int = Field(default=1800, ge=200)
    document_chunk_overlap: int = Field(default=200, ge=0)
    max_document_results: int = Field(default=5, ge=0, le=20)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "MemoryConfig":
        if self.preference_dup_low_threshold >= self.preference_dup_high_threshold:
            raise ValueError(
                "preference_dup_low_threshold must be strictly less than "
                "preference_dup_high_threshold"
            )
        if self.document_chunk_overlap >= self.document_chunk_chars:
            raise ValueError(
                "document_chunk_overlap must be strictly less than document_chunk_chars"
            )
        return self


class EventsConfig(_StrictModel):
    """Inbound event webhook (POST /events/webhook).

    No token → the endpoint is disabled and returns 404. The token gates who
    may wake an agent turn, so senders are operator-trusted; event *content*
    stays untrusted regardless (reduced tool scope + provenance tagging).
    """

    webhook_token: str | None = None
    coalesce_window_sec: float = Field(default=30.0, ge=0.0)


class AuthConfig(_StrictModel):
    """Dashboard authentication (WebAuthn passkeys + emailed login codes).

    Schema-only for now: nothing is enforced until `enabled` is flipped on.
    Note rp_id and expected_origin are NOT interchangeable — rp_id is the
    registrable domain with no port; expected_origin is the full browser
    origin (scheme + host + port) asserted in WebAuthn responses.
    """

    enabled: bool = False
    # Closed allow-list of emails permitted to sign in. No open signup, ever.
    allowed_emails: list[str] = Field(default_factory=list)
    # Secure + __Host- prefixed session cookie. The __Host- prefix requires
    # Secure/Path=/ and no Domain, so the cookie will not set over plain http —
    # right behind TLS, wrong for local dev on http://localhost (set False there).
    secure_cookies: bool = True
    rp_id: str = "localhost"
    rp_name: str = "Jarvis"
    expected_origin: str = "http://localhost:8080"
    session_ttl_days: int = Field(default=30, ge=1)
    session_idle_timeout_days: int = Field(default=7, ge=1)
    code_ttl_minutes: int = Field(default=10, ge=1)
    step_up_window_minutes: int = Field(default=5, ge=1)
    # Global cap on outstanding (unconsumed, unexpired) login codes across
    # all users. The per-address/per-IP buckets bound the request RATE; this
    # bounds the standing attack surface (each live code is a guessable
    # secret). Generous for a closed allow-list — hitting it means abuse.
    max_inflight_codes: int = Field(default=20, ge=1)


class MailConfig(_StrictModel):
    """Outbound mail for login codes.

    Mailtrap is TWO products, and wiring the wrong one fails silently:
    - Email SANDBOX (sandbox.smtp.mailtrap.io) is a FAKE SMTP server. It
      captures mail into a web inbox and never delivers it — every send
      returns 250 OK while no login code ever reaches anyone.
    - Email SENDING (live.smtp.mailtrap.io / send.api.mailtrap.io) is the
      real transactional delivery product.
    The validators below keep the two unmistakably distinct and refuse to
    start production (auth.enabled) pointed at the sandbox — see
    JarvisConfig._no_sandbox_mail_when_auth_enabled.

    Live sending only works from a VERIFIED domain (from_addr must be on
    it). An unverified demo domain can only mail the Mailtrap registration
    address — fine for one person, silently broken for the second.
    """

    provider: Literal["mailtrap_api", "mailtrap_smtp", "console"] = "console"
    # Mailtrap API token (${JARVIS_MAILTRAP_TOKEN}). Auth header per
    # https://docs.mailtrap.io/developers/authentication: either
    # `Api-Token: <token>` or `Authorization: Bearer <token>`.
    api_token: str | None = None
    # SMTP path only. sandbox.smtp.mailtrap.io captures; live.smtp.mailtrap.io
    # delivers. The `sandbox` flag must agree with the host chosen here.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    # Live SMTP authenticates as user "api" with the api_token as password;
    # sandbox inboxes have their own credentials. Both default from api_token.
    smtp_username: str | None = None
    smtp_password: str | None = None
    # Sender address — must be on the verified sending domain or nothing sends.
    from_addr: str = "jarvis@localhost"
    # Explicit declaration that mail is being CAPTURED, not delivered.
    sandbox: bool = False

    @model_validator(mode="after")
    def _mailtrap_products_kept_distinct(self) -> "MailConfig":
        if self.provider == "mailtrap_api":
            if not self.api_token:
                raise ValueError("mail.provider mailtrap_api requires mail.api_token")
            if self.sandbox:
                raise ValueError(
                    "mail.sandbox is only supported over SMTP (sandbox.smtp.mailtrap.io); "
                    "the send API is live delivery only — use mailtrap_smtp or console"
                )
        if self.provider == "mailtrap_smtp":
            if not self.smtp_host:
                raise ValueError("mail.provider mailtrap_smtp requires mail.smtp_host")
            looks_sandbox = "sandbox" in self.smtp_host
            if looks_sandbox and not self.sandbox:
                raise ValueError(
                    f"mail.smtp_host {self.smtp_host!r} is the Mailtrap SANDBOX: it captures "
                    "mail and NEVER delivers it. If that is intentional (local dev) set "
                    "mail.sandbox: true; production must use live.smtp.mailtrap.io"
                )
            if self.sandbox and not looks_sandbox:
                raise ValueError(
                    f"mail.sandbox is true but smtp_host {self.smtp_host!r} does not look "
                    "like a sandbox host — pick one: the flag and the host must agree"
                )
            if not (self.smtp_password or self.api_token):
                raise ValueError(
                    "mail.provider mailtrap_smtp requires mail.smtp_password or mail.api_token"
                )
        return self


class JarvisConfig(_StrictModel):
    llm: LLMConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    timezone: str = "UTC"
    # Free-text home location ("Austin, Texas") surfaced to every agent run as
    # user context, so briefs can fetch a local weather forecast without a
    # hardcoded city. None → the location line is simply omitted.
    home_location: str | None = None
    idle_timeout_sec: int = 900
    max_concurrent_agents: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_schedule_output_mode: OutputMode = "discord"
    # Max unsolicited pings per UTC day (P1 bypasses but still spends allowance);
    # sub-threshold notifications roll into the next scheduled digest.
    notification_daily_budget: int = Field(default=5, ge=1)
    # Which peers uvicorn trusts to set X-Forwarded-For — the reverse proxy's
    # IP ONLY. Trusting "*" lets anyone spoof their client IP and walk past
    # per-IP login rate limiting. None keeps uvicorn's own safe default
    # (loopback only).
    forwarded_allow_ips: str | None = None
    # Host header allow-list (the public domain, e.g. ["jarvis.example.com"]).
    # None disables the check (local dev). Loopback names are always allowed
    # on top so the in-container Docker healthcheck keeps working.
    trusted_hosts: list[str] | None = None

    @model_validator(mode="after")
    def _proxy_and_mail_traps(self) -> "JarvisConfig":
        if self.forwarded_allow_ips is not None and "*" in self.forwarded_allow_ips:
            raise ValueError(
                "forwarded_allow_ips must be the reverse proxy's IP(s), never '*': "
                "trusting X-Forwarded-For from anyone makes per-IP rate limiting spoofable"
            )
        if self.auth.enabled and self.mail.sandbox:
            raise ValueError(
                "auth.enabled with mail.sandbox: the Mailtrap sandbox CAPTURES mail and "
                "never delivers it, so every login code would silently vanish while sends "
                "report success. Point mail at Email Sending (live) before enabling auth."
            )
        return self


# --------------------------------------------------------------------
# channels.yaml
# --------------------------------------------------------------------


class DiscordChannelConfig(_StrictModel):
    token: str
    allowed_user_ids: list[str] = Field(min_length=1)
    enabled: bool = True


class ChannelsConfig(_StrictModel):
    discord: DiscordChannelConfig | None = None


# --------------------------------------------------------------------
# mcp-servers.yaml
# --------------------------------------------------------------------


class MCPServerConfig(_StrictModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    enabled: bool = True
    # Operator assertion that every tool on this server only observes state.
    # Fills in a missing readOnlyHint annotation at tool discovery so pure-data
    # servers (weather, market quotes) stay callable on scheduled/event turns,
    # which deny anything not strictly read-only. It never downgrades a hint
    # the server itself provides.
    read_only: bool = False

    # stdio
    command: list[str] | None = None
    env: dict[str, str] | None = None

    # http / sse
    url: str | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _transport_fields_required(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires `command`")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"{self.transport} transport requires `url`")
        return self


class MCPServersConfig(_StrictModel):
    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _only_stdio_in_yaml(self) -> "MCPServersConfig":
        for s in self.servers:
            if s.transport != "stdio":
                raise ValueError(
                    f"mcp-servers.yaml only supports stdio servers (server {s.name!r} "
                    f"uses {s.transport!r}); add http/sse servers via the dashboard"
                )
        return self
