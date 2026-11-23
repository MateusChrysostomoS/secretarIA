"""Backfill a "Sede" unit from the clinic address; reset paid-notice auto-approval (TASK-090 M2).

Data only (no schema change), idempotent:

1. Every tenant with a legacy `tenants.address` (at least a street line OR a city) and ZERO
   units (active or not) gets ONE unit named "Sede", active, filled from that address. The legacy
   JSON is `{line, complement, neighborhood, city, state, postal_code}`; there is no separate
   number, so `line` (street + number in free text) is split by `split_line`. The Sede is created
   WITHOUT the API's 422 validation: if the number or the CEP is missing/invalid it is created
   anyway with whatever exists, and the API then reports it as `address_incomplete`. Its text
   `address` is derived in the same format as `services/unit_address.format_unit_address`.
   A tenant that already has any unit is never touched, so running the upgrade again (or
   downgrade then upgrade) never creates a second Sede.
2. `tenants.paid_notices_auto_approved` saved as true becomes false (owner-approved safe default:
   the agenda asks before sending a billed notice; the server logic is unchanged).

This file is self-contained on purpose: the helpers below are copies, the migration must keep
working as the app code evolves (never import from `secretaria`).

DEPLOY ORDER: runs with the other additive revisions (`alembic upgrade head` = a3c9e5d7f1b4),
before the API and the worker. (The DROP of `custom_payment_note` once planned after it was
deleted by TASK-090 Adendo 1: that column stays.) A Sede created here usually has no CEP, so it
is INCOMPLETE and, until a manager completes it, the secretária does not answer that clinic
(Adendo 1 §A).

Downgrade restores NOTHING: it does not remove the Sede (by then it is ordinary clinic data,
possibly edited) and it cannot bring back the old `true` flags (the previous value is not kept).
It is a no-op and never fails. Consequence, acceptable for a one-time hand-run migration:
running upgrade again after a downgrade would recreate a Sede for a clinic that has since deleted
all its units, and would reset again any flag set back to true.

Revision ID: f2b8d4c6e0a3
Revises: e1a7c3b9d5f2
Create Date: 2026-11-22
"""

import re
import unicodedata
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8d4c6e0a3"
down_revision: str = "e1a7c3b9d5f2"
branch_labels: str | None = None
depends_on: str | None = None

SEDE_NAME = "Sede"
_MAX_ADDRESS = 512  # units.address column
# Max length of each copied part = its `units` column, so an oversized legacy value is cut
# instead of failing the whole migration on Postgres.
_MAX_PART = {"street": 255, "number": 30, "complement": 255, "neighborhood": 255, "city": 255}

_UF_BY_NAME = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}
_UFS = frozenset(_UF_BY_NAME.values())

# "<street>[,] <number>" where the number is the last token: digits (+ optional letter) or s/n.
_LINE_WITH_NUMBER = re.compile(
    r"^(?P<street>.+?)(?:\s*,\s*|\s+)(?P<number>\d{1,10}[A-Za-z]?|[sS]/?[nN])\s*$"
)
_COMPLEMENT_WORDS = frozenset(
    {"apto", "ap", "apartamento", "sala", "bloco", "bl", "cj", "conjunto"}
    | {"lote", "quadra", "andar"}
)
_MIN_STREET_LENGTH = 4  # "Rua 15" is a street named 15, not street "Rua" + number 15


