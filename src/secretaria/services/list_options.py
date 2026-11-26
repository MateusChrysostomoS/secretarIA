"""Paged option lists for the Portal (TASK-082): one resolver, three readers.

A Portal catalogue card (services, doctors, convênios) is recorded with page 1 of
its rows and a pointer, `Message.interactive["source"]`:

    {"v": 1, "kind": "service", "scope": {...server only...},
     "pinned_ids": ["svchelp|0"], "total": 37, "next_cursor": "<opaque>"}

This module turns that pointer back into the ordered catalogue. Its readers:

  * the card itself (`workers/shared/bubbles.py`): `card_source` / `first_page_rows`
    over the `OptionSource` the router attached - the router built those rows with
    the SAME `flow_router.portal_*_rows` helpers `resolve` calls, so page 1 of the
    card is page 1 of the route;
  * the options route (`api/internal.py`, `GET /internal/brain-message/messages/
    {id}/options`): `options_page`, with `q` searched over the WHOLE list;
  * the Portal tap check (`workers/portal/inbound.py`): `contains`, so a row loaded
    after page 1 is honoured only if it is really in that card's list.

`scope` is decided by the router and stored server-side; nothing here ever accepts
one from a client. The tenant boundary is the caller's: `tenant` and
`professionals` are THIS tenant's snapshots (the same `route()` receives), so an id
of another clinic simply is not in the list. Search text is never logged.

Contract: tasks/TASK-082/CONTRACT.md §c-§d.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from secretaria.ai.formatter import OPTION_PAGE_SIZE, OptionSource
from secretaria.services import appointment_edit_flow as aef, flow_router as fr, flow_units
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

KIND_SERVICE = "service"
KIND_PROFESSIONAL = "professional"
KIND_INSURANCE = "insurance"
KINDS = frozenset({KIND_SERVICE, KIND_PROFESSIONAL, KIND_INSURANCE})
# Which row-id prefix belongs to which kind. The fixed rows ("svchelp|0",
# "profhelp|0", "ins|particular", "ins|outro") are on the card itself and pass the
# ordinary window check; they never need re-resolving.
_KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("svc|", KIND_SERVICE),
    ("prof|", KIND_PROFESSIONAL),
    ("ins|", KIND_INSURANCE),
)

SOURCE_VERSION = 1
# Rows per request, and on the card's first page.
PAGE_SIZE = OPTION_PAGE_SIZE
MAX_PAGE_SIZE = OPTION_PAGE_SIZE
# The cursor is an offset; a list longer than this is not a list a patient scrolls.
MAX_CURSOR_OFFSET = 1000
MAX_CURSOR_CHARS = 200
_CURSOR_VERSION = 1
_CURSOR_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")
# A safety net only: brain-api refuses more than 40 with a 422 before it gets here.
MAX_QUERY_CHARS = 60


class InvalidCursor(ValueError):
    """A cursor this module did not write (or one that was edited)."""


@dataclass(frozen=True)
class OptionPage:
    items: list[dict]
    next_cursor: str | None
    total: int


# --------------------------------------------------------------------------
# Cursor: base64url(JSON {"v": 1, "o": <offset>}), no padding
# --------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    payload = json.dumps({"v": _CURSOR_VERSION, "o": int(offset)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """The offset `cursor` stands for; 0 for no cursor. Raises `InvalidCursor`.

    The scope is never in the cursor, so the worst an edited offset can do is move
    inside the same card's list - which the patient may read anyway.
    """
    if cursor is None:
        return 0
    if (
        not isinstance(cursor, str)
        or len(cursor) > MAX_CURSOR_CHARS
        or not _CURSOR_ALPHABET.fullmatch(cursor)
    ):
        raise InvalidCursor
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor from exc
    if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
        raise InvalidCursor
    offset = data.get("o")
    if type(offset) is not int or not 0 <= offset <= MAX_CURSOR_OFFSET:
        raise InvalidCursor
    return offset


# --------------------------------------------------------------------------
# Search text
# --------------------------------------------------------------------------


def _dropped(ch: str) -> bool:
    # Mn/Me: the accents NFKD split off; S*: emoji and other symbols; Cf: the
    # zero-width joiners inside composed emoji.
    category = unicodedata.category(ch)
    return category[0] == "S" or category in ("Mn", "Me", "Cf")


def _fold(text: str) -> str:
    """Accent-, case-, emoji- and spacing-insensitive form of `text`."""
    kept = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not _dropped(ch))
    return " ".join(kept.casefold().split())


def normalize_query(q: str | None) -> str:
    """The comparable form of a search box's text, clamped to MAX_QUERY_CHARS. "" = no filter."""
    return _fold((q or "").strip()[:MAX_QUERY_CHARS])


