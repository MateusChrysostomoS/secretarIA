"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings. Values come from the environment or `.env`.

    Real environment variables always take precedence over the `.env` file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"

    # --- Infrastructure ---
    DATABASE_URL: str = "postgresql+asyncpg://secretaria:secretaria@localhost:5432/secretaria"
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Meta WhatsApp Cloud API ---
    # App Secret - used to validate the X-Hub-Signature-256 HMAC header.
    META_APP_SECRET: str = ""
    # Verify token - our own chosen string for the webhook GET handshake.
    META_VERIFY_TOKEN: str = ""
    # Permanent System User access token (MVP: single tenant).
    META_ACCESS_TOKEN: str = ""
    # phone_number_id (NOT the phone number itself).
    META_PHONE_NUMBER_ID: str = ""
    META_GRAPH_API_VERSION: str = "v21.0"

    # --- Precheck service (service-to-service) ---
    PRECHECK_BASE_URL: str = "http://localhost:8001"
    PRECHECK_API_KEY: str = ""

    # --- Handover (bot <-> human secretary) ---
    HANDOVER_TIMEOUT_MINUTES: int = 30

    # --- Admin endpoint ---
    # Shared secret required on the X-Admin-Token header for POST /admin/reset.
    # Leave empty to keep the endpoint disabled (returns 503).
    ADMIN_TOKEN: str = ""

    # --- OpenAI (conversational AI) ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5-mini"
    # Hard cap per LLM call. Includes reasoning tokens on o-series / gpt-5
    # models — set too low and the budget is consumed by reasoning, returning
    # content="" (the model "ran out" before producing visible text). 2500 is
    # a safe floor for gpt-5-mini conversational turns with tool calls.
    OPENAI_MAX_TOKENS: int = 2500

    # --- Google Calendar ---
    # OAuth Web application client (same client_id/secret for every tenant).
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Single-tenant Fase A: refresh token in env. Multi-tenant: encrypted DB col.
    GOOGLE_REFRESH_TOKEN: str = ""
    GOOGLE_CALENDAR_ID: str = "primary"
    # IANA tz used to interpret naive datetimes coming from the LLM.
    CLINIC_TIMEZONE: str = "America/Sao_Paulo"

    # --- Platform encryption (tenant secrets at rest) ---
    # Fernet key (urlsafe base64, 32 bytes). Generate one with the snippet in
    # .env.example / docs/doctor-hub-backend.md. Needed before a Calendar connects.
    ENCRYPTION_KEY: str = ""

    # --- Doctor hub auth (MVP fake-token seam) ---
    # Placeholder "subscription" token. Presenting it as `Authorization: Bearer
    # <token>` authenticates the doctor hub. This is a stand-in for a paid
    # subscription until the Payments/Subscription API exists. The ONE place to
    # swap it for the real check is core/subscription.py:verify_subscription_token.
    MVP_FAKE_TOKEN: str = ""
    # Which tenant the fake token maps to. If empty AND exactly one tenant row
    # exists, that tenant is used. Set explicitly once there is more than one.
    MVP_FAKE_TOKEN_TENANT_ID: str = ""

    # --- Google Calendar OAuth (platform-level hub onboarding) ---
    # Reuses GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET above (an OAuth *Web
    # application* client). The redirect URI must be registered in the Google
    # Cloud Console exactly as set here.
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/oauth/google/callback"
    # Secret used to sign the opaque OAuth `state` (anti-CSRF; binds the tenant).
    OAUTH_STATE_SECRET: str = ""
    # Max age (seconds) of a `state` token between /start and /callback.
    OAUTH_STATE_MAX_AGE: int = 600
    # Where to send the doctor's browser after the OAuth callback completes.
    PORTAL_POST_OAUTH_REDIRECT: str = "http://localhost:3000/calendar/connected"

    # --- CORS (the Next.js doctor portal) ---
    # Comma-separated list of allowed origins for the hub API.
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ALLOW_ORIGINS into a clean list of origins."""
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
