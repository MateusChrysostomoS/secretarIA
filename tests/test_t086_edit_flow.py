"""TASK-086 T4b - "Alterar Dados" honours the convênio acceptance matrix (inventory A9-A12).

Router level, no database: the tenant snapshot carries `insurance_refused` (the clinic's refused
cells, already in the flow's plan-id space) exactly like the worker builds it. Both modes:
`shared` (one clinic matrix keyed by the tenant plan id) and `independent` (one matrix per
doctor). Without any refused cell the edit flow is byte-identical to before (characterization).

The appointment: Dr. A, "Consulta" at Centro, convênio Unimed (p1), Tuesday 2030-10-15 15:20.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.services import (  # noqa: E402
    appointment_edit as ae,
    appointment_edit_flow as aef,  # noqa: E402
    flow_router as fr,
)
from secretaria.services.appointment_edit_flow import apply_ai_edit, edit_step  # noqa: E402
from secretaria.services.insurance_acceptance import EMPTY, AcceptanceExceptions  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests._edit_flow_support import TZ, appointment, conversation, tenant  # noqa: E402

_WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
OPEN = {day: [{"start": "07:00", "end": "20:00"}] for day in _WEEK}

SVC_C = uuid4()  # Consulta
SVC_R = uuid4()  # Retorno
CENTRO = UnitOption(id=uuid4(), name="Centro", address="Rua A, 10", business_hours=OPEN)
SUL = UnitOption(id=uuid4(), name="Zona Sul", address="Av. B, 20", business_hours=OPEN)
NORTE = UnitOption(id=uuid4(), name="Norte", address=None, business_hours=OPEN)
P_UNIMED, P_AMIL = "p1", "p2"
REFUSED = ae.EDIT_ROW_INSURANCE_REFUSED


class _Agenda:
    """Always free; `with_unit` keeps the same agenda (the unit hours are not under test)."""

    tzinfo = TZ

    def __init__(self):
        self.created: list = []
        self.updated: list = []

    def with_unit(self, unit_id, location, *, business_hours=None):
        return self

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        return True

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end))
        return {"id": "evt-new", "htmlLink": None}

    async def update_event_details(self, event_id, start, end, summary, description=""):
        self.updated.append((event_id, start, end))
        return {"id": event_id}


def _types():
    return [
        {"name": "Consulta", "duration_min": 40, "is_active": True, "service_id": str(SVC_C)},
        {"name": "Retorno", "duration_min": 40, "is_active": True, "service_id": str(SVC_R)},
    ]


def _doctor(name, units, service_units=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=_types(),
        business_hours=None,
        min_booking_notice_hours=None,
        units=list(units),
        service_units=service_units or {},
    )


def _shared(*cells, **extra):
    """Mode 1: one clinic matrix, keyed by the tenant plan id."""
    refused = AcceptanceExceptions(
        clinic=frozenset((str(s), str(u.id) if u else None, p) for s, u, p in cells)
    )
    return tenant(
        insurance_mode="shared", insurance_accepted_by={}, insurance_refused=refused, **extra
    )


def _independent(doctors, by_doctor: list, **extra):
    """Mode 2: every doctor takes both plans by their own list; `by_doctor` = (doctor, refused
    cells) pairs."""
    refused = AcceptanceExceptions(
        by_professional={
            str(doc.id): frozenset((str(s), str(u.id) if u else None, p) for s, u, p in cells)
            for doc, cells in by_doctor
        }
    )
    return tenant(
        insurance_mode="independent",
        insurance_accepted_by={str(d.id): [P_UNIMED, P_AMIL] for d in doctors},
        insurance_refused=refused,
        **extra,
    )


def _appt(doctor, unit=CENTRO, **kw):
    base = appointment(professional_id=str(doctor.id), **kw)
    if unit is not None:
        base["unit_id"] = str(unit.id)
    return base


def _ctx(doctors):
    return ae.EditContext(calendars={str(d.id): _Agenda() for d in doctors})


async def _step(snapshot, step, body, *, appt, doctors, draft=None, ctx=None):
    draft = draft or ae.EditDraft.from_appointment(appt, TZ)
    conv = conversation(draft, step)
    return await edit_step(conv, snapshot, body, [appt], doctors, ctx or _ctx(doctors), "Maria")


def _draft(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


def _descriptions(rows) -> dict:
    return {row[0]: (row[2] if len(row) > 2 else None) for row in rows}


def _tap(doctor) -> str:
    return f"{fr._professional_row_title(doctor.name)} ({doctor.id})"


# --- Characterization: no refused cell = exactly today's edit flow ------------------------


async def test_no_exceptions_lists_are_identical_to_a_snapshot_without_the_matrix():
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [SUL])
    appt = _appt(a)
    for step, body in (
        (fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR),
        (fr.STEP_EDIT_MENU, ae.LABEL_EDIT_SERVICE),
        (fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE),
    ):
        plain = await _step(tenant(), step, body, appt=appt, doctors=[a, b])
        with_empty = await _step(
            tenant(insurance_refused=EMPTY), step, body, appt=appt, doctors=[a, b]
        )
        assert plain.flow_step == with_empty.flow_step
        assert plain.flow_edit_draft == with_empty.flow_edit_draft
        [bubble_plain] = plain.bubbles
        [bubble_empty] = with_empty.bubbles
        assert bubble_plain.rows == bubble_empty.rows
        assert bubble_plain.source.items == bubble_empty.source.items
        assert bubble_plain.source.scope == bubble_empty.source.scope


async def test_no_exceptions_a_doctor_insurance_and_service_change_still_reach_the_card():
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [CENTRO])
    appt = _appt(a)
    snapshot = _shared()  # shared mode, matrix untouched
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(
        professional_id=str(b.id), service="Retorno", insurance="Amil"
    )
    result = aef._confirm_result(draft, snapshot, [a, b], _ctx([a, b]))
    assert result.flow_step == fr.STEP_EDIT_CONFIRM


# --- A9: the doctor list's "aceita o convênio" -------------------------------------------


async def test_doctor_list_mark_follows_the_unit_the_booking_would_have_shared():
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [SUL])
    snapshot = _shared((SVC_C, SUL, P_UNIMED))
    result = await _step(
        snapshot, fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, appt=_appt(a), doctors=[a, b]
    )

    [bubble] = result.bubbles
    rows = _descriptions(bubble.rows)
    assert "aceita o convênio" in rows[f"prof|{a.id}"]
    assert "aceita o convênio" not in (rows[f"prof|{b.id}"] or "")
    items = _descriptions(bubble.source.items)
    assert "aceita o convênio" in items[f"prof|{a.id}"]
    assert "aceita o convênio" not in (items[f"prof|{b.id}"] or "")


async def test_doctor_list_mark_is_per_doctor_in_independent_mode():
    a, b = _doctor("Dr. A", [SUL]), _doctor("Dra. B", [SUL])
    # Only Dr. A refuses Unimed for Consulta at Zona Sul; Dra. B's matrix is hers alone.
    snapshot = _independent([a, b], [(a, [(SVC_C, SUL, P_UNIMED)])])
    result = await _step(
        snapshot, fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, appt=_appt(a, SUL), doctors=[a, b]
    )

    rows = _descriptions(result.bubbles[0].rows)
    assert "aceita o convênio" not in (rows[f"prof|{a.id}"] or "")
    assert "aceita o convênio" in rows[f"prof|{b.id}"]


def test_edit_professional_rows_without_context_mark_a_doctor_refusing_every_service():
    """The Portal's later pages (list_options) pass no service: "aceita" = in at least one."""
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [CENTRO])
    snapshot = _shared((SVC_C, CENTRO, P_UNIMED))  # Retorno still takes Unimed at Centro
    rows = _descriptions(aef.edit_professional_rows(snapshot, [a, b], "Unimed"))
    assert "aceita o convênio" in rows[f"prof|{a.id}"]

    snapshot = _independent([a, b], [(a, [(SVC_C, CENTRO, P_UNIMED), (SVC_R, CENTRO, P_UNIMED)])])
    rows = _descriptions(aef.edit_professional_rows(snapshot, [a, b], "Unimed"))
    assert "aceita o convênio" not in (rows[f"prof|{a.id}"] or "")
    assert "aceita o convênio" in rows[f"prof|{b.id}"]


