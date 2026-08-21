"""Rebooking after the DOCTOR cancelled, and the reason when the patient won't.

Ground truth: services/flow_router.py (`rebooking_candidates`,
`enter_rebooking`, `enter_decline_reasons`, `_handle_decline_reason`) and
workers/tasks.py::_handle_action_button's rebook* branches.

The invariant every test here defends: NONE of these paths reach the LLM. A tap
on the cancellation notice lands in the deterministic branch, keeps what the
patient already chose, and either opens a day list or answers with a fixed
line. The cancelled appointment is never reopened — confirming later produces a
new booking through the normal tail.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")
_SERVICE = "Limpeza"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(**kw):
    base = dict(
        initial_flows={"buttons": ["Serviços e Custo", "Remarcar/Cancelar", "Outro"]},
        appointment_types=[
            {"name": _SERVICE, "duration_min": 45, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurances=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _professional(name, *, offers=(_SERVICE,)):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=[
            {"name": s, "duration_min": 45, "is_active": True, "sort_order": 0} for s in offers
        ],
    )


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Calendar:
    """Always has days free; records the slot length it was scanned with."""

    def __init__(self):
        self.tzinfo = _TZ
        self.day_scans: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans.append((start_day, days, slot_minutes))
        base = datetime(2027, 3, 1, tzinfo=_TZ)
        return [base + timedelta(days=n) for n in range(5)]


async def _rebook(
    tenant, conversation, calendar, *, same, cancelled_id, professionals, service=_SERVICE
):
    """Drive the pair exactly as workers/tasks.py does: resolve the candidates
    first (the caller needs them to pick whose calendar to open), then enter."""
    candidates, prefix = ([], None)
    if not same:
        candidates, prefix = fr.rebooking_candidates(
            tenant,
            professionals,
            cancelled_professional_id=cancelled_id,
            service_name=service,
        )
    return await fr.enter_rebooking(
        conversation,
        tenant,
        calendar,
        professionals=professionals,
        cancelled_professional_id=cancelled_id,
        service_name=service,
        same_professional=same,
        candidates=candidates,
        prefix=prefix,
    )


def _bodies(result) -> str:
    return "\n".join(getattr(b, "body", "") for b in result.bubbles)


def _rows(result) -> list:
    return [row for b in result.bubbles for row in getattr(b, "rows", [])]


# ---------------------------------------------------------------------------
# §4.1 — "Outro horário": same doctor, same service
# ---------------------------------------------------------------------------


async def test_same_professional_keeps_the_doctor_and_the_service():
    """The patient chose both a moment ago; asking again would be the flow
    forgetting what it just did."""
    ana = _professional("Dra. Ana")

    result = await _rebook(
        _tenant(), _conversation(), _Calendar(), same=True, cancelled_id=ana.id, professionals=[ana]
    )

    assert result.action == "reply"
    assert result.flow_selected_professional_id == ana.id
    assert result.flow_selected_type == _SERVICE
    assert result.flow_step == fr.BOOKING_DAY_BRANCH.day_step


async def test_the_rebooked_slot_length_comes_from_the_service_not_the_default():
    """The cancelled booking's service is 45 min, the tenant default 30. A scan
    at the wrong length offers days that cannot actually fit it."""
    ana = _professional("Dra. Ana")
    calendar = _Calendar()

    await _rebook(
        _tenant(), _conversation(), calendar, same=True, cancelled_id=ana.id, professionals=[ana]
    )

    assert calendar.day_scans[0][2] == 45


@pytest.mark.parametrize("same", [True, False])
async def test_rebooking_never_delegates_to_the_llm(same):
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=same,
        cancelled_id=ana.id,
        professionals=[ana, bruno],
    )

    assert result.action != "delegate_llm"


# ---------------------------------------------------------------------------
# §4.2 — "Outro médico": everyone but the one who cancelled
# ---------------------------------------------------------------------------


def test_the_doctor_who_cancelled_is_never_offered_back():
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")

    pool, prefix = fr.rebooking_candidates(
        _tenant(), [ana, bruno], cancelled_professional_id=ana.id, service_name=_SERVICE
    )

    assert [p.id for p in pool] == [bruno.id]
    assert prefix is None


def test_only_doctors_who_offer_the_same_service_are_listed():
    ana = _professional("Dra. Ana")
    bruno = _professional("Dr. Bruno", offers=(_SERVICE,))
    carla = _professional("Dra. Carla", offers=("Ortodontia",))

    pool, prefix = fr.rebooking_candidates(
        _tenant(), [ana, bruno, carla], cancelled_professional_id=ana.id, service_name=_SERVICE
    )

    assert [p.id for p in pool] == [bruno.id]
    assert prefix is None


def test_service_matching_is_not_a_raw_string_compare():
    """"limpeza" and "Limpeza" are one service. A raw comparison would silently
    hide half the roster — the failure mode FEAT_35 exists for."""
    ana = _professional("Dra. Ana")
    bruno = _professional("Dr. Bruno", offers=("limpeza",))

    pool, _prefix = fr.rebooking_candidates(
        _tenant(), [ana, bruno], cancelled_professional_id=ana.id, service_name="Limpeza"
    )

    assert [p.id for p in pool] == [bruno.id]


def test_nobody_else_offers_it_falls_back_to_everyone_with_an_explanation():
    """Never a silently short list: an unexplained empty screen reads as the
    clinic being closed."""
    ana = _professional("Dra. Ana")
    carla = _professional("Dra. Carla", offers=("Ortodontia",))

    pool, prefix = fr.rebooking_candidates(
        _tenant(), [ana, carla], cancelled_professional_id=ana.id, service_name=_SERVICE
    )

    assert [p.id for p in pool] == [carla.id]
    assert prefix == fr.REBOOK_SERVICE_UNAVAILABLE_PREFIX


async def test_exactly_one_candidate_skips_the_choice_entirely():
    """A one-row list is ceremony, not a choice."""
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=False,
        cancelled_id=ana.id,
        professionals=[ana, bruno],
    )

    assert result.flow_selected_professional_id == bruno.id
    assert result.flow_selected_type == _SERVICE
    assert result.flow_step == fr.BOOKING_DAY_BRANCH.day_step


async def test_several_candidates_render_the_doctor_list_and_keep_the_service():
    """The doctor is the ONLY thing still to choose — the service must not be
    asked a second time."""
    ana = _professional("Dra. Ana")
    others = [_professional("Dr. Bruno"), _professional("Dra. Carla")]

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=False,
        cancelled_id=ana.id,
        professionals=[ana, *others],
    )

    assert result.flow_selected_type == _SERVICE
    assert result.flow_selected_professional_id is None  # still to be picked
    ids = {r[0] for r in _rows(result)}
    assert ids >= {f"prof|{p.id}" for p in others}
    assert f"prof|{ana.id}" not in ids


async def test_the_fallback_explanation_is_shown_above_the_list():
    ana = _professional("Dra. Ana")
    others = [
        _professional("Dr. Bruno", offers=("Ortodontia",)),
        _professional("Dra. Carla", offers=("Ortodontia",)),
    ]

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=False,
        cancelled_id=ana.id,
        professionals=[ana, *others],
    )

    assert fr.REBOOK_SERVICE_UNAVAILABLE_PREFIX in _bodies(result)


async def test_a_solo_clinic_gets_a_fixed_line_not_an_empty_list():
    ana = _professional("Dra. Ana")

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=False,
        cancelled_id=ana.id,
        professionals=[ana],
    )

    assert result.action == "reply"
    assert fr.REBOOK_NO_OTHER_PROFESSIONAL in _bodies(result)


async def test_the_single_candidate_day_list_offers_a_way_back():
    """§4.2's "Escolher outro Serviço" — the FEAT_32 back_target, not a bespoke
    mechanism."""
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")

    result = await _rebook(
        _tenant(),
        _conversation(),
        _Calendar(),
        same=False,
        cancelled_id=ana.id,
        professionals=[ana, bruno],
    )

    assert any(fr.LABEL_ANOTHER_SERVICE in str(r[1]) for r in _rows(result))


# ---------------------------------------------------------------------------
# §8 — "Cancelar": capture WHY, deterministically
# ---------------------------------------------------------------------------


def test_the_reason_question_fits_the_list_cap_and_remembers_the_appointment():
    appointment_id = uuid4()

    result = fr.enter_decline_reasons(appointment_id)

    assert 0 < len(_rows(result)) <= 10
    assert result.flow_step == fr.STEP_DECLINE_REASON
    assert result.flow_managing_appointment_id == appointment_id


@pytest.mark.parametrize("code,label", list(fr.DECLINE_REASONS))
def test_a_tapped_option_is_stored_as_its_stable_code(code, label):
    """Grouping happens on the code, so rewording a label later must not
    invalidate the history."""
    appointment_id = uuid4()
    conv = _conversation(
        flow_step=fr.STEP_DECLINE_REASON, flow_managing_appointment_id=appointment_id
    )

    result = fr._handle_decline_reason(conv, label)

    assert result.decline_reason == {
        "appointment_id": appointment_id,
        "reason_code": code,
        "reason_text": None,
    }


def test_free_text_is_kept_verbatim_as_its_own_code():
    """The list cannot enumerate every reason; typing is a first-class answer,
    not a failed match."""
    appointment_id = uuid4()
    conv = _conversation(
        flow_step=fr.STEP_DECLINE_REASON, flow_managing_appointment_id=appointment_id
    )

    result = fr._handle_decline_reason(conv, "achei caro demais")

    assert result.decline_reason["reason_code"] == fr.DECLINE_FREE_TEXT_CODE
    assert result.decline_reason["reason_text"] == "achei caro demais"


def test_the_reason_turn_thanks_and_never_delegates():
    """The spec is explicit that the bot must not 'conversar' about the reason."""
    conv = _conversation(flow_step=fr.STEP_DECLINE_REASON, flow_managing_appointment_id=uuid4())

    result = fr._handle_decline_reason(conv, "qualquer coisa")

    assert result.action == "reply"
    assert fr.DECLINE_THANKS in _bodies(result)


def test_an_answer_with_no_appointment_in_state_records_nothing():
    """The appointment id comes from the conversation, never the message — a
    patient cannot address someone else's booking by typing."""
    conv = _conversation(flow_step=fr.STEP_DECLINE_REASON, flow_managing_appointment_id=None)

    result = fr._handle_decline_reason(conv, "Não preciso mais")

    assert result.decline_reason is None
    assert result.action == "reply"  # still answered politely
