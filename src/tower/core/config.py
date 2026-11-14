"""Configuration via environment variables.

All variables are prefixed with ``TOWER_``, except those Railway injects itself
(``DATABASE_URL``, ``PORT``, ``RAILWAY_*``), which are read as-is.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "test", "prod"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOWER_", env_file=".env", extra="ignore", frozen=True
    )

    env: Env = "dev"
    process: str = "tower"
    """Name of the current process (gate, hangar…), set by the CLI and added to every log."""

    # --- Database-------------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres@localhost:5432/tower",
        validation_alias=AliasChoices("TOWER_DATABASE_URL", "DATABASE_URL"),
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=0, ge=0)
    db_pool_timeout: float = Field(default=10.0, gt=0)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)

    # --- Logs ------------------------------------------------------------------
    log_level: str = "info"
    log_format: LogFormat | None = None
    """``json`` (Railway) or ``console`` (human-readable, local). Default: json outside dev."""

    # --- gate (API) ------------------------------------------------------------
    host: str = "::"
    """``::`` listens on IPv4 and IPv6, required by Railway's private network."""
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "TOWER_PORT"))
    auth_cache_ttl: float = Field(default=15.0, ge=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_batch_size: int = Field(default=200, ge=1, le=1000)
    cors_origins: str = ""
    """Allowed origins, comma-separated (public site, extension)."""

    # --- hangar (worker) -------------------------------------------------------
    services: str = "ping"
    """Services enabled in this process, comma-separated."""
    queues: str = ""
    """Queues consumed, comma-separated. Empty: all queues of the enabled services."""
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_seconds: int = Field(default=300, ge=5)
    job_poll_interval: float = Field(default=5.0, gt=0)
    job_retention_days: int = Field(default=7, ge=1)
    event_retention_days: int = Field(default=30, ge=1)
    error_retention_days: int = Field(default=90, ge=1)
    """Retention period for *resolved* errors (open ones are kept)."""
    error_flush_interval: float = Field(default=5.0, gt=0)
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    # --- LLM providers and router ---------------------------------------------
    openai_api_key: SecretStr | None = None
    """OpenAI organisation WITHOUT data sharing (routes with ``data_sharing_ok = false``)."""
    openai_api_key_shared: SecretStr | None = None
    """OpenAI organisation WITH data sharing (free daily allocation; public data only)."""
    anthropic_api_key: SecretStr | None = None
    openai_usage_tier: int = Field(default=1, ge=1, le=5)
    llm_free_margin: float = Field(default=0.10, ge=0, le=1)
    """Safety margin applied to token estimates before using the free allocation."""

    # --- Outbound HTTP ----------------------------------------------------------
    http_user_agent: str = "Seat12A-Tower/0.1"
    """User-Agent of outbound fetches; add a generic contact URL, never a personal address."""

    # --- Fact-check and ingestion ----------------------------------------------
    fc_languages: str = "fr,en"
    """Languages processed by the fact-check pipeline (BCP 47 primary subtags)."""
    ingest_max_items: int = Field(default=50, ge=1, le=500)
    ingest_daily_quota: int = Field(default=500, ge=1)
    """Items per installation and per UTC day."""

    # --- Verification of extension contents (X oEmbed, service ingest) ----------
    oembed_enabled: bool = True
    """Check what the extension declares against the public embed of the post."""
    oembed_url: str = "https://publish.x.com/oembed"
    oembed_per_minute: int = Field(default=30, ge=1, le=120)
    """Embed requests per minute, all replicas together (one batch job per minute)."""
    oembed_daily_max: int = Field(default=20_000, ge=0)
    """Embed requests per UTC day; beyond, contents stay unverified."""
    oembed_concurrency: int = Field(default=2, ge=1, le=8)
    """Simultaneous embed requests within a batch."""
    oembed_max_age_h: int = Field(default=24, ge=1, le=168)
    """Only contents first seen within this window are checked."""
    oembed_timeout_s: float = Field(default=10.0, gt=0, le=60)

    # --- Fact-check pipeline (lot 3, services factcheck and investigate) ----------
    llm_fake: bool = False
    """Fake model provider producing schema-valid outputs (dev, smoke tests, eval demo).
    Refused when ``TOWER_ENV=prod``."""
    fc_draft_daily_quota: int = Field(default=50, ge=0)
    """Drafts sent to review per UTC day; the surplus waits for a free slot."""
    fc_cap_standard_usd: float = Field(default=0.10, ge=0)
    """Per-investigation cost cap at the standard depth: no escalation beyond it."""
    fc_cap_reinforced_usd: float = Field(default=0.40, ge=0)
    fc_cap_n4_usd: float = Field(default=0.60, ge=0)
    fc_n4_revalidate_hours: int = Field(default=24, ge=1)
    """N4 continuous revalidation: an N4 verdict expires (and is re-investigated) at most
    this many hours after its validation."""
    fc_searches_standard: int = Field(default=4, ge=0, le=20)
    """Web searches of a standard investigation (after the free sources)."""
    fc_searches_reinforced_extra: int = Field(default=4, ge=0, le=20)
    fc_passages_standard: int = Field(default=30, ge=4, le=200)
    """Passages given to the judge (~160 tokens each)."""
    fc_passages_reinforced: int = Field(default=60, ge=4, le=200)
    fc_passage_words: int = Field(default=120, ge=40, le=400)
    fc_confidence_coefficients: str = ""
    """JSON object overriding coefficients of the confidence regression (pipeline.md E10)."""
    fc_confidence_cap: float = Field(default=0.90, gt=0, le=1)
    fc_confidence_high: float = Field(default=0.80, gt=0, le=1)
    fc_confidence_medium: float = Field(default=0.55, gt=0, le=1)
    fc_escalation_threshold: float = Field(default=0.60, ge=0, le=1)
    fc_loaded_terms_file: str = ""
    """JSON lexicon of loaded terms by language; empty = the packaged default."""
    fc_reference_langs: str = "fr"
    """Reference languages always written besides the post language (``en`` is added for
    English posts)."""
    fc_review_locale_fallback: str = "fr"

    # --- Fact-check V1 (lot 4: embeddings, groups, reuse, profiles) ---------------
    fc_embed_batch_size: int = Field(default=2000, ge=100, le=2000)
    """Contents embedded per ``factcheck.embed_batch`` job (one API request per 256)."""
    fc_embed_debounce_seconds: int = Field(default=30, ge=0, le=600)
    """Delay before a batch runs, so the events of a minute share one batch."""
    fc_embedding_retention_days: int = Field(default=14, ge=7, le=60)
    fc_e1_sample_rate: float = Field(default=0.02, ge=0, le=0.2)
    """Share of contents discarded by E1 kept as a random sample (false negatives)."""
    fc_e1_thresholds: str = '{"N1": 0.15, "N2": 0.15, "N3": 0.05, "N4": 0.0}'
    """Default E1 thresholds by level (JSON), used by the default processing profile."""
    fc_match_candidates: int = Field(default=5, ge=1, le=20)
    """Nearest groups compared by E3."""
    fc_match_same_threshold: float = Field(default=0.92, ge=0.85, le=0.999)
    """Cosine similarity above which an identical-signature pair of the same language is
    the same claim without the arbiter."""
    fc_match_grey_threshold: float = Field(default=0.80, ge=0.5, le=0.99)
    """Lower bound of the grey zone (arbiter); below, the pair is different."""
    fc_current_period_days: int = Field(default=31, ge=1, le=366)
    """Without a reference fact, two "currently" claims share a period when their posts
    are at most this many days apart."""
    fc_band_tau_ratio: float = Field(default=0.05, gt=0, le=0.5)
    """Value band tolerance: tau = max(floor, ratio x |reference|)."""
    fc_band_floor_rate_points: float = Field(default=0.2, ge=0)
    """Tolerance floor for a rate (unit ``%``), in points."""
    fc_band_floor_default: float = Field(default=0.0, ge=0)
    """Tolerance floor for other units (in the unit of the fact)."""
    fc_verdict_ttl_days: str = ""
    """JSON object overriding the validity of a verdict by claim type, in days."""
    fc_level_budgets_usd: str = '{"N0": 1, "N1": 5, "N2": 10, "N3": 20, "N4": 15}'
    """Monthly caps of the budget scopes ``level:N0..N4`` created by ``tower llm seed``."""
    fc_insee_indicators: str = ""
    """JSON list of tracked INSEE indicators fed automatically into reference facts."""
    fc_external_factcheck_lookup: bool = True
    """E3 looks up existing fact-checks (Google Fact Check Tools) for new groups."""

    # --- Fact-check V2 (lot 5: mass flow, scheduler, light check, narratives) ----
    fc_mass_flow: bool = True
    """Extract N1 contents in micro-batches (lot 5). False: E1 classifies only (lot 4)."""
    fc_batch_size: int = Field(default=10, ge=1, le=12)
    fc_batch_max_tokens: int = Field(default=2500, ge=200, le=8000)
    fc_batch_max_wait_s: int = Field(default=21600, ge=60)
    """A batch leaves after this delay even if it is not full."""
    fc_batch_dissimilarity: float = Field(default=0.6, gt=0, le=1)
    """Two posts of a batch never have a cosine above this value."""
    fc_canary_rate: float = Field(default=0.01, ge=0, le=0.5)
    fc_solo_audit_rate: float = Field(default=0.02, ge=0, le=0.5)
    fc_scheduler_target_per_pass: float = Field(default=5.0, gt=0)
    fc_scheduler_min_interval_s: int = Field(default=60, ge=10)
    fc_scheduler_max_interval_s: int = Field(default=1800, ge=60)
    fc_promotion_observers: int = Field(default=5, ge=1)
    """Independent observers (installations) in 6 h promoting a group to N2."""
    fc_promotion_views: int = Field(default=50000, ge=1)
    fc_promotion_velocity_factor: float = Field(default=3.0, gt=1)
    fc_demotion_hours: int = Field(default=24, ge=1)
    fc_light_check_share: float = Field(default=0.2, ge=0, le=1)
    """Share of the new N1 groups of a pass sent to the light check (top priority)."""
    fc_deep_queue_capacity: int = Field(default=50, ge=1)
    """Backpressure: the scheduler enqueues no investigation beyond this fc-deep backlog."""
    fc_narrative_semantic_threshold: float = Field(default=0.80, ge=0.5, le=0.99)
    fc_narrative_medoid_floor: float = Field(default=0.60, ge=0.3, le=0.99)
    fc_narrative_rare_entity_max: int = Field(default=50, ge=1)
    """An entity seen in at most this many claims over 30 days is a specific anchor."""
    fc_narrative_close_days: int = Field(default=7, ge=1)
    fc_narrative_split_groups: int = Field(default=25, ge=2)
    fc_wave_thresholds: str = (
        '{"watch": {"share": 0.01, "accounts": 10}, '
        '"surge": {"share": 0.03, "accounts": 30, "growth": 2.0}, '
        '"alert": {"share": 0.08, "accounts": 80}}'
    )
    fc_watched_themes: str = "health,elections,security,disaster"
    """Themes under watch: a narrative of these themes at the watch tier goes to alert."""
    fc_surge_daily_quota: int = Field(default=3, ge=0)
    """Drafts reserved for wave mode, separate from the daily quota."""
    fc_surge_end_hours: int = Field(default=6, ge=1)
    fc_surge_verdict_ttl_hours: int = Field(default=12, ge=1)
    fc_sampling_per_minute: int = Field(default=200, ge=1)
    """Beyond this rate per surging narrative, only a stratified sample is extracted."""
    fc_novelty_posts: int = Field(default=3, ge=1)
    fc_novelty_accounts: int = Field(default=3, ge=1)
    fc_novelty_budget_share: float = Field(default=0.2, ge=0, le=1)

    # --- Knowledge sources (Atlas) -------------------------------------------------
    google_api_key: SecretStr | None = None
    """Google Fact Check Tools API key (``claims:search``)."""
    brave_api_key: SecretStr | None = None
    """Brave Search API subscription token."""
    piste_client_id: SecretStr | None = None
    piste_client_secret: SecretStr | None = None
    """Légifrance through PISTE (OAuth2 client credentials)."""
    piste_sandbox: bool = False
    """Use the PISTE sandbox endpoints instead of production."""
    insee_api_key: SecretStr | None = None
    """INSEE API portal key (BDM), when the portal requires one."""
    insee_series: str = ""
    """JSON object ``{"indicator": "idbank"}`` of the INSEE BDM series used by the selector."""
    atlas_wikipedia_concurrency: int = Field(default=3, ge=1, le=3)
    """Concurrent Wikimedia requests per process (Wikimedia limit: 3)."""
    atlas_cache_ttl_s: int = Field(default=7 * 86400, ge=60)
    """Default freshness of ``source_cache`` entries (search results: 1 day)."""
    atlas_http_timeout_s: float = Field(default=10.0, gt=0)
    # Lot 6: complete Atlas (service ``atlas`` for the synchronizations)
    atlas_legi_base_url: str = "https://echanges.dila.gouv.fr/OPENDATA/LEGI/"
    """DILA open-data directory of the LEGI archives (global dump + daily diffs)."""
    atlas_legi_texts: str = ""
    """Comma-separated LEGI text ids mirrored; empty = the priority codes
    (``LEGI_PRIORITY_TEXTS``: penal, criminal procedure, civil, labour, CESEDA, electoral,
    public health, internal security, Constitution)."""
    atlas_legi_max_archive_mb: int = Field(default=2000, ge=1)
    """Download refused above this size (the global dump is about 1.3 GB)."""
    atlas_news_poll_s: int = Field(default=900, ge=60)
    atlas_news_retention_days: int = Field(default=90, ge=1)
    atlas_news_max_items: int = Field(default=50, ge=1, le=500)
    """Items read per feed and poll (newest first)."""
    atlas_news_concurrency: int = Field(default=4, ge=1, le=16)
    atlas_stat_indicators: str = ""
    """JSON list of the international indicators (Eurostat, World Bank, OECD, IMF) used by
    the selector and fed daily as pending reference facts (docs/configuration.md)."""
    atlas_on_demand_legal: str = "eurlex,fedlex,canada_laws"
    """Free on-demand legal adapters enabled (cached in ``source_cache``)."""
    atlas_gdelt_enabled: bool = True
    """GDELT DOC 2.0 (free, no key; one request per 5 s at most)."""

    # --- Budgets ---------------------------------------------------------------
    budget_reservation_ttl_s: int = Field(default=1800, ge=60)
    """A budget/free-allocation reservation not settled after this delay is released."""

    # --- Radar (X official API, hangar service ``radar``) -----------------------
    x_bearer_token: SecretStr | None = None
    """App-only bearer token of the X API (secret, never logged)."""
    x_api_base: str = "https://api.x.com"
    """Base URL of the X API (a local fake server in smoke tests)."""
    x_price_post_read_usd: float = Field(default=0.005, ge=0)
    x_price_user_read_usd: float = Field(default=0.010, ge=0)
    x_monthly_cap_usd: float = Field(default=20.0, ge=0)
    """Initial ``provider:x`` cap created by ``tower llm seed`` (then edited in base)."""
    radar_max_results: int = Field(default=20, ge=5, le=100)
    """Posts requested per timeline page (X bounds: 5..100)."""
    radar_max_pages: int = Field(default=3, ge=1, le=10)
    """Timeline pages read per poll job at most."""
    radar_exclude: str = "retweets"
    """``exclude`` of the timeline call: ``retweets``, ``replies``, both or empty."""
    radar_interval_high_s: int = Field(default=120, ge=30)
    """Base interval for priority >= 50."""
    radar_interval_normal_s: int = Field(default=600, ge=30)
    """Base interval for priority 0..49."""
    radar_interval_low_s: int = Field(default=1800, ge=30)
    """Base interval for priority < 0."""
    radar_interval_min_s: int = Field(default=60, ge=30)
    radar_interval_max_s: int = Field(default=21600, ge=60)
    radar_expected_posts_per_poll: float = Field(default=2.0, gt=0)
    """Average posts returned by one poll (an empty read costs nothing on X): sizes the
    daily poll allowance (remaining budget / days left / average cost)."""
    radar_schedule_batch: int = Field(default=100, ge=1, le=1000)
    """Poll jobs enqueued per scheduling round at most."""

    # --- intercom (Discord bot) -------------------------------------------------
    discord_token: SecretStr | None = None
    discord_guild_id: int | None = None
    """Guild where slash commands are synced (instant); global sync when empty."""
    discord_channel_incidents: int | None = None
    discord_channel_budget: int | None = None
    discord_channel_dossiers: int | None = None
    discord_channel_admin: int | None = None
    discord_alert_role_id: int | None = None
    """Role mentioned in wave alerts (``narrative.surge_detected`` at tier ``alert``)."""
    gate_url: str = "http://localhost:8000"
    gate_token: SecretStr | None = None
    """Service token of the bot (scopes: see docs/intercom.md)."""
    intercom_cursor_name: str = "intercom"
    discord_locale: str = "fr"
    """Language of channel notifications (``fr`` or ``en``); replies follow the user."""

    # --- Railway (read-only)----------------------------------------------
    railway_replica_id: str | None = Field(
        default=None, validation_alias=AliasChoices("RAILWAY_REPLICA_ID")
    )
    railway_deployment_id: str | None = Field(
        default=None, validation_alias=AliasChoices("RAILWAY_DEPLOYMENT_ID")
    )

    @model_validator(mode="after")
    def _no_fake_in_prod(self) -> Settings:
        if self.llm_fake and self.env == "prod":
            raise ValueError("TWR-FCK-016: TOWER_LLM_FAKE is refused when TOWER_ENV=prod")
        return self

    @field_validator("log_level")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    # --- Derived values------------------------------------------------------
    @property
    def effective_log_format(self) -> LogFormat:
        if self.log_format is not None:
            return self.log_format
        return "console" if self.env == "dev" else "json"

    @property
    def service_names(self) -> list[str]:
        return _split(self.services)

    @property
    def queue_names(self) -> list[str]:
        return _split(self.queues)

    @property
    def fc_language_list(self) -> list[str]:
        return [lang.lower() for lang in _split(self.fc_languages)]

    @property
    def fc_reference_lang_list(self) -> list[str]:
        return [lang.lower() for lang in _split(self.fc_reference_langs)]

    @property
    def cors_origin_list(self) -> list[str]:
        return _split(self.cors_origins)

    @property
    def sqlalchemy_url(self) -> str:
        """URL for SQLAlchemy (asyncpg driver), without parameters asyncpg does not know."""
        scheme, rest = _normalized(self.database_url)
        return f"postgresql+asyncpg{rest}" if scheme else rest

    @property
    def asyncpg_dsn(self) -> str:
        """Raw DSN for a direct asyncpg connection (LISTEN)."""
        _, rest = _normalized(self.database_url)
        return f"postgresql{rest}"

    @property
    def db_ssl(self) -> bool | None:
        mode = dict(parse_qsl(urlsplit(self.database_url).query)).get("sslmode")
        if mode is None:
            return None
        return mode not in {"disable", "allow"}

    @property
    def radar_exclude_list(self) -> list[str]:
        return _split(self.radar_exclude)

    @property
    def instance_id(self) -> str:
        """Human-readable instance identifier (worker id, application_name)."""
        replica = (self.railway_replica_id or "local")[:8]
        return f"{self.process}-{replica}"


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalized(url: str) -> tuple[str, str]:
    """Return (original scheme, rest of the URL from ``://``), without ``sslmode``."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError(f"Expected a PostgreSQL URL, got scheme {parts.scheme!r}")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"])
    rebuilt = urlunsplit(("x", parts.netloc, parts.path, query, parts.fragment))
    return parts.scheme, rebuilt[1:]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