# --------------------------------------------------------------------------
# Resolution: (kind, scope, tenant snapshot) -> the ordered list
# --------------------------------------------------------------------------


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


SCOPE_DOCTOR_UNIT = "doctor_unit"


# TASK-086: the router writes this key on a card whose rows depend on the convênio matrix
# (doctor marks, "não atende este convênio" service rows, the convênio list of a known
# service) - all of which read each doctor's units.
SCOPE_UNIT_MARKS = "unit_marks"


def needs_unit_options(scope: Any) -> bool:
    """Does resolving `scope` need each doctor's units and service x unit marks?

    The "Alterar dados" services-at-unit list (TASK-083) does, and so does any card the
    convênio matrix marked (TASK-086, `SCOPE_UNIT_MARKS`). Its readers (the options route, the
    Portal tap check) then attach them to the snapshots the same way the worker does
    (`unit_booking.attach_unit_options`); every other card resolves exactly as before.
    """
    if not isinstance(scope, dict):
        return False
    return scope.get("list") == SCOPE_DOCTOR_UNIT or scope.get(SCOPE_UNIT_MARKS) is True


def _scoped_services(scope: dict, tenant: Any, professionals: list) -> list[dict] | None:
    """The service dicts a service card with `scope` listed (see `fr.service_scope`)."""
    which = scope.get("list")
    if which == "tenant":
        return active_appointment_types(tenant)
    if which == "clinic":
        return fr._clinic_service_catalog(tenant, professionals)
    if which == "doctor":
        professional_id = _uuid(scope.get("professional_id"))
        if professional_id is None:
            return None
        professional = fr._find_professional_by_id(professionals, professional_id)
        # A doctor deactivated since the card was sent offers nothing any more.
        return professional_appointment_types(professional, tenant) if professional else []
    if which == SCOPE_DOCTOR_UNIT:
        # TASK-083: "Alterar dados" lists a doctor's services AT one unit (follow-up E1 (b)),
        # re-resolved now: a unit or a service mark removed since narrows it, never widens.
        professional_id = _uuid(scope.get("professional_id"))
        unit_id = _uuid(scope.get("unit_id"))
        if professional_id is None or unit_id is None:
            return None
        professional = fr._find_professional_by_id(professionals, professional_id)
        if professional is None:
            return []
        return flow_units.services_at_unit(
            professional, professional_appointment_types(professional, tenant), unit_id
        )
    return None