# --- A10: the convênio list's mark -------------------------------------------------------


async def test_insurance_marks_consider_the_drafts_service_and_unit():
    a = _doctor("Dr. A", [CENTRO, SUL])
    appt = _appt(a)

    async def marks(snapshot):
        result = await _step(
            snapshot, fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE, appt=appt, doctors=[a]
        )
        [bubble] = result.bubbles
        assert (
            _descriptions(bubble.rows)["ins|Amil"] == _descriptions(bubble.source.items)["ins|Amil"]
        )
        return _descriptions(bubble.rows)

    refused_here = await marks(_shared((SVC_C, CENTRO, P_AMIL)))
    assert refused_here["ins|Unimed"] == f"{ae.MARK_FREE} aceito pelo médico"
    assert refused_here["ins|Amil"] == f"{ae.MARK_BUSY} não aceito pelo médico"
    # Another service, or another unit, refusing Amil does not touch this booking.
    other_service = await marks(_shared((SVC_R, CENTRO, P_AMIL)))
    assert other_service["ins|Amil"] == f"{ae.MARK_FREE} aceito pelo médico"
    assert (await marks(_shared((SVC_C, SUL, P_AMIL))))["ins|Amil"] == (
        f"{ae.MARK_FREE} aceito pelo médico"
    )
    independent = await marks(_independent([a], [(a, [(SVC_C, CENTRO, P_AMIL)])]))
    assert independent["ins|Amil"] == f"{ae.MARK_BUSY} não aceito pelo médico"


