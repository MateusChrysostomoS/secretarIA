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

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
