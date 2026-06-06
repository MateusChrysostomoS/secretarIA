"""Opaque, signed, time-limited tokens (stdlib HMAC-SHA256).

Used for the Google OAuth `state` parameter: we encode the tenant_id, sign it
with OAUTH_STATE_SECRET and check the signature + age in the callback. This
binds the callback to the tenant who started the flow (anti-CSRF) without
trusting a browser session, and needs no extra dependency.

Token format:  <urlsafe_b64(json_payload)>.<urlsafe_b64(hmac_sig)>
"""

import base64
import hashlib
import hmac
import json
import time


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign(payload: dict, secret: str) -> str:
    """Serialise `payload` (+ a timestamp) and append an HMAC signature."""
    stamped = {**payload, "ts": int(time.time())}
    body = _b64encode(json.dumps(stamped, separators=(",", ":")).encode("utf-8"))
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(digest)}"


def verify(token: str, secret: str, max_age: int) -> dict | None:
    """Return the original payload if `token` is authentic and fresh, else None.

    None is returned for any failure (bad shape, bad signature, expired) so the
    caller treats every invalid state uniformly.
    """
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None

    expected = _b64encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    # Constant-time comparison to avoid leaking signature bytes via timing.
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        data = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return None

    ts = data.get("ts")
    if not isinstance(ts, int) or (time.time() - ts) > max_age:
        return None

    data.pop("ts", None)
    return data
