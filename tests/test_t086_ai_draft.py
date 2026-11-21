"""TASK-086 A8 (W6): an AI draft whose convênio the matrix refuses never lands on the express card.

When service + doctor + convênio arrive together and the matrix refuses the combination (unit
known -> that unit; unit unknown -> every unit the service can be booked at, or the "no unit"
slice for a doctor without links), the resolver lands on the convênio step with one short line,
keeping the service and the doctor. Both modes: `shared` (clinic matrix) and `independent`
(per-doctor matrix). Pure resolver, fake agenda, no database.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, draft_from_record  # noqa: E402
from secretaria.services.flow_units import STEP_AWAITING_UNIT  # noqa: E402
from secretaria.services.insurance_acceptance import AcceptanceExceptions  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests.test_booking_draft_resolver import (  # noqa: E402
    DAY,
    NOW,
    _conv,
    _doctor,
    _resolve,
    _sole,
    _tenant,
)

TEN = dt.time(10, 0)
CONSULTA = uuid4()
RETORNO = uuid4()
PLAN = "plan-unimed"
PLAN_B = "plan-bradesco"
HOURS = {"thursday": [{"start": "08:00", "end": "12:00"}]}
SERVICES = [
    {
        "name": "Consulta",
        "duration_min": 30,
        "is_active": True,
        "sort_order": 0,
        "service_id": str(CONSULTA),
    },
    {
        "name": "Retorno",
        "duration_min": 20,
        "is_active": True,
        "sort_order": 1,
        "service_id": str(RETORNO),
    },
]
FULL = BookingDraft(service="Consulta", insurance="unimed", day=DAY, time=TEN)


def _clinic(mode="shared", refused=None, **kw):
    return _tenant(
        initial_flows={"ai_draft_v2": True},
        appointment_types=[dict(entry) for entry in SERVICES],
        collect_insurance=True,
        insurance_mode=mode,
        # TASK-088: a SECOND plan the matrix never refuses, so a refused Unimed still has a list to
        # be asked again from (with nothing else accepted the booking goes Particular instead -
        # `test_a_refused_combo_with_no_other_plan_goes_particular_and_says_so`).
        insurances=["Unimed", "Bradesco"],
        insurance_plans=[{"id": PLAN, "name": "Unimed"}, {"id": PLAN_B, "name": "Bradesco"}],
        insurance_refused=refused,
        **kw,
    )


def _takes_both(*doctors):
    """`independent` mode: the doctor's OWN list decides - these doctors take both plans."""
    return {str(doctor.id): [PLAN, PLAN_B] for doctor in doctors}


def _shared(*cells):
    return AcceptanceExceptions(clinic=frozenset(cells))


def _independent(professional, *cells):
    return AcceptanceExceptions(by_professional={str(professional.id): frozenset(cells)})


def _with_units(professional, *units):
    professional.units = list(units)
    professional.service_units = {}
    return professional


def _unit(name="Centro"):
    return UnitOption(uuid4(), name, None, HOURS)


def _assert_convenio_step(res, *, service="Consulta"):
    """The convênio asked again (TASK-088): the list of what is accepted - the refused plan is
    not in it - with the refusal line as its body."""
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    [list_bubble] = [b for b in res.result.bubbles if isinstance(b, SlotsBubble)]
    assert list_bubble.body in (fr.INSURANCE_REFUSED_LIST, fr.INSURANCE_REFUSED_LIST_UNIT)
    ids = [row[0] for row in list_bubble.rows]
    assert "ins|Unimed" not in ids and "ins|Bradesco" in ids
    assert res.result.flow_selected_type == service
    # The refused convênio is asked again, never kept behind the question.
    assert res.result.flow_selected_insurance is None
    assert res.result.flow_selected_slot is None


# --------------------------------------------------------------------------
# Characterization: nothing refused -> the express card, as before
# --------------------------------------------------------------------------


