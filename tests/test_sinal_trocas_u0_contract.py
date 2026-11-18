"""TASK-085 R / U0 - the frozen surface of the 3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §4).

D-R10/D-R11 (the old consultation leaves the doctor's agenda while the difference Pix is open) and
D-R14 (the clinic changes a price). The parallel slices UA1/UB1 (then UA2/UA3/UH/UB2) code against
these names and signatures; a drift here breaks them before it ships. The few pure helpers (event
ids, the sweep job id, the mail-cap key) are implemented and tested here; every other body is a
stub that raises NotImplementedError until its owner slice lands.
"""

import inspect
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import aliased

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS
from secretaria.models import Appointment, AppointmentStatus, PixDeposit, appointment_reminder as ar
from secretaria.models.deposit_adjustment import (
    ADJUSTMENT_STATUSES,
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    OLD_SLOT_REBOOKED,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    OLD_SLOT_STATES,
    REBOOKABLE_STATUSES,
    RESTORABLE_STATUSES,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    STATUS_RESTORING,
    SUSPENDED_STATUSES,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_REASONS,
    REASON_PRICE_CHANGE_NO_EMAIL,
    REASON_SERVICE_DIFF_REBOOK_DUE,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.schemas.webhook import _ACTION_BUTTON_PREFIXES, decode_action_id
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.payments import (
    adjustment_queries,
    adjustment_suspension,
    old_slot,
    price_change,
)
from secretaria.workers import price_change as price_change_job
from secretaria.workers.shared import price_change_opening
from tests.before_support import make_world
from tests.payments_support import make_sessionmaker

# Google's event-id alphabet (base32hex): a-v and 0-9 only - never w, x, y or z.
BASE32HEX = re.compile(r"^[a-v0-9]{5,1024}$")


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def _kinds(fn) -> dict[str, inspect._ParameterKind]:
    return {name: p.kind for name, p in inspect.signature(fn).parameters.items()}


# --- state (mapa-u §1.2 / §4.2) --------------------------------------------------------------


def test_the_new_adjustment_statuses():
    assert (STATUS_RESTORING, STATUS_OLD_SLOT_LOST, STATUS_REBOOKED) == (
        "restoring",
        "old_slot_lost",
        "rebooked",
    )
    assert {STATUS_RESTORING, STATUS_OLD_SLOT_LOST, STATUS_REBOOKED} <= ADJUSTMENT_STATUSES
    assert all(len(status) <= 24 for status in ADJUSTMENT_STATUSES)  # VARCHAR(24)


def test_the_status_groups_are_the_contract_s():
    assert SUSPENDED_STATUSES == (
        "awaiting_payment",
        "awaiting_choice",
        "restoring",
        "old_slot_lost",
    )
    assert RESTORABLE_STATUSES == ("awaiting_choice", "restoring")
    assert REBOOKABLE_STATUSES == ("awaiting_choice", "old_slot_lost")
    # Every restorable or rebookable change still keeps the consultation out of the agenda.
    assert set(RESTORABLE_STATUSES) | set(REBOOKABLE_STATUSES) <= set(SUSPENDED_STATUSES)
    assert set(SUSPENDED_STATUSES) <= ADJUSTMENT_STATUSES


def test_the_old_slot_states_and_columns():
    assert (
        OLD_SLOT_RELEASING,
        OLD_SLOT_RELEASED,
        OLD_SLOT_RESTORED,
        OLD_SLOT_REBOOKED,
        OLD_SLOT_REPLACED,
    ) == ("releasing", "released", "restored", "rebooked", "replaced")
    assert OLD_SLOT_STATES == frozenset(
        {"releasing", "released", "restored", "rebooked", "replaced"}
    )
    assert all(len(state) <= 16 for state in OLD_SLOT_STATES)  # VARCHAR(16)
    adjustment = DepositAdjustment(old_service="A", new_service="B", kind="increase", status="x")
    assert adjustment.old_slot is None and adjustment.old_slot_at is None


def test_the_two_new_attention_reasons():
    assert REASON_SERVICE_DIFF_REBOOK_DUE == "service_diff_rebook_due"
    assert REASON_PRICE_CHANGE_NO_EMAIL == "price_change_no_email"
    assert {REASON_SERVICE_DIFF_REBOOK_DUE, REASON_PRICE_CHANGE_NO_EMAIL} <= ATTENTION_REASONS
    assert all(len(reason) <= 32 for reason in ATTENTION_REASONS)  # VARCHAR(32)


def test_the_price_change_reminder_kind_is_shown_never_planned():
    assert ar.REMINDER_KIND_PRICE_CHANGE == "price_change"
    assert ar.REMINDER_KIND_PRICE_CHANGE in ar.REMINDER_KINDS
    assert ar.REMINDER_KIND_PRICE_CHANGE in ar.REMINDER_KINDS_UNPLANNED
    assert ar.REMINDER_KIND_PRICE_CHANGE not in ar.REMINDER_KINDS_STAFF
    assert all(len(kind) <= 16 for kind in ar.REMINDER_KINDS)  # VARCHAR(16)


# --- the "Outro horário" button prefix (mapa-u §1.5) -----------------------------------------


def test_the_other_slot_prefix_is_routed():
    assert "svcdiffother|" in _ACTION_BUTTON_PREFIXES
    adjustment_id = uuid4()
    button_id = f"svcdiffother|{adjustment_id}"
    assert decode_action_id(button_id) == ("svcdiffother", str(adjustment_id))
    assert decode_action_id("svcdiffother|not-a-uuid") is None
    assert len(button_id) <= 256
    # The owner's labels (UA2 owns the constants) fit a WhatsApp button.
    assert len("Outro horário") <= MAX_BUTTON_LABEL_CHARS
    assert len("Cancelar consulta") <= MAX_BUTTON_LABEL_CHARS


def test_the_older_choice_prefixes_still_decode_to_themselves():
    adjustment_id = str(uuid4())
    for action in ("svcdiffkeep", "svcdiffresend", "svcdiffboth", "svcdiffyes", "svcdiffno"):
        assert decode_action_id(f"{action}|{adjustment_id}") == (action, adjustment_id)


# --- the suspended consultation does not occupy its slot (mapa-u §1.3) ------------------------


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


CASES = {
    "suspend_awaiting_payment": (MODE_SUSPEND, STATUS_AWAITING_PAYMENT),
    "suspend_awaiting_choice": (MODE_SUSPEND, STATUS_AWAITING_CHOICE),
    "suspend_restoring": (MODE_SUSPEND, STATUS_RESTORING),
    "suspend_old_slot_lost": (MODE_SUSPEND, STATUS_OLD_SLOT_LOST),
    "suspend_rebooked": (MODE_SUSPEND, STATUS_REBOOKED),
    "suspend_kept_old": (MODE_SUSPEND, STATUS_KEPT_OLD),
    "suspend_applied": (MODE_SUSPEND, STATUS_APPLIED),
    "suspend_cancelled": (MODE_SUSPEND, STATUS_CANCELLED),
    "applied_awaiting_payment": (MODE_APPLIED, STATUS_AWAITING_PAYMENT),
    "legacy_awaiting_payment": (None, STATUS_AWAITING_PAYMENT),
    "no_adjustment": (None, None),
}
SUSPENDED_CASES = {
    "suspend_awaiting_payment",
    "suspend_awaiting_choice",
    "suspend_restoring",
    "suspend_old_slot_lost",
}


async def test_a_suspended_consultation_is_hidden_and_does_not_occupy_its_slot(db):
    async with db() as session:
        world = await make_world(session)
    async with db() as session:
        ids = {
            name: (await _appointment_with(session, world, mode=mode, status=status)).id
            for name, (mode, status) in CASES.items()
        }
        await session.commit()

    async with db() as session:
        occupying = set(
            await session.scalars(
                select(Appointment.id).where(adjustment_suspension.occupies_slot_clause())
            )
        )
        suspended = set(
            await session.scalars(
                select(Appointment.id).where(adjustment_suspension.suspended_clause())
            )
        )
        visible = set(
            await session.scalars(
                select(Appointment.id).where(adjustment_suspension.visible_to_patient_clause())
            )
        )
        flags = {
            name: await adjustment_suspension.is_suspended(
                session, tenant_id=world.tenant_id, appointment_id=appointment_id
            )
            for name, appointment_id in ids.items()
        }
        open_ones = {
            name: await adjustment_suspension.open_suspension(
                session, tenant_id=world.tenant_id, appointment_id=appointment_id
            )
            for name, appointment_id in ids.items()
        }

    hidden = {ids[name] for name in SUSPENDED_CASES}
    assert suspended == hidden
    assert occupying == set(ids.values()) - hidden
    assert visible == occupying  # the same rule read from two sides
    assert {name for name, flag in flags.items() if flag} == SUSPENDED_CASES
    assert {name for name, found in open_ones.items() if found is not None} == SUSPENDED_CASES


async def test_occupies_slot_clause_takes_an_alias_and_is_scoped_by_clinic(db):
    async with db() as session:
        world = await make_world(session)
        other = await make_world(session)
    async with db() as session:
        lost = await _appointment_with(
            session, world, mode=MODE_SUSPEND, status=STATUS_OLD_SLOT_LOST
        )
        plain = await _appointment_with(session, world, mode=None, status=None)
        await session.commit()
    alias = aliased(Appointment)
    async with db() as session:
        occupying = set(
            await session.scalars(
                select(alias.id).where(
                    adjustment_suspension.occupies_slot_clause(alias.id, alias.tenant_id)
                )
            )
        )
        # The adjustment belongs to `world`'s clinic: correlated to another clinic it says nothing.
        foreign = set(
            await session.scalars(
                select(alias.id).where(
                    alias.id == lost.id,
                    adjustment_suspension.occupies_slot_clause(alias.id, other.tenant_id),
                )
            )
        )
    assert occupying == {plain.id}
    assert foreign == {lost.id}


def test_occupies_slot_clause_lives_with_the_suspension_rule_in_a_models_only_module():
    """Importers use `adjustment_suspension.occupies_slot_clause` (mapa-u §1.3): `agenda` and
    `release_keep` sit below the payments package, which already imports them."""
    assert "occupies_slot_clause" in adjustment_suspension.__all__
    # The older names keep their re-export through adjustment_queries.
    assert adjustment_queries.suspended_clause is adjustment_suspension.suspended_clause
    source = inspect.getsource(adjustment_suspension)
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "secretaria.services" in line]


