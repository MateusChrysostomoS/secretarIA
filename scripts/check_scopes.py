"""Diagnose Fase A auth: what scopes does our refresh token actually carry?

A refresh token doesn't reveal its scopes; the access token it produces does.
This script:
  1. Trades the refresh token in .env for a fresh access token.
  2. Asks Google's tokeninfo endpoint what scopes that access token carries.
  3. Probes the real Calendar API (GET /calendars/primary) so any 403 body
     from the Calendar service itself shows up.

Uses `get_settings()` (same loader as services/calendar.py) so the values
inspected here are EXACTLY the ones the app would use.

Run:
    uv run python scripts/check_scopes.py
"""

import httpx

from secretaria.config import get_settings
from secretaria.services.calendar import SCOPES as EXPECTED_SCOPES

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
TOKENINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v1/tokeninfo"
CALENDAR_PROBE = "https://www.googleapis.com/calendar/v3/calendars/{cal}"


def _short(token: str) -> str:
    return f"{token[:6]}...{token[-6:]} (len={len(token)})"


def main() -> None:
    s = get_settings()
    missing_env = [
        name
        for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        if not getattr(s, name)
    ]
    if missing_env:
        raise SystemExit(f"Missing in .env: {', '.join(missing_env)}")

    print("== Inputs (as the app sees them) ==")
    print(f"  client_id:     {_short(s.GOOGLE_CLIENT_ID)}")
    print(f"  refresh_token: {_short(s.GOOGLE_REFRESH_TOKEN)}")
    print(f"  calendar_id:   {s.GOOGLE_CALENDAR_ID}")
    print(f"  expected:      {EXPECTED_SCOPES}")
    print()

    # 1. Refresh -> access token.
    print("== Step 1: refresh -> access token ==")
    resp = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": s.GOOGLE_CLIENT_ID,
            "client_secret": s.GOOGLE_CLIENT_SECRET,
            "refresh_token": s.GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    print(f"  status: {resp.status_code}")
    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        print(f"  FAILED. body: {data}")
        raise SystemExit(1)
    print(f"  access_token: {_short(access_token)}")
    if "scope" in data:
        # Google often echoes the scope on refresh too.
        print(f"  scope (refresh response): {data['scope']}")
    print()

    # 2. tokeninfo -> what scopes are inside it?
    print("== Step 2: tokeninfo ==")
    info = httpx.get(
        TOKENINFO_ENDPOINT,
        params={"access_token": access_token},
        timeout=20.0,
    ).json()
    granted = (info.get("scope") or "").split()
    print(f"  authorized email: {info.get('email')}")
    print(f"  audience (client_id of grant): {info.get('audience')}")
    print(f"  expires_in: {info.get('expires_in')}s")
    print(f"  granted scopes ({len(granted)}):")
    for sc in granted:
        mark = "OK " if sc in EXPECTED_SCOPES else "   "
        print(f"    {mark} {sc}")
    missing = [sc for sc in EXPECTED_SCOPES if sc not in granted]
    print()

    # 3. Calendar API probe.
    print("== Step 3: real Calendar API probe ==")
    probe = httpx.get(
        CALENDAR_PROBE.format(cal=s.GOOGLE_CALENDAR_ID),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20.0,
    )
    print(f"  GET /calendars/{s.GOOGLE_CALENDAR_ID} -> {probe.status_code}")
    if probe.status_code == 200:
        body = probe.json()
        print(f"  summary: {body.get('summary')}  tz: {body.get('timeZone')}")
    else:
        print(f"  body: {probe.text[:600]}")
    print()

    # Verdict.
    print("== Verdict ==")
    if missing:
        print("  CENARIO A: refresh token is missing required scopes.")
        for sc in missing:
            print(f"    - {sc}")
        print("  Fix: re-run scripts/gcal_auth.py, OVERWRITE the line in .env,")
        print("       authorize with the SAME Gmail and accept the calendar scope.")
        return
    if probe.status_code != 200:
        print(f"  CENARIO B: scopes OK, but Calendar API returned {probe.status_code}.")
        print("  Look at the body above. Common causes:")
        print("    - Calendar API not enabled on the GCP project (enable it).")
        print("    - Authorized email above does not own GOOGLE_CALENDAR_ID.")
        print("    - Calendar ID is wrong (try 'primary' to isolate).")
        return
    print("  All green. Auth is fine; if test_agent.py still 403s, the bug is")
    print("  in the request shape, not the credentials.")


if __name__ == "__main__":
    main()
