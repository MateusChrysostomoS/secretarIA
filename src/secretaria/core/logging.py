"""Structured logging setup using structlog.

JSON output in non-dev environments, human-readable console output in dev.
Never use `print` in application code - always use a structlog logger.
"""

import logging
import sys

import structlog

from secretaria.config import get_settings

_configured = False

# tenant-secrets-encryption skill: discipline fails, a processor doesn't. Any value
# whose key ends in `_encrypted` or looks secret-bearing is blanked before ANY
# renderer sees it — even an exception field that happens to carry a token.
_SECRET_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "refresh_token",
    "access_token",
    "encryption_key",
)
_REDACTED = "***REDACTED***"


def redact_secrets(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Blank any structured value whose key ends in `_encrypted` or looks secret-bearing."""
    for key in list(event_dict):
        low = key.lower()
        if low == "event":
            continue  # the event NAME is never a secret; never blank it
        if low.endswith("_encrypted") or any(hint in low for hint in _SECRET_HINTS):
            event_dict[key] = _REDACTED
    return event_dict


def setup_logging() -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,  # before any renderer (tenant-secrets-encryption)
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if settings.is_production or settings.APP_ENV.lower() == "staging":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