# --- old_slot: event ids (implemented) ------------------------------------------------------


class _GoogleCalendar:
    """Anything that is not the secretarIA's own agenda gets a bare Google id."""


def _local_calendar() -> LocalCalendarService:
    return LocalCalendarService(uuid4())


@pytest.mark.parametrize(
    ("builder", "prefix"),
    [
        (lambda a, c: old_slot.restore_event_id(a, calendar=c), "difkeep"),
        (lambda a, c: old_slot.paid_event_id(a, calendar=c), "difpaid"),
        (lambda a, c: old_slot.rebook_event_id(a, calendar=c, source="patient"), "difmove"),
        (lambda a, c: old_slot.rebook_event_id(a, calendar=c, source="hub"), "difhub"),
    ],
)
def test_event_ids_are_deterministic_base32hex_and_loc_on_the_own_agenda(builder, prefix):
    adjustment_id = uuid4()
    google = builder(adjustment_id, _GoogleCalendar())
    assert google == f"{prefix}{adjustment_id.hex}"
    assert builder(adjustment_id, _GoogleCalendar()) == google  # deterministic
    assert builder(uuid4(), _GoogleCalendar()) != google
    assert BASE32HEX.match(google)
    assert builder(adjustment_id, None) == google  # no calendar object = a Google id

    local = builder(adjustment_id, _local_calendar())
    assert local == f"loc_{prefix}{adjustment_id.hex}"
    assert BASE32HEX.match(local.removeprefix("loc_"))
    assert len(local) <= 64  # calendar_events.id is VARCHAR(64)


