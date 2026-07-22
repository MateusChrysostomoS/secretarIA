"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic import AliasChoices, Field
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

    # --- Precheck hand-off (mesh hub-and-spoke, OUTBOUND to brain-api) ---
    # DIRECTION CHANGE: secretaria used to be designed to call precheck
    # directly (PRECHECK_BASE_URL/PRECHECK_API_KEY, now removed — see
    # services/precheck.py's module docstring). The mesh's trust shape is
    # hub-and-spoke: secretaria now asks brain-api (the hub) to have precheck
    # pre-seed the session server-side (POST /internal/precheck-handoff, via
    # BRAIN_API_BASE_URL/INTERNAL_API_KEY above); secretaria never talks to
    # precheck directly. What remains local is purely cosmetic: the shared
    # PreCheck WhatsApp number we build a wa.me deep link to, and the prefill
    # text for that link. Empty PRECHECK_WHATSAPP_NUMBER means the hand-off
    # feature reports itself unavailable (see services/precheck.py).
    PRECHECK_WHATSAPP_NUMBER: str = ""
    PRECHECK_HANDOFF_PREFILL: str = "Olá! Quero fazer minha pré-consulta."

    # --- Internal API (service-to-service, INBOUND) ---
    # Shared secret that callers in the Brain Co mesh (today: brain-api fetching a
    # tenant's appointments/patients for the doctor portal) must present on the
    # X-Internal-Api-Key header to reach /internal/*. MUST equal brain-api's own
    # INTERNAL_API_KEY byte-for-byte. Empty fails CLOSED: /internal/* then rejects
    # every caller (api/internal.py: require_internal_api_key). NEVER logged.
    # Distinct from ADMIN_TOKEN below (a different mechanism / surface).
    # This SAME value is also SENT outbound to brain-api when verifying hub
    # subscription tokens (core/subscription.py) — one shared secret, both
    # directions.
    INTERNAL_API_KEY: str = ""
    # Rotation window: during a key rotation, the OLD value goes here and the NEW
    # value goes into INTERNAL_API_KEY above. require_internal_api_key accepts
    # either for INBOUND verification; we only ever SEND INTERNAL_API_KEY
    # outbound. Leave empty outside of a rotation.
    INTERNAL_API_KEY_PREVIOUS: str = ""

    # --- brain-api (service-to-service, OUTBOUND) ---
    # secretaria -> brain-api, used to verify a doctor-hub subscription token
    # (core/subscription.py). Internal DNS/URL of brain-api, e.g.
    # http://brain-api:8000. Empty fails CLOSED: the hub then rejects every
    # caller (there is nothing to verify against).
    BRAIN_API_BASE_URL: str = ""
    # Timeout for the brain-api verification call. Any timeout/network error
    # fails CLOSED (treated as "not active").
    BRAIN_API_TIMEOUT_SECONDS: float = 5.0

    # --- Subscription verification cache ---
    # In-process cache of POSITIVE (active) verification results, keyed by a
    # hash of the token. Bounds how often we hit brain-api and sets the upper
    # bound on how quickly a revoked/canceled subscription actually locks the
    # hub out (worst case: this many seconds after brain-api starts saying
    # active=false). <= 0 disables caching (every request re-verifies).
    SUBSCRIPTION_CACHE_TTL_SECONDS: int = 60

    # --- Entitlement cache (services/entitlements_client.py) ---
    # TTL of the FRESH per-tenant entitlement cache (`ent:{tenant_id}`), read on
    # every inbound message before the bot replies (workers/tasks.py). This is
    # the revocation-latency ceiling: how long a tenant can keep chatting with
    # the bot after brain-api starts reporting it as disentitled. <= 0 disables
    # fresh caching (every message re-fetches from brain-api). A separate STALE
    # copy is always kept for 24h and used only when a fresh fetch fails, so a
    # brief brain-api outage degrades gracefully instead of locking every
    # tenant out at once.
    ENTITLEMENT_CACHE_TTL_SECONDS: int = 300

    # --- Handover (bot <-> human secretary) ---
    HANDOVER_TIMEOUT_MINUTES: int = 30

    # --- Context-aware opening greeting (workers/tasks.py + services/patient_context.py) ---
    # Hours ahead of now within which an upcoming appointment counts as "soon"
    # (opening state HAS_UPCOMING_SOON vs HAS_UPCOMING).
    UPCOMING_SOON_HOURS: int = 48
    # Lookback hours behind now within which a non-cancelled past appointment
    # counts as "just had a consult". Past rows older than this are IGNORED by
    # the opening router: there is no auto-transition to attended/no_show (only
    # the doctor sets status via the hub PATCH), so weeks-old rows stuck in
    # SCHEDULED must never influence the greeting.
    POST_CONSULT_WINDOW_HOURS: int = 48

    # --- Onboarding / multi-professional (cross-service contract v1) ---
    # workers/tasks.py:_resolve_tenant's MVP single-tenant scaffold (fall back
    # to META_PHONE_NUMBER_ID / auto-create a Tenant from env when no row
    # matches the inbound phone_number_id). Off by default: production/
    # multi-tenant deployments must never silently adopt or fabricate a
    # tenant for an unrecognized WhatsApp number - an unknown phone_number_id
    # is simply dropped. Only the exact phone_number_id match path (the real
    # routing logic) is unaffected by this flag.
    ALLOW_WEBHOOK_AUTOPROVISION: bool = False
    # Threshold used by services/tenant_config.professional_completeness to
    # decide the partial-activation rule: tenants with `total_active` active
    # professionals at or below this value must have EVERY one of them fully
    # configured (hours + services + calendar) before config_complete is
    # True; above it, one fully-configured professional is enough (a large
    # roster shouldn't block go-live on its slowest-to-configure member).
    PARTIAL_ACTIVATION_THRESHOLD: int = 10

    # --- Onboarding nudge crons (workers/onboarding_cron.py — contract v1 §11/§12) ---
    # Local wall-clock window ("HH:MM-HH:MM") during which retry nudges, the
    # D+60 closing email, and config reminders may actually be SENT. The D+30
    # manual-review flag is NOT gated by this window (it sends no message, see
    # workers/onboarding_cron.py). A tenant due outside this window simply
    # waits for the next hourly tick that lands inside it.
    NUDGE_SEND_WINDOW: str = "09:00-20:00"
    # Fixed day-offsets (from onboarding_anchor_at) for the first retry
    # nudges, e.g. [3, 7, 14, 21, 30]. Overriding via env expects a JSON array
    # (pydantic-settings decodes list-typed settings as JSON), e.g.
    # RETRY_CADENCE_DAYS=[3,7,14,21,30].
    RETRY_CADENCE_DAYS: list[int] = [3, 7, 14, 21, 30]
    # Recurring step (days) once RETRY_CADENCE_DAYS is exhausted (e.g. D+30 ->
    # D+44 -> D+58 for the defaults below).
    RETRY_BIWEEKLY_DAYS: int = 14
    # Total days after onboarding_anchor_at the auto-retry population is
    # nudged. Beyond this, next_retry_time() returns None (no more nudges) and
    # the D+60 closing email fires once.
    RETRY_WINDOW_TOTAL_DAYS: int = 60
    # Fixed day-offsets (from config_reminder_anchor_at) for the first config
    # reminders, e.g. [1, 3, 7]. Same JSON-array override format as above.
    CONFIG_REMINDER_CADENCE_DAYS: list[int] = [1, 3, 7]
    # Recurring step (days) once CONFIG_REMINDER_CADENCE_DAYS is exhausted.
    # Uncapped — config reminders keep recurring until config_status becomes
    # 'completa' (unlike the retry cadence, there is no total-days cutoff).
    CONFIG_REMINDER_WEEKLY_DAYS: int = 7

    # --- Inbound rate limiting (per WhatsApp id, Redis-backed) ---
    # Caps how many inbound messages one wa_id may send in a sliding window
    # before the bot goes silent for them, protecting against floods that would
    # otherwise drive unbounded LLM spend. Silence is intentional (no reply).
    RATE_LIMIT_MAX_MESSAGES: int = 10
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_SILENCE_SECONDS: int = 300

    # --- Admin endpoint ---
    # Shared secret required on the X-Admin-Token header for POST /admin/reset.
    # Leave empty to keep the endpoint disabled (returns 503).
    ADMIN_TOKEN: str = ""
    # Rotation window for ADMIN_TOKEN: during a rotation, the OLD value goes
    # here and the NEW value goes into ADMIN_TOKEN above. require_admin accepts
    # either. Leave empty outside of a rotation.
    ADMIN_TOKEN_PREVIOUS: str = ""

    # --- OpenAI (conversational AI) ---
    OPENAI_API_KEY: str = ""
    # OPENAI_MODEL is accepted as a legacy env alias (AliasChoices tries the
    # new name first) so existing deployments that only set OPENAI_MODEL
    # don't silently change model on upgrade.
    OPENAI_SECRETARIA_MODEL: str = Field(
        default="gpt-5-mini",
        validation_alias=AliasChoices("OPENAI_SECRETARIA_MODEL", "OPENAI_MODEL"),
    )
    # Hard cap per LLM call. Includes reasoning tokens on o-series / gpt-5
    # models — set too low and the budget is consumed by reasoning, returning
    # content="" (the model "ran out" before producing visible text). 2500 is
    # a safe floor for gpt-5-mini conversational turns with tool calls.
    OPENAI_MAX_TOKENS: int = 2500

    # --- Audio transcription (transcription-core) ---
    # STT model, NEVER the chat model: the transcription endpoint rejects chat
    # models, and TranscriptionConfig fails fast on one.
    OPENAI_TRANSCRIPT_MODEL: str = "gpt-4o-mini-transcribe"
    GROQ_API_KEY: str = ""  # optional fallback STT provider
    AUDIO_TRANSCRIPTION_PRIMARY: str = "openai"  # "openai" | "groq"
    AUDIO_DOMAIN_PROMPT: str = (
        "Mensagem de voz em português do Brasil enviada à secretária de uma "
        "clínica: agendar, remarcar ou cancelar consulta, horários, convênio, "
        "exames, receita."
    )

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
    # .env.example / docs/CHECKPOINT_onboarding_multiprofessional.md. Needed before a Calendar connects.
    ENCRYPTION_KEY: str = ""

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

    # --- SMTP (operational alerts to tenants) ---
    # When SMTP_HOST is empty, email alerts are silently skipped (fail-open).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "SecretarIA"
    SMTP_USE_TLS: bool = True
    # How long to silence repeated calendar-unavailable alerts per tenant (seconds).
    CALENDAR_ALERT_SILENCE_SECONDS: int = 14400  # 4 hours

    # --- Transactional email (onboarding lifecycle - contract v1 §4/§10/§12) ---
    # A SEPARATE kill switch from the SMTP_* alert settings above (calendar-
    # unavailable / human-backup emails, which are always-on whenever
    # SMTP_HOST is set): the ten onboarding templates in services/email.py
    # (professional invites, retry/config-reminder nudges, connection
    # success, closing email) are off by default even when SMTP_HOST is
    # already configured for the legacy alerts, so enabling this feature is
    # a deliberate opt-in. Reuses SMTP_HOST/SMTP_PORT/SMTP_USERNAME/
    # SMTP_PASSWORD above (same mail server) but has its own From identity.
    EMAIL_ENABLED: bool = False
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "SecretarIA"

    # --- Reminders plugin (bronze_1 tier / reactivation_pack addon) ---
    # Name of the pre-approved Meta utility (HSM) template used for reminders
    # sent OUTSIDE the 24h customer-service window. Must already be approved
    # on the tenant's WABA. "appointment_reminder" is a sandbox/dev-friendly
    # default name only — it is NOT auto-approved; production tenants need
    # their own reviewed-and-approved template (name it honestly per
    # environment, do not assume the sandbox name carries over to prod).
    REMINDER_TEMPLATE_NAME: str = "appointment_reminder"
    # Lookback (minutes) added behind each lead-time window when the reminder
    # cron scans for due appointments, so a missed/delayed tick still catches
    # appointments that drifted past the exact lead time. Must be >= the cron
    # period (5 minutes) or a tick could skip appointments entirely.
    REMINDER_SCAN_WINDOW_MINUTES: int = 15

    # --- Human backup 24/7 plugin ---
    # Sent once, verbatim, when an inbound message arrives outside the
    # tenant's configured business hours and the human_backup_24_7 addon is
    # entitled (see plugins/human_backup.py).
    HUMAN_BACKUP_ACK_TEXT: str = (
        "Recebi sua mensagem! Nossa equipe humana vai te responder em instantes."
    )

    # --- Asaas / Pix deposit ---
    ASAAS_BASE_URL: str = "https://api.asaas.com/v3"
    ASAAS_TIMEOUT_SECONDS: float = 10.0
    # Meta utility (HSM) template name for a deposit reminder with 3
    # quick-reply buttons (wave S3 - not sent yet by this wave). The template
    # itself needs external Meta approval before it can actually be used;
    # this setting only names it ahead of time.
    REMINDER_DEPOSIT_TEMPLATE_NAME: str = "appointment_reminder_deposit"

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