async def test_no_exceptions_still_lands_on_the_express_card():
    res = await _resolve(FULL, tenant=_clinic(refused=None), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == "Unimed"


async def test_a_refusal_for_another_service_does_not_block():
    sole = _sole()
    res = await _resolve(
        FULL, tenant=_clinic(refused=_shared((str(RETORNO), None, PLAN))), pros=[sole]
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION


async def test_particular_is_never_refused():
    sole = _sole()
    draft = BookingDraft(service="Consulta", insurance="Particular", day=DAY, time=TEN)
    res = await _resolve(
        draft, tenant=_clinic(refused=_shared((str(CONSULTA), None, PLAN))), pros=[sole]
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION


# --------------------------------------------------------------------------
# Refused -> the convênio step, both modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_refused_combo_lands_on_the_convenio_step_not_the_card(mode):
    sole = _sole()
    cell = (str(CONSULTA), None, PLAN)
    refused = _shared(cell) if mode == "shared" else _independent(sole, cell)
    res = await _resolve(
        FULL,
        tenant=_clinic(mode, refused, insurance_accepted_by=_takes_both(sole)),
        pros=[sole],
    )
    _assert_convenio_step(res)
    assert "insurance" not in res.accepted


async def test_independent_matrix_of_another_doctor_does_not_refuse():
    sole, other = _sole(), _sole()
    refused = _independent(other, (str(CONSULTA), None, PLAN))
    res = await _resolve(
        FULL,
        tenant=_clinic("independent", refused, insurance_accepted_by=_takes_both(sole)),
        pros=[sole],
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == "Unimed"


async def test_a_recorded_convenio_is_checked_too():
    sole = _sole()
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        conv=_conv(flow_selected_insurance="Unimed"),
        tenant=_clinic(refused=_shared((str(CONSULTA), None, PLAN))),
        pros=[sole],
    )
    _assert_convenio_step(res)


async def test_the_parked_draft_keeps_the_day_and_drops_the_convenio():
    sole = _sole()
    res = await _resolve(
        FULL, tenant=_clinic(refused=_shared((str(CONSULTA), None, PLAN))), pros=[sole]
    )
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert parked is not None
    assert (parked.service, parked.day, parked.time) == ("Consulta", DAY, TEN)
    assert parked.insurance is None


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_the_only_unit_refuses_lands_on_the_convenio_step(mode):
    centro = _unit()
    sole = _with_units(_sole(), centro)
    cell = (str(CONSULTA), str(centro.id), PLAN)
    refused = _shared(cell) if mode == "shared" else _independent(sole, cell)
    res = await _resolve(
        FULL,
        tenant=_clinic(mode, refused, insurance_accepted_by=_takes_both(sole)),
        pros=[sole],
    )
    _assert_convenio_step(res)


async def test_unit_unknown_and_refused_everywhere_asks_the_unit_first():
    # TASK-088: the unit comes first, whatever the convênio; the refusal is judged at the pick.
    centro, sul = _unit("Centro"), _unit("Sul")
    sole = _with_units(_sole(), centro, sul)
    refused = _shared((str(CONSULTA), str(centro.id), PLAN), (str(CONSULTA), str(sul.id), PLAN))
    res = await _resolve(FULL, tenant=_clinic(refused=refused), pros=[sole])
    assert res.landing_step == STEP_AWAITING_UNIT
    picked = await _resolve(
        FULL,
        conv=_conv(flow_selected_unit_id=sul.id),
        tenant=_clinic(refused=refused),
        pros=[sole],
    )
    _assert_convenio_step(picked)


async def test_a_refused_combo_with_no_other_plan_goes_particular_and_says_so():
    """A clinic whose only plan is refused: no list to choose from - Particular, with the line."""
    sole = _sole()
    refused = _shared((str(CONSULTA), None, PLAN), (str(CONSULTA), None, PLAN_B))
    res = await _resolve(FULL, tenant=_clinic(refused=refused), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    first = res.result.bubbles[0]
    assert isinstance(first, TextBubble) and first.body == fr.INSURANCE_REFUSED_PARTICULAR


async def test_unit_unknown_and_accepted_somewhere_asks_the_unit():
    # TASK-088: the unit comes BEFORE the convênio and is never narrowed by it (the old T4a unit
    # filter / convênio-specific confirm card are gone from this path): Centro refuses the plan,
    # but both units are listed - the plan is judged at the unit the patient picks.
    centro, sul = _unit("Centro"), _unit("Sul")
    sole = _with_units(_sole(), centro, sul)
    refused = _shared((str(CONSULTA), str(centro.id), PLAN))
    res = await _resolve(FULL, tenant=_clinic(refused=refused), pros=[sole])
    assert res.landing_step == STEP_AWAITING_UNIT


async def test_a_picked_unit_that_refuses_lands_on_the_convenio_step():
    centro, sul = _unit("Centro"), _unit("Sul")
    sole = _with_units(_sole(), centro, sul)
    refused = _shared((str(CONSULTA), str(centro.id), PLAN))
    res = await _resolve(
        FULL,
        conv=_conv(flow_selected_unit_id=centro.id),
        tenant=_clinic(refused=refused),
        pros=[sole],
    )
    _assert_convenio_step(res)


# --------------------------------------------------------------------------
# Multi-doctor clinic: the doctor stays chosen
# --------------------------------------------------------------------------


async def test_multi_doctor_refusal_keeps_the_doctor():
    ana = _doctor("Dra. Ana", [])
    ana.appointment_types = [dict(SERVICES[0])]
    beto = _doctor("Dr. Beto", [])
    beto.appointment_types = [dict(SERVICES[0])]
    refused = _independent(ana, (str(CONSULTA), None, PLAN))
    draft = BookingDraft(
        service="Consulta", professional_id=ana.id, insurance="unimed", day=DAY, time=TEN
    )
    res = await _resolve(
        draft,
        tenant=_clinic("independent", refused, insurance_accepted_by=_takes_both(ana, beto)),
        pros=[ana, beto],
    )
    _assert_convenio_step(res)
    assert res.result.flow_selected_professional_id == ana.id
    # Dr. Beto takes it: the same draft for him books as before.
    other = await _resolve(
        BookingDraft(
            service="Consulta", professional_id=beto.id, insurance="unimed", day=DAY, time=TEN
        ),
        tenant=_clinic("independent", refused, insurance_accepted_by=_takes_both(ana, beto)),
        pros=[ana, beto],
    )
    assert other.landing_step == fr.STEP_AWAITING_CONFIRMATION