def test_every_path_has_its_own_prefix():
    """A re-sent id is a 409 that `create_event(event_id=)` reads as success: the restore, the
    paid change, the patient's and the hub's rebook must never share an id."""
    adjustment_id = uuid4()
    cal = _GoogleCalendar()
    ids = {
        old_slot.restore_event_id(adjustment_id, calendar=cal),
        old_slot.paid_event_id(adjustment_id, calendar=cal),
        old_slot.rebook_event_id(adjustment_id, calendar=cal, source="patient"),
        old_slot.rebook_event_id(adjustment_id, calendar=cal, source="hub"),
    }
    assert len(ids) == 4
    from secretaria.services import release_keep

    assert release_keep.keep_event_id(adjustment_id) not in ids


def test_rebook_event_id_refuses_an_unknown_source():
    with pytest.raises(ValueError):
        old_slot.rebook_event_id(uuid4(), calendar=None, source="clinic")


def test_old_slot_constants_and_literals():
    assert old_slot.RETRY_AFTER == timedelta(minutes=2)
    assert set(get_args(old_slot.RestoreOutcome)) == {
        "restored",
        "slot_taken",
        "calendar_unavailable",
        "already_decided",
        "not_applicable",
    }
    assert set(get_args(old_slot.RestoreTrigger)) == {"tap", "silence", "no_room", "not_applied"}