def split_line(line: str | None) -> tuple[str | None, str | None]:
    """Split a free-text street line into (street, number).

    "Av. Paulista, 1000" -> ("Av. Paulista", "1000"); "Rua A 45" -> ("Rua A", "45");
    no trailing number -> (line, None); None/blank -> (None, None).
    """
    text = " ".join((line or "").split())
    if not text:
        return None, None
    match = _LINE_WITH_NUMBER.match(text)
    if match:
        street = match.group("street").rstrip(" ,")
        # The tail is not a street number when the street part still holds a comma
        # ("Rua B 10, apto 2") or ends with a complement keyword ("Quadra 10 Lote 5"): leave the
        # line whole rather than mislabel a complement as the number.
        last_word = street.rsplit(None, 1)[-1].rstrip(".").lower()
        if (
            len(street) >= _MIN_STREET_LENGTH
            and "," not in street
            and last_word not in _COMPLEMENT_WORDS
        ):
            return street, match.group("number")
    return text, None


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _cep(raw: object) -> str | None:
    digits = re.sub(r"[^0-9]", "", raw) if isinstance(raw, str) else ""
    return digits if len(digits) == 8 else None


def _uf(raw: object) -> str | None:
    """UF (2 letters) from "sp"/"SP" or a full state name ("São Paulo"); else None."""
    text = _clean(raw)
    if text is None:
        return None
    if text.upper() in _UFS:
        return text.upper()
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return _UF_BY_NAME.get(" ".join(folded.split()))


def _derived_line(parts: dict[str, str | None]) -> str:
    """Same format as services/unit_address.format_unit_address (empty parts omitted)."""
    number_part = " - ".join(x for x in (parts["number"], parts["complement"]) if x)
    city_part = " - ".join(x for x in (parts["city"], parts["state"]) if x)
    cep = parts["cep"]
    cep_part = f"{cep[:5]}-{cep[5:]}" if cep else None
    segments = (parts["street"], number_part, parts["neighborhood"], city_part, cep_part)
    return ", ".join(s for s in segments if s)[:_MAX_ADDRESS]


def sede_parts(address: object) -> dict[str, str | None] | None:
    """Structured parts for the Sede from a legacy `tenants.address`, or None when it holds
    neither a street line nor a city (nothing worth creating a unit for)."""
    if not isinstance(address, dict):
        return None
    street, number = split_line(_clean(address.get("line")))
    city = _clean(address.get("city"))
    if street is None and city is None:
        return None
    parts: dict[str, str | None] = {
        "cep": _cep(address.get("postal_code")),
        "street": street,
        "number": number,
        "complement": _clean(address.get("complement")),
        "neighborhood": _clean(address.get("neighborhood")),
        "city": city,
        "state": _uf(address.get("state")),
    }
    for name, limit in _MAX_PART.items():
        value = parts[name]
        if value is not None:
            parts[name] = value[:limit].rstrip() or None
    return parts


_tenants = sa.table(
    "tenants",
    sa.column("id", sa.Uuid()),
    sa.column("address", sa.JSON()),
    sa.column("paid_notices_auto_approved", sa.Boolean()),
)
_units = sa.table(
    "units",
    sa.column("id", sa.Uuid()),
    sa.column("tenant_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("address", sa.String()),
    sa.column("cep", sa.String()),
    sa.column("street", sa.String()),
    sa.column("number", sa.String()),
    sa.column("complement", sa.String()),
    sa.column("neighborhood", sa.String()),
    sa.column("city", sa.String()),
    sa.column("state", sa.String()),
    sa.column("is_active", sa.Boolean()),
)


def upgrade() -> None:
    conn = op.get_bind()

    has_unit = sa.exists().where(_units.c.tenant_id == _tenants.c.id)
    rows = conn.execute(
        sa.select(_tenants.c.id, _tenants.c.address).where(
            _tenants.c.address.is_not(None), ~has_unit
        )
    ).all()
    for tenant_id, address in rows:
        parts = sede_parts(address)
        if parts is None:
            continue
        conn.execute(
            sa.insert(_units).values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=SEDE_NAME,
                address=_derived_line(parts) or None,
                is_active=True,
                **parts,
            )
        )

    conn.execute(
        sa.update(_tenants)
        .where(_tenants.c.paid_notices_auto_approved.is_(True))
        .values(paid_notices_auto_approved=False)
    )


def downgrade() -> None:
    """Restores nothing (see the module docstring)."""
