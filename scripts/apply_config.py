"""Apply a JSON config file onto a tenant row (the doctor-hub columns).

This is the script equivalent of `PUT /tenants/me/config`: it writes the
non-sensitive per-tenant configuration (greeting, persona, business hours,
appointment types, deterministic `initial_flows`, language, timezone, ...)
straight onto the `tenants` row, so the agent + flow router "dress" themselves
for this clinic on the next inbound WhatsApp message. No app or token needed.

It connects with whatever `DATABASE_URL` the environment provides, so run it
where that points at the database you mean to change (e.g. inside the Easypanel
app container, where it resolves the internal Postgres host).

Only whitelisted keys are written; unknown keys are reported and ignored. The
update is partial — keys absent from the file are left untouched. Secrets
(access_token, refresh token) are NEVER touched here.

Usage:
    uv run python scripts/apply_config.py scripts/configs/clinica-psi-infantil.json
    uv run python scripts/apply_config.py <config.json> <tenant_id>
"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.models import Tenant
from secretaria.services.flow_router import flows_enabled, menu_buttons
from secretaria.services.tenant_config import can_activate, has_google_refresh_token

logger = get_logger(__name__)

# Columns this script is allowed to set (mirrors api/config.py:_SCALAR_FIELDS,
# plus clinic_name and the activation flag). is_active is handled separately.
_ALLOWED_FIELDS = (
    "clinic_name",
    "contact_email",
    "greeting_message",
    "returning_greeting_message",
    "greeting_buttons",
    "persona_notes",
    "language",
    "timezone",
    "google_calendar_id",
    "appointment_duration_min",
    "business_hours",
    "appointment_types",
    "initial_flows",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Path to the tenant config JSON file.")
    parser.add_argument(
        "tenant_id",
        nargs="?",
        default=None,
        help="Target tenant UUID. Omit to use the single existing tenant.",
    )
    return parser.parse_args()


def _validate(data: dict) -> list[str]:
    """Return a list of human-readable warnings (does not block the write)."""
    warnings: list[str] = []

    def _check_buttons(label: str, buttons) -> None:
        if not isinstance(buttons, list):
            warnings.append(f"{label}: expected a list, got {type(buttons).__name__}.")
            return
        if len(buttons) > 3:
            warnings.append(f"{label}: {len(buttons)} buttons; WhatsApp shows only the first 3.")
        for b in buttons:
            if isinstance(b, str) and len(b) > 20:
                warnings.append(f"{label}: '{b}' is >20 chars; WhatsApp truncates reply-button titles.")

    if "greeting_buttons" in data:
        _check_buttons("greeting_buttons", data["greeting_buttons"])
    if isinstance(data.get("initial_flows"), dict) and "buttons" in data["initial_flows"]:
        _check_buttons("initial_flows.buttons", data["initial_flows"]["buttons"])

    for i, t in enumerate(data.get("appointment_types") or []):
        if not isinstance(t, dict) or not t.get("name"):
            warnings.append(f"appointment_types[{i}]: missing 'name'.")

    flows = data.get("initial_flows")
    if isinstance(flows, dict) and flows.get("enabled") and not (data.get("appointment_types")):
        warnings.append(
            "initial_flows.enabled is true but no appointment_types set — the "
            "service-catalog flow will have nothing to offer."
        )
    return warnings


async def main() -> None:
    setup_logging()
    args = _parse_args()
    get_settings()  # surfaces a clear error early if the env is misconfigured.

    path = Path(args.config)
    if not path.is_file():
        raise SystemExit(f"Config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("Top-level JSON must be an object of config fields.")

    unknown = [k for k in data if k not in _ALLOWED_FIELDS and k != "is_active"]
    if unknown:
        print(f"[skip] ignoring unknown keys: {', '.join(sorted(unknown))}")
    for w in _validate(data):
        print(f"[warn] {w}")

    async with async_session_factory() as session:
        async with session.begin():
            if args.tenant_id:
                try:
                    tenant_uuid = UUID(args.tenant_id)
                except ValueError:
                    raise SystemExit(
                        f"'{args.tenant_id}' is not a valid tenant UUID. Omit the "
                        "argument to target the single existing tenant, or pass a "
                        "real id (SELECT id FROM tenants;). Note: '[tenant_id]' in "
                        "the docs is an optional-arg placeholder, not literal text."
                    ) from None
                tenant = await session.get(Tenant, tenant_uuid)
            else:
                rows = (await session.scalars(select(Tenant).limit(2))).all()
                if len(rows) != 1:
                    raise SystemExit(
                        f"Expected exactly one tenant, found {len(rows)}. "
                        "Pass a tenant_id explicitly."
                    )
                tenant = rows[0]
            if tenant is None:
                raise SystemExit("Tenant not found.")

            applied = []
            for field_name in _ALLOWED_FIELDS:
                if field_name in data:
                    setattr(tenant, field_name, data[field_name])
                    applied.append(field_name)

            # Activation is gated like the API: it needs a connected Calendar,
            # an active appointment type and an availability window. We honour an
            # explicit request but report (never silently flip to a broken live).
            connected = await has_google_refresh_token(session, tenant.id)
            if "is_active" in data:
                want_active = bool(data["is_active"])
                ok, reason = can_activate(tenant, connected)
                if want_active and not ok:
                    print(f"[warn] is_active requested but prerequisites unmet: {reason}")
                    print("       Setting it anyway (owner override); booking steps may degrade.")
                tenant.is_active = want_active
                applied.append("is_active")

            tenant_id = tenant.id
            summary = {
                "clinic_name": tenant.clinic_name,
                "is_active": tenant.is_active,
                "flows_enabled": flows_enabled(tenant),
                "menu_buttons": menu_buttons(tenant) if flows_enabled(tenant) else [],
                "appointment_types": len(tenant.appointment_types or []),
                "business_hours_days": len(tenant.business_hours or {}),
                "calendar_connected": connected,
            }

    logger.info("apply_config_done", tenant_id=str(tenant_id), fields=sorted(applied))
    print("\n" + "=" * 60)
    print(f"Applied {len(applied)} field(s) to tenant {tenant_id}:")
    print("  " + ", ".join(sorted(applied)))
    print("-" * 60)
    for k, v in summary.items():
        print(f"  {k:<20} {v}")
    print("=" * 60)
    if summary["flows_enabled"] and summary["is_active"]:
        print("Workflows ON + bot active — send a message to the WhatsApp number to test.")
    elif not summary["is_active"]:
        print("NOTE: is_active is False — the webhook will send the 'unavailable' fallback.")
    elif not summary["flows_enabled"]:
        print("NOTE: initial_flows.enabled is False — the legacy full-LLM path is used.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