# --- A11: the card refuses a draft the matrix does not accept -----------------------------


async def test_changing_to_a_refused_convenio_is_refused_and_the_convenio_kept_shared():
    a = _doctor("Dr. A", [CENTRO])
    result = await _step(
        _shared((SVC_C, CENTRO, P_AMIL)),
        fr.STEP_EDIT_INSURANCE,
        "Amil",
        appt=_appt(a),
        doctors=[a],
    )
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert _draft(result).current["insurance"] == "Unimed"


async def test_changing_to_a_refused_convenio_is_refused_independent():
    a = _doctor("Dr. A", [CENTRO])
    result = await _step(
        _independent([a], [(a, [(SVC_C, CENTRO, P_AMIL)])]),
        fr.STEP_EDIT_INSURANCE,
        "Amil",
        appt=_appt(a),
        doctors=[a],
    )
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert _draft(result).current["insurance"] == "Unimed"


async def test_a_pure_reschedule_is_never_refused_even_if_the_original_cell_is_now_refused():
    a = _doctor("Dr. A", [CENTRO])
    appt = _appt(a)
    snapshot = _shared((SVC_C, CENTRO, P_UNIMED))
    moved = ae.EditDraft.from_appointment(appt, TZ).with_changes(
        start_at="2030-10-16T09:00", end_at="2030-10-16T09:40"
    )

    card = aef._confirm_result(moved, snapshot, [a], _ctx([a]))
    assert card.flow_step == fr.STEP_EDIT_CONFIRM

    confirmed = await _step(
        snapshot, fr.STEP_EDIT_CONFIRM, fr.LABEL_CONFIRM, appt=appt, doctors=[a], draft=moved
    )
    assert confirmed.appointment_edit is not None
    assert confirmed.appointment_edit["service_id"] == SVC_C


async def test_a_doctor_change_into_a_refused_cell_goes_back_to_the_appointments_doctor():
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [SUL])
    appt = _appt(a)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(
        professional_id=str(b.id), unit_id=str(SUL.id)
    )
    result = aef._confirm_result(draft, _shared((SVC_C, SUL, P_UNIMED)), [a, b], _ctx([a, b]))

    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    kept = _draft(result)
    assert kept.current["professional_id"] == str(a.id)
    assert kept.current["unit_id"] == str(CENTRO.id)


async def test_a_stale_card_is_refused_at_confirmar_when_the_matrix_changed_since():
    a = _doctor("Dr. A", [CENTRO])
    appt = _appt(a)
    ctx = _ctx([a])
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(insurance="Amil")
    # The card was drawn while Amil was accepted ...
    card = aef._confirm_result(draft, _shared(), [a], ctx)
    assert card.flow_step == fr.STEP_EDIT_CONFIRM
    # ... and the clinic refused it before "Confirmar".
    result = await _step(
        _shared((SVC_C, CENTRO, P_AMIL)),
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[a],
        draft=draft,
        ctx=ctx,
    )
    assert result.appointment_edit is None
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert ctx.calendars[str(a.id)].updated == []