def _service_refused(
    scope: dict, tenant: Any, professionals: list, services: list[dict]
) -> frozenset[str]:
    """The service names a matrix-marked card showed as "não atende este convênio" (TASK-086).

    Same doctor the router used (`fr._booking_professional`): the card's own doctor, or the
    clinic's only one on a single-doctor list. No convênio in the scope: no marks (today's)."""
    insurance = scope.get("insurance")
    if not isinstance(insurance, str):
        return frozenset()
    which = scope.get("list")
    unit_id = _uuid(scope.get("unit_id"))
    if unit_id is not None and which in ("doctor", SCOPE_DOCTOR_UNIT):
        # "Alterar consulta" (A12): the card marked the services at ITS unit (the booking's,
        # or the one the services-at-unit list is about), so later pages must ask the same.
        professional_id = _uuid(scope.get("professional_id"))
        doctor = (
            fr._find_professional_by_id(professionals, professional_id)
            if professional_id is not None
            else None
        )
        return frozenset(aef._refused_service_names(tenant, doctor, services, insurance, unit_id))
    professional = None
    if which == "doctor":
        professional_id = _uuid(scope.get("professional_id"))
        if professional_id is not None:
            professional = fr._find_professional_by_id(professionals, professional_id)
    elif which == "tenant" and len(fr.topology_roster(professionals)) == 1:
        professional = fr.topology_roster(professionals)[0]
    return fr.service_refused_names(tenant, professional, services, insurance)


def _scoped_professionals(scope: dict, professionals: list) -> list | None:
    """The roster a doctor card listed; `only_ids` (a pool) keeps the card's own order."""
    only = scope.get("only_ids")
    if only is None:
        return list(professionals)
    if not isinstance(only, list):
        return None
    by_id = {str(p.id): p for p in professionals}
    return [by_id[pid] for pid in only if isinstance(pid, str) and pid in by_id]


def _resolve_named(
    kind: str, scope: Any, tenant: Any, professionals: list | None
) -> list[tuple[tuple, str]]:
    """Every row of the list with the plain name search compares against."""
    if not isinstance(scope, dict):
        return []
    roster = list(professionals or [])
    if kind == KIND_SERVICE:
        services = _scoped_services(scope, tenant, roster)
        if services is None:
            return []
        rows = fr.portal_service_rows(services, _service_refused(scope, tenant, roster, services))
        return [(row, str(s.get("name", ""))) for row, s in zip(rows, services, strict=True)]
    if kind == KIND_PROFESSIONAL:
        insurance = scope.get("insurance")
        pool = _scoped_professionals(scope, roster)
        if pool is None or not (insurance is None or isinstance(insurance, str)):
            return []
        if scope.get("edit") is True:
            # "Alterar consulta": past page 1 there is no live free/busy mark (that needs
            # the appointment's agenda); the convênio mark and the specialty stay.
            service = scope.get("service")
            units = scope.get("units")
            rows = aef.edit_professional_rows(
                tenant,
                pool,
                insurance,
                service=service if isinstance(service, str) else None,
                units=units if isinstance(units, dict) else None,
            )
        else:
            service = scope.get("service")
            rows = fr.portal_professional_rows(
                tenant, pool, insurance, service_name=service if isinstance(service, str) else None
            )
        return [(row, str(p.name or "")) for row, p in zip(rows, pool, strict=True)]
    if kind == KIND_INSURANCE:
        plans = fr._tenant_insurance_plans(tenant)
        if "edit_doctor_id" in scope:
            doctor_id = _uuid(scope.get("edit_doctor_id"))
            doctor = (
                fr._find_professional_by_id(roster, doctor_id) if doctor_id is not None else None
            )
            rows = aef.edit_insurance_rows(
                tenant,
                plans,
                doctor,
                service_id=_uuid(scope.get("service_id")),
                unit_id=_uuid(scope.get("unit_id")),
            )
        else:
            service = scope.get("service")
            if isinstance(service, str):
                # TASK-088: the convênio list of a known booking lists only the plans accepted
                # for (service, doctor, unit) - re-filtered now with the router's own function,
                # so page 1 and every later page agree. Without a doctor in the scope it is the
                # older TASK-086 (A3) filter: accepted somewhere for the service.
                plans = fr.insurance_plans_for_booking(
                    tenant,
                    plans,
                    roster,
                    service,
                    _uuid(scope.get("professional_id")),
                    _uuid(scope.get("unit_id")),
                )
            rows = fr.portal_insurance_rows(plans)
        return [(row, str(plan["name"])) for row, plan in zip(rows, plans, strict=True)]
    return []


