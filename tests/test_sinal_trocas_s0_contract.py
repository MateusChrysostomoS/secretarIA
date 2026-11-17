"""TASK-085 R S0 - the frozen surface later slices import (tasks/TASK-085/sinal-trocas/contrato.md).

The state constants, the three choice buttons (D-R7) and their routing prefixes, the choice
deadline (D-R8), the result kinds, the "suspended consultation" clause (S5 hides it from the
patient) and the stubs of the functions S1..S3b implement: their names and parameters are what
the parallel slices code against, so a drift here breaks them before it ships.
"""

import inspect
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS
from secretaria.models import Appointment, AppointmentStatus, PixDeposit
from secretaria.models.deposit_adjustment import (
    ADJUSTMENT_MODES,
    ADJUSTMENT_STATUSES,
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_EXPIRED,
    STATUS_KEPT_OLD,
    SUSPENDED_STATUSES,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_REASONS,
    REASON_SERVICE_DIFF_UNPAID,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.schemas.webhook import _ACTION_BUTTON_PREFIXES, decode_action_id
from secretaria.services.payments import adjustment_queries, adjustment_suspension, service_change
from secretaria.services.payments.service_change import (
    ACTION_CHOICE_BOTH,
    ACTION_CHOICE_KEEP,
    ACTION_CHOICE_RESEND,
    CHOICE_ACTIONS,
    CHOICE_BOTH,
    CHOICE_KEEP,
    CHOICE_MIN_LEAD,
    CHOICE_OF_ACTION,
    CHOICE_RESEND,
    CHOICE_TTL,
    DIFFERENCE_HOLD_GRACE,
    LABEL_CHOICE_BOTH,
    LABEL_CHOICE_KEEP,
    LABEL_CHOICE_RESEND,
    MIN_SUSPEND_PIX,
    SWEEP_MARGIN,
    ChoiceOutcome,
    ResultKind,
    ServiceChangeResult,
    choice_buttons,
    choice_deadline,
    difference_hold_expiry,
    resend_fits,
    suspend_cutoff,
    suspend_pix_expiry,
)
from tests.before_support import make_world
from tests.payments_support import make_sessionmaker

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


# --- state -------------------------------------------------------------------------------


def test_the_new_modes_and_statuses():
    assert (MODE_SUSPEND, MODE_APPLIED) == ("suspend", "applied")
    assert ADJUSTMENT_MODES == frozenset({MODE_SUSPEND, MODE_APPLIED})
    assert (STATUS_AWAITING_CHOICE, STATUS_KEPT_OLD) == ("awaiting_choice", "kept_old")
    assert {STATUS_AWAITING_CHOICE, STATUS_KEPT_OLD} <= ADJUSTMENT_STATUSES
    assert all(len(status) <= 24 for status in ADJUSTMENT_STATUSES)  # VARCHAR(24)
    assert SUSPENDED_STATUSES == (STATUS_AWAITING_PAYMENT, STATUS_AWAITING_CHOICE)


def test_the_unpaid_difference_reason():
    assert REASON_SERVICE_DIFF_UNPAID == "service_diff_unpaid"
    assert REASON_SERVICE_DIFF_UNPAID in ATTENTION_REASONS
    assert len(REASON_SERVICE_DIFF_UNPAID) <= 32  # VARCHAR(32)


def test_the_result_kinds():
    assert set(get_args(ResultKind)) == {
        "applied_now",
        "awaiting_payment",
        "applied_awaiting_payment",
        "suspended",
        "asked",
        "blocked",
    }
    assert ServiceChangeResult(kind="suspended").kind == "suspended"


# --- buttons (D-R7) -----------------------------------------------------------------------


def test_the_three_labels_are_the_owner_s_and_fit_whatsapp():
    assert (LABEL_CHOICE_KEEP, LABEL_CHOICE_RESEND, LABEL_CHOICE_BOTH) == (
        "Manter a antiga",
        "Reenviar o Pix",
        "Cancelar ambas",
    )
    assert all(
        len(label) <= MAX_BUTTON_LABEL_CHARS
        for label in (LABEL_CHOICE_KEEP, LABEL_CHOICE_RESEND, LABEL_CHOICE_BOTH)
    )


def test_each_button_is_an_action_routed_on_both_channels():
    adjustment_id = uuid4()
    buttons = choice_buttons(adjustment_id)
    assert buttons == [
        (f"svcdiffkeep|{adjustment_id}", "Manter a antiga"),
        (f"svcdiffresend|{adjustment_id}", "Reenviar o Pix"),
        (f"svcdiffboth|{adjustment_id}", "Cancelar ambas"),
    ]
    for button_id, _label in buttons:
        assert len(button_id) <= 256
        action, target = decode_action_id(button_id)
        assert action in CHOICE_ACTIONS and target == str(adjustment_id)
    assert {f"{action}|" for action in CHOICE_ACTIONS} <= set(_ACTION_BUTTON_PREFIXES)
    assert (ACTION_CHOICE_KEEP, ACTION_CHOICE_RESEND, ACTION_CHOICE_BOTH) == (
        "svcdiffkeep",
        "svcdiffresend",
        "svcdiffboth",
    )


def test_without_a_window_the_resend_button_is_left_out():
    adjustment_id = uuid4()
    assert [label for _id, label in choice_buttons(adjustment_id, resend=False)] == [
        "Manter a antiga",
        "Cancelar ambas",
    ]


def test_a_tampered_choice_id_is_not_an_action():
    assert decode_action_id("svcdiffkeep|not-a-uuid") is None


def test_each_action_maps_to_one_choice():
    assert (CHOICE_KEEP, CHOICE_RESEND, CHOICE_BOTH) == ("keep", "resend", "both")
    assert CHOICE_OF_ACTION == {
        ACTION_CHOICE_KEEP: CHOICE_KEEP,
        ACTION_CHOICE_RESEND: CHOICE_RESEND,
        ACTION_CHOICE_BOTH: CHOICE_BOTH,
    }


# --- the choice deadline (D-R8) --------------------------------------------------------------


def test_the_constants_of_the_window():
    assert CHOICE_TTL == timedelta(hours=24) and CHOICE_MIN_LEAD == timedelta(hours=1)
    assert SWEEP_MARGIN == timedelta(minutes=5) and MIN_SUSPEND_PIX == timedelta(minutes=30)
    assert DIFFERENCE_HOLD_GRACE == timedelta(minutes=15)


FAR = NOW + timedelta(days=5)


def test_the_cutoff_is_before_the_earlier_of_the_two_visits():
    """Fix round 1: the NEW visit counts too (moving a visit earlier must not let the choice
    run past it)."""
    old, new = NOW + timedelta(days=2), NOW + timedelta(hours=10)
    expected = new - CHOICE_MIN_LEAD - SWEEP_MARGIN
    assert suspend_cutoff(old, new) == expected == suspend_cutoff(new, old)


def test_the_choice_waits_24_hours_when_both_visits_are_far():
    assert choice_deadline(NOW, FAR, FAR + timedelta(days=1)) == NOW + timedelta(hours=24)


def test_the_choice_never_runs_past_the_cutoff_of_either_visit():
    near = NOW + timedelta(hours=10)
    cutoff = near - timedelta(hours=1, minutes=5)
    assert choice_deadline(NOW, near, FAR) == cutoff
    assert choice_deadline(NOW, FAR, near) == cutoff


@pytest.mark.parametrize("minutes_ahead", [65, 60, 30, -60])
def test_no_room_for_a_choice_is_none(minutes_ahead):
    near = NOW + timedelta(minutes=minutes_ahead)
    assert choice_deadline(NOW, near, FAR) is None
    assert choice_deadline(NOW, FAR, near) is None


def test_the_suspended_pix_takes_the_clinic_ttl_when_there_is_room():
    assert suspend_pix_expiry(NOW, 30, FAR, FAR) == NOW + timedelta(minutes=30)


def test_the_suspended_pix_is_capped_at_the_cutoff():
    near = NOW + timedelta(hours=2)  # cutoff = NOW + 55 min
    assert suspend_pix_expiry(NOW, 120, near, FAR) == NOW + timedelta(minutes=55)


def test_without_a_minimally_useful_pix_the_change_is_not_suspended():
    """Less than min(ttl, 30 min) before the cutoff -> None -> the legacy deferred mode."""
    near = NOW + timedelta(hours=1, minutes=34)  # cutoff = NOW + 29 min
    assert suspend_pix_expiry(NOW, 30, near, FAR) is None
    assert suspend_pix_expiry(NOW, 30, FAR, near) is None
    # A clinic TTL shorter than the floor is its own floor.
    assert suspend_pix_expiry(NOW, 10, near, FAR) == NOW + timedelta(minutes=10)


def test_resend_needs_an_hour_before_the_new_visit_and_a_useful_pix():
    assert resend_fits(NOW, 30, FAR, FAR)
    assert not resend_fits(NOW, 30, FAR, NOW + timedelta(minutes=59))
    assert not resend_fits(NOW, 30, NOW + timedelta(hours=1, minutes=34), FAR)


def test_the_hold_never_outlives_the_new_visit():
    deadline = NOW + timedelta(hours=3)
    assert difference_hold_expiry(deadline, FAR) == deadline + DIFFERENCE_HOLD_GRACE
    new_start = deadline + timedelta(minutes=5)
    assert difference_hold_expiry(deadline, new_start) == new_start


# --- the frozen signatures of what S1..S3b implement ----------------------------------------


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_request_paid_change_is_the_engine_entry_point():
    assert service_change.request_paid_change is service_change.request_service_change


def test_choice_outcome_shape():
    outcome = ChoiceOutcome(kind="kept_old")
    assert outcome.adjustment_id is None and outcome.appointment_id is None
    assert outcome.pix_copy_paste is None and outcome.diff_cents == 0


@pytest.mark.parametrize(
    ("module", "name", "params"),
    [
        (service_change, "on_difference_unpaid", ["session", "tenant", "adjustment", "now"]),
        (
            service_change,
            "choose_after_expiry",
            ["session", "tenant_id", "adjustment_id", "choice", "now"],
        ),
        (service_change, "followup_kept_old", ["tenant_id", "appointment_id"]),
        (service_change, "choice_message_text", ["old_service", "new_service", "diff_cents"]),
        (service_change, "choice_answer_text", ["outcome"]),
    ],
)
async def test_the_s1_stubs_are_frozen(module, name, params):
    fn = getattr(module, name)
    assert _params(fn) == params
    if inspect.iscoroutinefunction(fn):
        with pytest.raises(NotImplementedError, match="TASK-085 R S1"):
            await fn(**dict.fromkeys(params))
    else:
        with pytest.raises(NotImplementedError, match="TASK-085 R S1"):
            fn(**dict.fromkeys(params))


async def test_the_s2_stubs_are_frozen():
    from secretaria.services.payments import deposit_lifecycle

    requote = deposit_lifecycle.requote_awaiting
    assert _params(requote) == ["session", "tenant", "appointment", "deposit", "now"]
    recharge = deposit_lifecycle.recharge_after_edit
    assert _params(recharge) == [
        "tenant_id", "appointment_id", "old_amount_cents", "waba_token", "redis",
    ]
    with pytest.raises(NotImplementedError, match="TASK-085 R S2"):
        await requote(None, tenant=None, appointment=None, deposit=None, now=None)
    with pytest.raises(NotImplementedError, match="TASK-085 R S2"):
        await recharge(tenant_id=None, appointment_id=None, old_amount_cents=None)


async def test_the_s3b_stub_is_frozen():
    from secretaria.workers.shared import service_change_actions

    fn = service_change_actions.handle_service_diff_choice
    assert _params(fn) == ["reply", "action", "adjustment_id", "redis"]
    with pytest.raises(NotImplementedError, match="TASK-085 R S3b"):
        await fn(None, "svcdiffkeep", uuid4())


# --- the suspended consultation (S5 hides it; S4 refuses edits on it) -------------------------


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _appointment_with(session, world, *, mode, status) -> Appointment:
    start = datetime.now(UTC) + timedelta(days=4)
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        phone=world.patient.wa_id,
        appointment_type="Consulta",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    await session.flush()
    if status is None:
        return appointment
    deposit = PixDeposit(
        tenant_id=world.tenant_id,
        appointment_id=appointment.id,
        patient_id=world.patient.id,
        amount_cents=6000,
        percent_applied=30,
        status=PixDepositStatus.PAID,
        provider="asaas",
        provider_charge_id=f"pay_{uuid4().hex[:8]}",
        deposit_timing="before",
    )
    session.add(deposit)
    await session.flush()
    session.add(
        DepositAdjustment(
            id=uuid4(),
            tenant_id=world.tenant_id,
            deposit_id=deposit.id,
            appointment_id=appointment.id,
            kind=KIND_INCREASE,
            mode=mode,
            status=status,
            old_service="Consulta",
            new_service="Avaliação",
            diff_cents=3000,
        )
    )
    await session.flush()
    return appointment


async def test_only_a_suspend_change_still_open_suspends_the_consultation(db):
    async with db() as session:
        world = await make_world(session)
    async with db() as session:
        cases = {
            "suspend_awaiting_payment": (MODE_SUSPEND, STATUS_AWAITING_PAYMENT),
            "suspend_awaiting_choice": (MODE_SUSPEND, STATUS_AWAITING_CHOICE),
            "suspend_applied": (MODE_SUSPEND, STATUS_APPLIED),
            "suspend_kept_old": (MODE_SUSPEND, STATUS_KEPT_OLD),
            "suspend_expired": (MODE_SUSPEND, STATUS_EXPIRED),
            "applied_awaiting_payment": (MODE_APPLIED, STATUS_AWAITING_PAYMENT),
            "legacy_awaiting_payment": (None, STATUS_AWAITING_PAYMENT),
            "no_adjustment": (None, None),
        }
        ids = {}
        for name, (mode, status) in cases.items():
            ids[name] = (await _appointment_with(session, world, mode=mode, status=status)).id
        await session.commit()

    async with db() as session:
        suspended = set(
            await session.scalars(
                select(Appointment.id).where(adjustment_queries.suspended_clause())
            )
        )
        visible = set(
            await session.scalars(
                select(Appointment.id).where(adjustment_queries.visible_to_patient_clause())
            )
        )
        flags = {
            name: await adjustment_queries.is_suspended(
                session, tenant_id=world.tenant_id, appointment_id=appointment_id
            )
            for name, appointment_id in ids.items()
        }

    expected = {ids["suspend_awaiting_payment"], ids["suspend_awaiting_choice"]}
    assert suspended == expected
    assert visible == set(ids.values()) - expected
    assert {name for name, flag in flags.items() if flag} == {
        "suspend_awaiting_payment",
        "suspend_awaiting_choice",
    }


async def test_the_clause_is_scoped_by_clinic(db):
    async with db() as session:
        world = await make_world(session)
        other = await make_world(session)
    async with db() as session:
        appointment = await _appointment_with(
            session, world, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        await session.commit()
    async with db() as session:
        assert await adjustment_queries.is_suspended(
            session, tenant_id=world.tenant_id, appointment_id=appointment.id
        )
        assert not await adjustment_queries.is_suspended(
            session, tenant_id=other.tenant_id, appointment_id=appointment.id
        )


async def test_the_clause_works_on_an_aliased_appointments_table(db):
    from sqlalchemy.orm import aliased

    async with db() as session:
        world = await make_world(session)
    async with db() as session:
        suspended = await _appointment_with(
            session, world, mode=MODE_SUSPEND, status=STATUS_AWAITING_CHOICE
        )
        plain = await _appointment_with(session, world, mode=None, status=None)
        await session.commit()
    other = aliased(Appointment)
    async with db() as session:
        hidden = set(
            await session.scalars(
                select(other.id).where(
                    adjustment_queries.suspended_clause(other.id, other.tenant_id)
                )
            )
        )
        shown = set(
            await session.scalars(
                select(other.id).where(
                    adjustment_queries.visible_to_patient_clause(other.id, other.tenant_id)
                )
            )
        )
    assert hidden == {suspended.id} and shown == {plain.id}


async def test_open_suspension_names_the_change_and_the_patient_text(db):
    async with db() as session:
        world = await make_world(session)
    async with db() as session:
        suspended = await _appointment_with(
            session, world, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        plain = await _appointment_with(
            session, world, mode=MODE_APPLIED, status=STATUS_AWAITING_PAYMENT
        )
        await session.commit()
    async with db() as session:
        found = await adjustment_queries.open_suspension(
            session, tenant_id=world.tenant_id, appointment_id=suspended.id
        )
        none = await adjustment_queries.open_suspension(
            session, tenant_id=world.tenant_id, appointment_id=plain.id
        )
    assert found is not None and found.mode == MODE_SUSPEND and none is None
    assert "aguardando o Pix da diferença" in adjustment_queries.SUSPENDED_VISIT_TEXT


def test_the_clause_lives_in_a_module_that_imports_only_models():
    """S5 adds the clause to `patient_context` and the reminder queries, which the payments
    package itself imports: the clause's own module must never import back into services."""
    source = inspect.getsource(adjustment_suspension)
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "secretaria.services" in line]
    assert adjustment_queries.suspended_clause is adjustment_suspension.suspended_clause
