"""Structured logging setup using structlog.

JSON output in non-dev environments, human-readable console output in dev.
Never use `print` in application code - always use a structlog logger.
"""

import logging
import re
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


# One-time transactional secrets carried as job arguments: the patient-portal
# OTP (`code`) and the single-use password-reset/invite URL (`link`). Neither is
# personal data nor a credential name, so neither list above catches them.
_JOB_SECRET_KEYS = frozenset({"code", "otp", "link"})

# `'key': value` (a repr'd dict) or `key=value` (a repr'd kwarg). The value is a
# quoted string — which may have NO closing quote, because arq truncates the
# argument string at 80 chars (`'code': '91…`) — or a bare token up to the next
# separator. `$` as a terminator is what stops a truncated value from leaking.
# A bare value never starts a container (`{[(`) or a quote: `'variables': {…}`
# matches an EMPTY value, so the scan goes on INTO the dict and finds `'code'`.
_KEYED_VALUE = re.compile(
    r"""(?P<head>(?P<q>['"])(?P<dkey>\w+)(?P=q)\s*:\s*|\b(?P<kkey>\w+)=)"""
    # `[bBrRuU]{0,2}`: a repr'd bytes value is `b'…'` — without the prefix the
    # bare-token branch would eat only the `b` and leave the quoted secret intact.
    r"""(?P<value>[bBrRuU]{0,2}(?:'(?:[^'\\]|\\.)*(?:'|$)|"(?:[^"\\]|\\.)*(?:"|$))"""
    r"""|[^,}\])\s'"{\[(]*)"""
)
# An e-mail address is a POSITIONAL job argument (`send_transactional_email(
# template, to, variables)`), so it has no key to match on — match its shape.
# `[\w.-]*` after the `@` also covers an address cut short by the truncation.
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]*")


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return (
        low.endswith("_encrypted")
        or any(hint in low for hint in _SECRET_HINTS)
        or low in _PII_KEYS
        or low in _JOB_SECRET_KEYS
    )


def redact_free_text(text: str) -> str:
    """Scrub a free-text log message the same way `redact_secrets` scrubs keys.

    For stdlib loggers that bypass structlog and hand us an already-formatted
    string (arq's `→ job(args…)` line). Same vocabulary as `redact_secrets`, plus
    `_JOB_SECRET_KEYS`, applied by REGEX because there is no dict here — and to
    ANY job, not only `send_transactional_email`, so the next job that carries a
    secret is covered without anyone remembering to add it.
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group("dkey") or match.group("kkey")
        if not _is_sensitive_key(key):
            return match.group(0)
        return f"{match.group('head')}'{_REDACTED}'"

    return _EMAIL.sub(_REDACTED, _KEYED_VALUE.sub(_sub, text))


class ArqJobArgsRedactionFilter(logging.Filter):
    """Redact job arguments that arq's own logger prints in clear text.

    `arq.worker.Worker.run_job` logs `'%6.2fs → %s(%s)%s'` with
    `arq.utils.args_to_string(args, kwargs)` — a plain `repr()` of every
    argument of every job, at INFO, through stdlib `logging`, never through
    structlog, so `redact_secrets` never sees it. That line printed the portal
    OTP and the recipient's e-mail for `send_transactional_email` (see
    docs/CHECKPOINT_secretaria_arq_log_secret_leak.md). We redact instead of
    raising the logger to WARNING: the job name/duration line is the worker's
    only operational trace.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken record must not kill logging
            return True
        redacted = redact_free_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# Every arq logger that can format job data: `arq.worker` (job start `→ args`,
# job end `● repr(result)`) and `arq.jobs` (result serialization). The filter
# sits on the LOGGER, not a handler, so it holds whatever handler arq's own
# `dictConfig` installs (`disable_existing_loggers` is False there).
_ARQ_LOGGERS = ("arq.worker", "arq.jobs")


def install_arq_log_redaction() -> None:
    """Attach `ArqJobArgsRedactionFilter` to arq's loggers. Idempotent."""
    for name in _ARQ_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, ArqJobArgsRedactionFilter) for f in target.filters):
            target.addFilter(ArqJobArgsRedactionFilter())


def setup_logging() -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    install_arq_log_redaction()

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
