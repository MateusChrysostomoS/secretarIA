"""Google Calendar OAuth helpers (platform-level hub onboarding).

A manual authorization-code flow (no extra dependency): build the consent URL,
then exchange the code for tokens server-to-server with httpx. Only the
refresh_token is kept by the caller; the access_token is discarded.

`access_type=offline` + `prompt=consent` are mandatory to reliably receive a
refresh_token. The scope matches Fase A: calendar.events only.
"""

from urllib.parse import urlencode

import httpx

from secretaria.config import get_settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def build_authorization_url(state: str) -> str:
    """Build the Google consent URL the doctor is redirected to."""
    settings = get_settings()
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",  # required so Google returns a refresh_token
        "prompt": "consent",  # force consent so the refresh_token is always sent
        "include_granted_scopes": "false",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code_for_refresh_token(code: str) -> str | None:
    """Exchange an authorization `code` for tokens; return the refresh_token.

    Returns None if Google did not include a refresh_token (e.g. the user had
    already consented and `prompt=consent` was somehow not honoured).
    """
    settings = get_settings()
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_ENDPOINT, data=payload)
        resp.raise_for_status()
        return resp.json().get("refresh_token")