async def test_particular_and_a_typed_convenio_are_never_refused():
    a = _doctor("Dr. A", [CENTRO])
    snapshot = _shared((SVC_C, CENTRO, P_UNIMED), (SVC_C, CENTRO, P_AMIL))
    appt = _appt(a, insurance="Amil")
    for insurance in (fr.LABEL_INSURANCE_PARTICULAR, "Plano do Bairro"):
        draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(insurance=insurance)
        assert aef._confirm_result(draft, snapshot, [a], _ctx([a])).flow_step == (
            fr.STEP_EDIT_CONFIRM
        )


async def test_an_ai_proposed_refused_convenio_is_refused_like_the_tap():
    a = _doctor("Dr. A", [CENTRO])
    appt = _appt(a)
    draft = ae.EditDraft.from_appointment(appt, TZ)
    conv = conversation(draft, fr.STEP_EDIT_MENU)
    result = await apply_ai_edit(
        conv, _shared((SVC_C, CENTRO, P_AMIL)), {"insurance": "Amil"}, [appt], [a], _ctx([a])
    )
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert _draft(result).current["insurance"] == "Unimed"


# --- A12: service and unit lists ---------------------------------------------------------


async def test_service_list_marks_a_service_the_doctor_does_not_take_the_convenio_for():
    a = _doctor("Dr. A", [CENTRO])
    result = await _step(
        _shared((SVC_R, CENTRO, P_UNIMED)),
        fr.STEP_EDIT_MENU,
        ae.LABEL_EDIT_SERVICE,
        appt=_appt(a),
        doctors=[a],
    )
    [bubble] = result.bubbles
    rows = _descriptions(bubble.rows)
    assert rows["svc|Retorno"] == REFUSED
    assert rows["svc|Consulta"] is None
    items = _descriptions(bubble.source.items)
    assert items["svc|Retorno"] == REFUSED and items["svc|Consulta"] is None


