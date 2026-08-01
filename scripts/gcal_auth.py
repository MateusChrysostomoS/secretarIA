"""Generate a Google Calendar refresh token for Fase A (single tenant).

One-shot CLI: opens your browser, asks you to authorise SecretarIA against
your Google Calendar, prints the refresh_token to paste into .env as
GOOGLE_REFRESH_TOKEN.

Pre-reqs in .env:
  - GOOGLE_CLIENT_ID
  - GOOGLE_CLIENT_SECRET

Pre-reqs in Google Cloud Console (OAuth client of type "Web application"):
  - Authorized redirect URI: http://localhost:8080/
  - Calendar API enabled
  - Consent screen scopes: https://www.googleapis.com/auth/calendar.events
    and https://www.googleapis.com/auth/calendar.app.created (the latter
    covers calendars.insert + CRUD on app-created calendars - shared_account
    mode, see docs/CHECKPOINT_google_calendar_modes.md)
  - Your Gmail added as a Test User while the consent screen is in "Testing"
    mode (refresh tokens issued in Testing mode expire after 7 days; publish
    the app for permanent refresh tokens).

Run from the project root so pydantic-settings finds .env:
    uv run python scripts/gcal_auth.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

from secretaria.config import get_settings

# Mirrors src/secretaria/services/calendar.py::SCOPES.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.app.created",
]
REDIRECT_URI = "http://localhost:8080/"


def main() -> None:
    settings = get_settings()
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise SystemExit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env first."
        )

    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent are required for Google to actually
    # return a refresh_token (otherwise it skips it on a re-authorization).
    creds = flow.run_local_server(
        host="localhost",
        port=8080,
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )

    if not creds.refresh_token:
        raise SystemExit(
            "No refresh_token returned. Check that:\n"
            "  - OAuth client is the Web application type\n"
            "  - http://localhost:8080/ is registered as a redirect URI\n"
            "  - You authorized with a Gmail listed as Test User"
        )

    print("\n--- COPY INTO .env ---")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("----------------------\n")
    print("Done. Paste the line above into .env, then proceed to Fase A.")


if __name__ == "__main__":
    main()
