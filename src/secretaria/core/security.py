"""Security helpers - Meta webhook HMAC-SHA256 signature validation.

Meta signs every webhook POST with the `X-Hub-Signature-256` header in the
format `sha256=<hex digest>`. The digest is an HMAC of the RAW request body
keyed with the App Secret. We MUST verify it against the raw bytes, before
any JSON parsing, otherwise the comparison will not match.
"""

import hashlib
import hmac

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

SIGNATURE_PREFIX = "sha256="


def verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """Return True if `signature_header` is a valid HMAC of `raw_body`.

    Args:
        raw_body: the exact bytes of the request body (NOT re-serialized JSON).
        signature_header: value of the `X-Hub-Signature-256` header.
        app_secret: the Meta App Secret.

    The comparison uses `hmac.compare_digest` for constant-time safety.
    """
    if not signature_header:
        logger.warning("webhook_signature_missing")
        return False

    if not signature_header.startswith(SIGNATURE_PREFIX):
        logger.warning("webhook_signature_malformed")
        return False

    if not app_secret:
        # Misconfiguration - refuse rather than accept everything.
        logger.error("webhook_signature_no_app_secret")
        return False

    received_digest = signature_header[len(SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    valid = hmac.compare_digest(received_digest, expected_digest)
    if not valid:
        logger.warning("webhook_signature_invalid")
    return valid