def resolve(
    kind: str, scope: Any, tenant: Any, professionals: list | None
) -> list[tuple[str, str, str | None]]:
    """The whole ordered list a card of `kind` with `scope` stands for, as Portal rows.

    Pinned rows are never part of it. An unknown kind or a malformed scope is an
    empty list, never an error: the caller answers that as "not found".
    """
    return [row for row, _name in _resolve_named(kind, scope, tenant, professionals)]


def _item(row: tuple) -> dict:
    return {"id": row[0], "title": row[1], "description": row[2] if len(row) > 2 else None}


def options_page(
    kind: str,
    scope: Any,
    tenant: Any,
    professionals: list | None,
    *,
    cursor: str | None = None,
    page_size: int = PAGE_SIZE,
    q: str | None = None,
) -> OptionPage:
    """One page of the list, filtered by `q` over the WHOLE list. Raises `InvalidCursor`.

    `total` counts the matches (the whole list without `q`). An offset past the end
    is an empty page, not an error.
    """
    offset = decode_cursor(cursor)
    size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    named = _resolve_named(kind, scope, tenant, professionals)
    needle = normalize_query(q)
    if needle:
        named = [(row, name) for row, name in named if needle in _fold(name)]
    total = len(named)
    next_offset = offset + size
    return OptionPage(
        items=[_item(row) for row, _name in named[offset:next_offset]],
        next_cursor=(
            encode_cursor(next_offset)
            if next_offset < total and next_offset <= MAX_CURSOR_OFFSET
            else None
        ),
        total=total,
    )


def kind_for_reply_id(reply_id: str) -> str | None:
    """The list kind a tapped row id belongs to, or None for any other id."""
    for prefix, kind in _KIND_BY_PREFIX:
        if reply_id.startswith(prefix):
            return kind
    return None


def contains(kind: str, scope: Any, tenant: Any, professionals: list | None, reply_id: str) -> bool:
    """Is `reply_id` a row of this card's list - re-resolved now, under its own scope?"""
    if kind_for_reply_id(reply_id) != kind:
        return False
    return any(row[0] == reply_id for row in resolve(kind, scope, tenant, professionals))


# --------------------------------------------------------------------------
# The card: what is stored with page 1
# --------------------------------------------------------------------------


def first_page_rows(source: OptionSource) -> list[tuple]:
    """The rows a Portal card is recorded with: page 1 of the list, then the pinned rows."""
    return list(source.items[:PAGE_SIZE]) + list(source.pinned)


def card_source(source: OptionSource) -> dict:
    """The pointer stored beside page 1 in `Message.interactive["source"]`.

    A convênio card also says whether any of its plans carries a clinic note
    (`has_clinic_notes`, TASK-085 R U1b); every other card's pointer is unchanged.
    TASK-090: the notes no longer exist, so that flag is always False (kept only for
    deploy-window compatibility; follow-up: remove it with the BMF sheet).
    """
    total = len(source.items)
    pointer = {
        "v": SOURCE_VERSION,
        "kind": source.kind,
        "scope": dict(source.scope),
        "pinned_ids": [row[0] for row in source.pinned],
        "total": total,
        "next_cursor": encode_cursor(PAGE_SIZE) if total > PAGE_SIZE else None,
    }
    if source.has_clinic_notes is not None:
        pointer["has_clinic_notes"] = bool(source.has_clinic_notes)
    return pointer


def stored_source(card: Any) -> dict | None:
    """A stored card's pointer when it is one this module can resolve, else None."""
    if not isinstance(card, dict):
        return None
    source = card.get("source")
    if (
        not isinstance(source, dict)
        or source.get("v") != SOURCE_VERSION
        or source.get("kind") not in KINDS
        or not isinstance(source.get("scope"), dict)
    ):
        return None
    return source