# --- old_slot: the frozen signatures of what UA1 implements (mapa-u §4.3) ---------------------

OLD_SLOT_SIGNATURES = [
    ("restore_event_id", ["adjustment_id", "calendar"], False),
    ("paid_event_id", ["adjustment_id", "calendar"], False),
    ("rebook_event_id", ["adjustment_id", "calendar", "source"], False),
    ("vacate_old_slot", ["tenant_id", "adjustment_id"], True),
    ("retry_vacates", ["session", "now", "limit"], True),
    ("old_slot_free", ["session", "tenant", "appointment", "adjustment"], True),
    ("restore_old_slot", ["session", "tenant_id", "adjustment_id", "now", "trigger"], True),
    ("mark_old_slot_lost", ["session", "tenant", "adjustment", "now"], True),
    ("to_restoring", ["session", "adjustment", "from_status", "now"], True),
    ("rebookable", ["session", "tenant_id", "appointment_id"], True),
    (
        "finish_rebook",
        [
            "session",
            "tenant_id",
            "adjustment_id",
            "appointment",
            "event_id",
            "event_link",
            "now",
            "source",
        ],
        True,
    ),
    (
        "place_new_event_after_payment",
        ["session", "tenant", "appointment", "adjustment", "patient_name"],
        True,
    ),
    ("discard_event", ["calendar", "event_id"], True),
]


@pytest.mark.parametrize(("name", "params", "is_async"), OLD_SLOT_SIGNATURES)
def test_old_slot_signatures_are_frozen(name, params, is_async):
    fn = getattr(old_slot, name)
    assert _params(fn) == params
    assert inspect.iscoroutinefunction(fn) is is_async


def test_old_slot_keyword_only_parameters():
    kinds = _kinds(old_slot.restore_old_slot)
    assert kinds["session"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        kinds[name] is inspect.Parameter.KEYWORD_ONLY
        for name in ("tenant_id", "adjustment_id", "now", "trigger")
    )
    assert inspect.signature(old_slot.retry_vacates).parameters["limit"].default == 50
    for name in ("vacate_old_slot", "discard_event"):
        kinds = _kinds(getattr(old_slot, name)).values()
        assert all(kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for kind in kinds)


async def test_the_old_slot_stubs_raise_until_ua1_lands():
    with pytest.raises(NotImplementedError):
        await old_slot.vacate_old_slot(uuid4(), uuid4())
    with pytest.raises(NotImplementedError):
        await old_slot.restore_old_slot(
            None, tenant_id=uuid4(), adjustment_id=uuid4(), now=datetime.now(UTC), trigger="tap"
        )


# --- price change (mapa-u §2 / §4.5) ----------------------------------------------------------


def test_price_change_constants():
    assert price_change.PRICE_NOTICE_MIN_LEAD == timedelta(hours=3)
    assert price_change.PRICE_MAIL_DAILY_CAP == 3
    assert price_change.EMAIL_TEMPLATE == "price_change_patient"
    assert price_change.SWEEP_JOB == "sweep_price_changes"
    assert price_change.SWEEP_DEFER == timedelta(seconds=60)
    assert price_change.SWEEP_BUCKET == timedelta(minutes=5)
    assert price_change.SWEEP_JOB == price_change_job.sweep_price_changes.__name__


def test_the_sweep_job_id_is_one_per_clinic_and_five_minute_window():
    tenant_id = UUID("11111111-2222-3333-4444-555555555555")
    other = uuid4()
    t0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    bucket = int(t0.timestamp()) // 300
    assert price_change.sweep_job_id(tenant_id, t0) == f"pricechg:{tenant_id}:{bucket}"
    # Several edits in the same window are ONE job; the next window is another.
    assert price_change.sweep_job_id(tenant_id, t0 + timedelta(minutes=4, seconds=59)) == (
        price_change.sweep_job_id(tenant_id, t0)
    )
    assert price_change.sweep_job_id(tenant_id, t0 + timedelta(minutes=5)) != (
        price_change.sweep_job_id(tenant_id, t0)
    )
    assert price_change.sweep_job_id(other, t0) != price_change.sweep_job_id(tenant_id, t0)
    # The same instant in another timezone is the same window.
    from zoneinfo import ZoneInfo

    local = t0.astimezone(ZoneInfo("America/Sao_Paulo"))
    assert price_change.sweep_job_id(tenant_id, local) == price_change.sweep_job_id(tenant_id, t0)