async def test_picking_a_service_refused_at_the_bookings_unit_offers_the_units_that_take_it():
    a = _doctor("Dr. A", [CENTRO, SUL])
    appt = _appt(a)
    snapshot = _shared((SVC_R, CENTRO, P_UNIMED))
    listed = await _step(snapshot, fr.STEP_EDIT_SERVICE, "Retorno", appt=appt, doctors=[a])

    assert listed.flow_step == fr.STEP_EDIT_UNIT
    assert [row[0] for row in listed.bubbles[0].rows] == [f"unit|{SUL.id}"]

    picked = await _step(
        snapshot, fr.STEP_EDIT_UNIT, "Zona Sul", appt=appt, doctors=[a], draft=_draft(listed)
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(picked).current["unit_id"] == str(SUL.id)
    assert _draft(picked).current["service"] == "Retorno"

    # A typed unit the convênio is refused at never matches the filtered list.
    typed = await _step(
        snapshot, fr.STEP_EDIT_UNIT, "Centro", appt=appt, doctors=[a], draft=_draft(listed)
    )
    assert typed.flow_step != fr.STEP_EDIT_CONFIRM


async def test_a_service_refused_at_every_unit_keeps_the_draft_with_a_plain_notice():
    a = _doctor("Dr. A", [CENTRO, SUL])
    appt = _appt(a)
    result = await _step(
        _shared((SVC_R, CENTRO, P_UNIMED), (SVC_R, SUL, P_UNIMED)),
        fr.STEP_EDIT_SERVICE,
        "Retorno",
        appt=appt,
        doctors=[a],
    )
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_AT_UNITS
    assert _draft(result).current["service"] == "Consulta"


async def test_the_new_doctors_unit_list_only_shows_units_that_take_the_convenio():
    a = _doctor("Dr. A", [CENTRO])
    b = _doctor("Dra. B", [SUL, NORTE])
    snapshot = _independent([a, b], [(b, [(SVC_C, NORTE, P_UNIMED), (SVC_R, NORTE, P_UNIMED)])])
    result = await _step(snapshot, fr.STEP_EDIT_DOCTOR, _tap(b), appt=_appt(a), doctors=[a, b])

    assert result.flow_step == fr.STEP_EDIT_UNIT
    assert [row[0] for row in result.bubbles[0].rows] == [f"unit|{SUL.id}"]


async def test_services_at_a_unit_list_marks_the_refused_ones():
    a = _doctor("Dr. A", [CENTRO])
    b = _doctor("Dra. B", [SUL, NORTE])
    snapshot = _shared((SVC_R, SUL, P_UNIMED))
    appt = _appt(a)
    listed = await _step(snapshot, fr.STEP_EDIT_DOCTOR, _tap(b), appt=appt, doctors=[a, b])
    services = await _step(
        snapshot, fr.STEP_EDIT_UNIT, "Zona Sul", appt=appt, doctors=[a, b], draft=_draft(listed)
    )
    assert services.flow_step == fr.STEP_EDIT_SERVICE
    rows = _descriptions(services.bubbles[0].rows)
    assert rows["svc|Retorno"] == REFUSED
    assert rows["svc|Consulta"] is None


async def test_an_ai_proposed_service_refused_for_a_doctor_without_units_says_why():
    """No unit at all: the "no unit" slice decides, and the words are the convênio's own."""
    a = _doctor("Dr. A", [])
    appt = _appt(a, unit=None)
    conv = conversation(ae.EditDraft.from_appointment(appt, TZ), fr.STEP_EDIT_MENU)
    result = await apply_ai_edit(
        conv, _shared((SVC_R, None, P_UNIMED)), {"service": "Retorno"}, [appt], [a], _ctx([a])
    )
    assert result.bubbles[0].body == ae.EDIT_INSURANCE_NOT_ACCEPTED
    assert _draft(result).current["service"] == "Consulta"


# --- The card's scope carries what a re-resolution (list_options) needs ------------------


async def test_matrix_marked_cards_carry_their_context_in_the_scope_and_round_trip():
    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [SUL])
    snapshot = _shared((SVC_C, SUL, P_UNIMED), (SVC_C, CENTRO, P_AMIL))
    appt = _appt(a)

    doctors = await _step(
        snapshot, fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, appt=appt, doctors=[a, b]
    )
    scope = doctors.bubbles[0].source.scope
    assert scope["unit_marks"] is True and scope["service"] == "Consulta"
    assert scope["units"] == {str(a.id): str(CENTRO.id), str(b.id): str(SUL.id)}
    again = aef.edit_professional_rows(
        snapshot, [a, b], scope["insurance"], service=scope["service"], units=scope["units"]
    )
    # Past page 1 (no live free/busy mark) the convênio marks are the card's own.
    assert _descriptions(again) == {f"prof|{a.id}": "aceita o convênio", f"prof|{b.id}": None}

    plans = await _step(
        snapshot, fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE, appt=appt, doctors=[a]
    )
    scope = plans.bubbles[0].source.scope
    assert scope["unit_marks"] is True
    assert (scope["service_id"], scope["unit_id"]) == (str(SVC_C), str(CENTRO.id))
    again = aef.edit_insurance_rows(
        snapshot,
        fr._tenant_insurance_plans(snapshot),
        a,
        service_id=scope["service_id"],
        unit_id=scope["unit_id"],
    )
    assert again == plans.bubbles[0].source.items


async def test_portal_later_pages_resolve_the_same_marks_as_the_card():
    """Integration (list_options, TASK-086): the Portal's pages/taps after page 1 re-resolve the
    edit doctor and convênio lists through `list_options` with the card's scope - same marks."""
    from secretaria.services import list_options

    a, b = _doctor("Dr. A", [CENTRO]), _doctor("Dra. B", [SUL])
    snapshot = _shared((SVC_C, SUL, P_UNIMED), (SVC_C, CENTRO, P_AMIL))
    appt = _appt(a)

    doctors = await _step(
        snapshot, fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, appt=appt, doctors=[a, b]
    )
    source = doctors.bubbles[0].source
    rows = list_options.resolve(list_options.KIND_PROFESSIONAL, source.scope, snapshot, [a, b])
    assert _descriptions(rows) == {f"prof|{a.id}": "aceita o convênio", f"prof|{b.id}": None}

    plans = await _step(
        snapshot, fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE, appt=appt, doctors=[a]
    )
    source = plans.bubbles[0].source
    rows = list_options.resolve(list_options.KIND_INSURANCE, source.scope, snapshot, [a])
    assert rows == source.items
