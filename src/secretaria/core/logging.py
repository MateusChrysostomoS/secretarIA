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

# LGPD defence in depth (PROMPT_FIX_21). Personal data and conversation content
# must be stripped AT THE CALL SITE — a phone number or a message body should
# never be handed to the logger in the first place. This set is the backstop
# for the one that slips through (or gets reintroduced later).
#
# Matched EXACTLY, never as a substring, so the identifiers we depend on
# operationally survive: `phone_number_id` is Meta's opaque WABA id (not a
# phone), and `wa_id_suffix` / `to_suffix` / `wa_id_sha256` are the sanctioned
# reduced forms. Add the raw name here and log the reduced form instead.
_PII_KEYS = frozenset(
    {
        # Who — phone numbers and identities.
        "wa_id",
        "waid",
        "wa_ids",
        "patient_wa_id",
        "from",
        "from_",
        "to",
        "recipient",
        "phone",
        "phone_number",
        "display_phone_number",
        "msisdn",
        "contact",
        "contacts",
        "full_name",
        "patient_name",
        # What — conversation/clinical content and raw provider payloads.
        "body",
        "text",
        "message",
        "content",
        "inbound_body",
        "reply",
        "rejected_body",
        "prompt",
        "transcript",
        "response",
        "response_text",
        "payload",
    }
)


def wa_suffix(value: str | None, size: int = 4) -> str | None:
    """Last `size` DIGITS of a phone/wa_id — the only form allowed in a log.

    Returns None for an empty value, so a caller never has to guard. Digits
    only, so formatting ("+55 11 ...") cannot smuggle extra characters in.
    """
    if not value:
        return None
    digits = "".join(filter(str.isdigit, value))
    return digits[-size:] or None


def redact_secrets(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Blank secret-bearing keys AND personal-data/content keys before rendering.

    Two independent rules:
      * secrets — key ends in `_encrypted` or contains a `_SECRET_HINTS` hint
        (SUBSTRING match: `waba_token_encrypted`, `authorization`, ...);
      * personal data / content — key is EXACTLY one of `_PII_KEYS`.
    """
    for key in list(event_dict):
        low = key.lower()
        if low == "event":
            continue  # the event NAME is never a secret; never blank it
        if (
            low.endswith("_encrypted")
            or any(hint in low for hint in _SECRET_HINTS)
            or low in _PII_KEYS
        ):
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