def test_the_sweep_job_id_refuses_a_naive_instant():
    with pytest.raises(ValueError):
        price_change.sweep_job_id(uuid4(), datetime(2026, 11, 20, 12, 0))


def test_the_mail_cap_key_never_carries_the_address():
    tenant_id = uuid4()
    key = price_change.mail_cap_key(tenant_id, "  Maria@Example.com ", "20261120")
    assert key.startswith(f"price_change_mail:{tenant_id}:") and key.endswith(":20261120")
    digest = key.split(":")[2]
    assert re.fullmatch(r"[0-9a-f]{16}", digest)
    assert "maria" not in key.lower() and "example" not in key.lower()
    # Normalized, so the cap counts one person once; salted per clinic.
    assert key == price_change.mail_cap_key(tenant_id, "maria@example.com", "20261120")
    assert key != price_change.mail_cap_key(uuid4(), "maria@example.com", "20261120")


def test_the_price_opening_shape():
    assert set(get_args(price_change.PriceOpeningKind)) == {
        "pix",
        "refund_question",
        "abated",
        "requoted",
        "unchanged",
        "pending",
        "too_close",
        "retry",
        "not_found",
    }
    opening = price_change.PriceOpening(kind="not_found")
    assert (
        opening.appointment_id,
        opening.adjustment_id,
        opening.diff_cents,
        opening.pix_copy_paste,
        opening.expires_at,
        opening.old_amount_cents,
    ) == (None, None, None, None, None, None)
    with pytest.raises(AttributeError):  # frozen
        opening.kind = "pix"  # type: ignore[misc]


PRICE_SIGNATURES = [
    (price_change, "enqueue_sweep", ["arq_pool", "tenant_id", "changed_at"], True),
    (price_change, "sweep_price_changes", ["tenant_id", "changed_at", "now", "redis"], True),
    (
        price_change,
        "price_notice_needed",
        ["session", "tenant", "appointment", "deposit", "now"],
        True,
    ),
    (
        price_change,
        "open_price_notice",
        ["session", "tenant", "reminder_id", "patient_id", "now"],
        True,
    ),
    (price_change, "price_opening_messages", ["opening", "tenant"], False),
    (price_change, "sweep_job_id", ["tenant_id", "changed_at"], False),
    (price_change, "mail_cap_key", ["tenant_id", "address", "local_day"], False),
    (price_change_job, "sweep_price_changes", ["ctx", "tenant_id", "changed_at"], True),
    (
        price_change_opening,
        "send_price_change_opening",
        ["reply", "tenant", "reminder_id", "redis"],
        True,
    ),
]


@pytest.mark.parametrize(("module", "name", "params", "is_async"), PRICE_SIGNATURES)
def test_price_change_signatures_are_frozen(module, name, params, is_async):
    fn = getattr(module, name)
    assert _params(fn) == params
    assert inspect.iscoroutinefunction(fn) is is_async


def test_price_change_keyword_only_parameters():
    assert _kinds(price_change.enqueue_sweep)["changed_at"] is inspect.Parameter.KEYWORD_ONLY
    for name in ("changed_at", "now", "redis"):
        assert _kinds(price_change.sweep_price_changes)[name] is inspect.Parameter.KEYWORD_ONLY
    opening = _kinds(price_change_opening.send_price_change_opening)
    assert opening["reminder_id"] is inspect.Parameter.KEYWORD_ONLY
    assert (
        inspect.signature(price_change_opening.send_price_change_opening)
        .parameters["redis"]
        .default
        is None
    )


async def test_the_price_change_stubs_raise_until_ub1_and_ub2_land():
    now = datetime.now(UTC)
    with pytest.raises(NotImplementedError):
        await price_change.sweep_price_changes(uuid4(), changed_at=now, now=now, redis=None)
    with pytest.raises(NotImplementedError):
        await price_change_job.sweep_price_changes({}, str(uuid4()), now.isoformat())
    with pytest.raises(NotImplementedError):
        await price_change_opening.send_price_change_opening(None, None, reminder_id=uuid4())


# --- the new modules import alone (a cycle only shows for some import orders) -----------------


@pytest.mark.parametrize(
    "module",
    [
        "secretaria.services.payments.old_slot",
        "secretaria.services.payments.price_change",
        "secretaria.workers.price_change",
        "secretaria.workers.shared.price_change_opening",
        "secretaria.services.payments.adjustment_suspension",
    ],
)
def test_each_new_module_imports_alone(module):
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-800:]
