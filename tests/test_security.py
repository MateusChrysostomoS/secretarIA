"""Tests for the Meta webhook HMAC-SHA256 signature validation."""

import hashlib
import hmac

from secretaria.core.security import verify_meta_signature

APP_SECRET = "unit-test-secret"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    """Build a valid `X-Hub-Signature-256` header value for `body`."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_is_accepted() -> None:
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    assert verify_meta_signature(body, _sign(body), APP_SECRET) is True


def test_invalid_signature_is_rejected() -> None:
    body = b'{"hello":"world"}'
    bad_signature = "sha256=" + "0" * 64
    assert verify_meta_signature(body, bad_signature, APP_SECRET) is False


def test_signature_from_wrong_secret_is_rejected() -> None:
    body = b'{"hello":"world"}'
    signature = _sign(body, secret="the-wrong-secret")
    assert verify_meta_signature(body, signature, APP_SECRET) is False


def test_tampered_body_is_rejected() -> None:
    signature = _sign(b'{"amount":10}')
    assert verify_meta_signature(b'{"amount":9999}', signature, APP_SECRET) is False


def test_missing_header_is_rejected() -> None:
    assert verify_meta_signature(b"{}", None, APP_SECRET) is False


def test_malformed_header_without_prefix_is_rejected() -> None:
    assert verify_meta_signature(b"{}", "deadbeef", APP_SECRET) is False


def test_empty_app_secret_is_rejected() -> None:
    body = b"{}"
    assert verify_meta_signature(body, _sign(body), "") is False
