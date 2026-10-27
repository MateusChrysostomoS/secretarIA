"""The clinic's Editar/Remarcar service (TASK-032 R7, spec 2026-10-09 §1/§3/§4)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    ProfessionalEditNotice,
    Tenant,
)
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.staff_appointment_edit import StaffEditError, StaffEditRequest
from tests._r7_support import FakeCalendar, add_professional, set_appointment, set_tenant, soon
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world

SERVICES = [
    {"name": "Consulta", "is_active": True, "duration_min": 30},
    {"name": "Retorno", "is_active": True, "duration_min": 40},
]


@pytest.fixture(autouse=True)
def _calendars():
    FakeCalendar.reset()


async def calendar_for(professional_id):
    return FakeCalendar.get(str(professional_id) if professional_id else "tenant")


def _req(**values) -> StaffEditRequest:
    allow = values.pop("allow_overlap", False)
    return StaffEditRequest(fields=frozenset(values), allow_overlap=allow, **values)


async def _world(db, **kwargs):
    kwargs.setdefault("start_at", soon())
    kwargs.setdefault("google_event_id", "evt-1")
    world = await seed_world(db, **kwargs)
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


async def _edit(db, world, request):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        return await sae.apply_staff_edit(
            session, tenant, appointment, request, calendar_for=calendar_for
        )


async def _notices(db, appointment_id) -> list[ProfessionalEditNotice]:
    async with db() as session:
        return list(
            await session.scalars(
                select(ProfessionalEditNotice).where(
                    ProfessionalEditNotice.appointment_id == appointment_id
                )
            )
        )


def _labels(outcome) -> list[str]:
    return [change.label for change in outcome.changes]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def test_a_new_time_moves_the_event_and_the_row_keeping_the_duration(db):
    world = await _world(db)
    new = world.start_at + timedelta(days=1, hours=1)

    outcome = await _edit(db, world, _req(start_at=new))

    assert _labels(outcome) == ["Data", "Horário"] and outcome.time_changed is True
    [(event_id, start, end, summary, description)] = FakeCalendar.get("tenant").updated
    assert (event_id, start, end, summary) == (
        "evt-1",
        new,
        new + timedelta(minutes=30),
        "Consulta - Maria",
    )
    assert "Convênio: não informado" in description
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == new and row.status == AppointmentStatus.RESCHEDULED
    [notice] = await _notices(db, world.appointment.id)
    assert notice.changed_fields == ["data", "horário"]
    assert outcome.notice_id == str(notice.id)


async def test_a_new_service_takes_its_duration_and_mails_no_doctor(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(service="retorno"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Serviço", "Consulta", "Retorno")
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert row.end_at - row.start_at == timedelta(minutes=40)
    assert outcome.notice_id is None and await _notices(db, world.appointment.id) == []


async def test_a_doctor_on_another_agenda_is_created_there_and_the_old_event_deleted(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(professional_id=new_pro))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Médico", "Dra. Ana", "Dr. Bruno")
    ]
    assert len(FakeCalendar.get(str(new_pro)).created) == 1
    assert FakeCalendar.get(str(old_pro)).cancelled == ["evt-1"]
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == new_pro and row.google_event_id == f"evt-new-{new_pro}"
    assert [n.changed_fields for n in await _notices(db, world.appointment.id)] == [["médico"]]


async def test_a_doctor_on_the_same_agenda_keeps_the_event(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")
    FakeCalendar.instances[str(old_pro)] = FakeCalendar("shared")
    FakeCalendar.instances[str(new_pro)] = FakeCalendar("shared")

    await _edit(db, world, _req(professional_id=new_pro))

    assert len(FakeCalendar.get(str(old_pro)).updated) == 1
    assert FakeCalendar.get(str(new_pro)).created == []
    assert FakeCalendar.get(str(old_pro)).cancelled == []
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-1"


async def test_a_taken_slot_is_409_unless_the_clinic_squeezes_it_in(db):
    world = await _world(db)
    FakeCalendar.get("tenant").free = False
    new = world.start_at + timedelta(hours=2)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=new))
    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert FakeCalendar.get("tenant").updated == []
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at

    await _edit(db, world, _req(start_at=new, allow_overlap=True))
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == new


async def test_google_refusing_is_502_and_nothing_changes(db):
    world = await _world(db)
    FakeCalendar.get("tenant").fail = CalendarUnavailableError("down")

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (502, "calendar_unavailable")
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at and row.status == AppointmentStatus.SCHEDULED


async def test_a_database_failure_after_google_puts_google_back(db, monkeypatch):
    world = await _world(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(sae, "write_appointment_edit", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    moved, restored = FakeCalendar.get("tenant").updated
    assert restored[1] == world.start_at and restored[3] == "Consulta - Maria"
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at


async def test_a_row_cancelled_while_google_was_moving_is_409_and_google_is_put_back(db):
    world = await _world(db)

    async def _someone_cancels() -> None:
        await set_appointment(db, world.appointment.id, status=AppointmentStatus.CANCELLED)

    FakeCalendar.get("tenant").during = _someone_cancels

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (409, "not_live")
    assert len(FakeCalendar.get("tenant").updated) == 2  # moved, then restored


@pytest.mark.parametrize(
    ("request_values", "code"),
    [
        ({"insurance": None}, "nothing_changed"),
        ({"service": "Cirurgia"}, "service_not_offered"),
        ({"phone": "1234"}, "invalid_phone"),
    ],
)
async def test_validation_errors_are_422(db, request_values, code):
    world = await _world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(**request_values))

    assert (err.value.status_code, err.value.code) == (422, code)
    assert FakeCalendar.instances == {}  # Google never touched


async def test_the_same_time_is_nothing_changed_and_a_past_time_is_refused(db):
    world = await _world(db)
    with pytest.raises(StaffEditError) as same:
        await _edit(db, world, _req(start_at=world.start_at))
    assert same.value.code == "nothing_changed"

    with pytest.raises(StaffEditError) as past:
        await _edit(db, world, _req(start_at=datetime.now(UTC) - timedelta(hours=1)))
    assert (past.value.status_code, past.value.code) == (422, "start_in_past")


async def test_a_professional_of_another_clinic_or_inactive_is_unknown(db):
    world = await _world(db)
    other = await seed_world(db, phone_number_id="pnid-2", professional_name="Dr. Fora")
    retired = await add_professional(db, world.tenant.id, "Dr. Antigo", active=False)

    for professional_id in (other.appointment.professional_id, retired):
        with pytest.raises(StaffEditError) as err:
            await _edit(db, world, _req(professional_id=professional_id))
        assert (err.value.status_code, err.value.code) == (422, "unknown_professional")


async def test_only_the_contact_phone_changes_nothing_else(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(phone="+55 (11) 97777-6666"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Telefone de contato", "não informado", "5511977776666")
    ]
    assert FakeCalendar.instances == {}  # the event carries no phone: Google untouched
    assert outcome.notice_id is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.phone == "5511977776666"
    async with db() as session:
        assert (await session.get(Patient, world.patient.id)).wa_id == WA_ID


async def test_convenio_and_who_it_is_for_change_and_can_be_cleared(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(insurance="Unimed", attendee_name="João"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Convênio", "não informado", "Unimed"),
        ("Paciente", "você", "João"),
    ]
    assert FakeCalendar.get("tenant").updated[0][3] == "Consulta - João"

    cleared = await _edit(db, world, _req(insurance="", attendee_name=None))
    assert [(c.label, c.after) for c in cleared.changes] == [
        ("Convênio", "não informado"),
        ("Paciente", "você"),
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance is None and row.attendee_name is None


async def test_a_closed_appointment_or_a_block_cannot_be_edited(db):
    closed = await _world(db, status=AppointmentStatus.CANCELLED)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, closed, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (409, "not_live")

    block = await _world(db, phone_number_id="pnid-3")
    await set_appointment(db, block.appointment.id, patient_id=None, phone=None)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, block, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (422, "not_a_patient_appointment")


def test_phone_normalisation():
    assert sae.normalize_phone("+55 (11) 98888-7777") == "5511988887777"
    assert sae.normalize_phone("  ") is None and sae.normalize_phone(None) is None
    for bad in ("11988887777", "1" * 16, "abc"):
        with pytest.raises(ValueError):
            sae.normalize_phone(bad)


@pytest.mark.parametrize(
    "changed",
    [
        {"insurance": "Unimed"},
        {"attendee_name": "João"},
        {"phone": "5511999990000"},
        {"appointment_type": "Retorno"},
    ],
)
async def test_a_concurrent_field_edit_is_not_overwritten(db, changed):
    world = await _world(db)

    async def concurrent():
        await set_appointment(db, world.appointment.id, **changed)

    FakeCalendar.get("tenant").during = concurrent
    with pytest.raises(StaffEditError) as error:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))
    assert error.value.code == "appointment_changed"
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at
    for key, value in changed.items():
        assert getattr(row, key) == value
    restored = FakeCalendar.get("tenant").updated[-1]
    assert restored[1] == world.start_at
    if "insurance" in changed:
        assert "Convênio: Unimed" in restored[4]
    if "attendee_name" in changed:
        assert restored[3] == "Consulta - João"
    if "appointment_type" in changed:
        assert restored[3] == "Retorno - Maria"


# --------------------------------------------------------------------------
# A PAID sinal and a new SERVICE (plan 2026-10-26 Task 7): the money goes through
# services/payments/service_change.request_service_change (source "hub").
# Sinal: 10000 cents at 30 %. New sinal = price x 30 %.
# --------------------------------------------------------------------------

PRICED = [
    {"name": "Consulta", "price": "R$ 200,00", "duration_min": 30, "is_active": True},
    {"name": "Avaliação", "price": "R$ 500,00", "duration_min": 50, "is_active": True},
    {"name": "Retorno", "price": "R$ 100,00", "duration_min": 20, "is_active": True},
    {"name": "Sem preço", "duration_min": 30, "is_active": True},
]


@pytest.fixture
def provider(monkeypatch):
    from secretaria.services.payments import registry
    from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
    from tests.payments_support import FakeProvider

    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _paid_world(db, **kwargs):
    from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
    from secretaria.services import tenant_config as cfg
    from secretaria.services.payments import accounts
    from tests._reminders_v2 import seed_paid_deposit

    world = await _world(db, **kwargs)
    await seed_paid_deposit(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.appointment_types = PRICED
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.pix_difference_refund_min_days = 2
        tenant.deposit_hold_ttl_minutes = 30
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
        row.status = ACCOUNT_CONNECTED
        await session.commit()
    return world


async def _adjustments(db):
    from secretaria.models.deposit_adjustment import DepositAdjustment

    async with db() as session:
        return list(await session.scalars(select(DepositAdjustment)))


async def test_paid_dearer_service_waits_for_the_difference_pix_and_changes_nothing(
    db, provider
):
    world = await _paid_world(db)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    change = outcome.service_change
    assert (change.status, change.diff_cents) == ("awaiting_payment", 5000)
    assert (change.old_service, change.new_service) == ("Consulta", "Avaliação")
    assert change.pix_copy_paste == "000201pix" and change.expires_at is not None
    assert outcome.changes == ()  # the service did not change yet: nothing else to tell
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Consulta"
    assert _utc(row.end_at) - _utc(row.start_at) == timedelta(minutes=30)
    calendar = FakeCalendar.get("tenant")
    assert calendar.updated == [] and calendar.created == []
    # the longer window was checked before charging
    assert calendar.checked == [
        (world.start_at, world.start_at + timedelta(minutes=50), "evt-1")
    ]
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == ("awaiting_payment", 5000)
    assert adjustment.pending_edit["appointment_type"] == "Avaliação"
    assert adjustment.pending_edit["source"] == "hub"
    assert outcome.notice_id is None and await _notices(db, world.appointment.id) == []


async def test_paid_dearer_service_with_a_new_time_moves_the_time_now_and_the_service_on_payment(
    db, provider
):
    from secretaria.models import PixDeposit
    from secretaria.models.deposit_adjustment import STATUS_APPLIED, STATUS_AWAITING_PAYMENT
    from secretaria.services.payments import service_change

    world = await _paid_world(db)
    new = world.start_at + timedelta(days=1)

    outcome = await _edit(db, world, _req(service="Avaliação", start_at=new))

    assert outcome.service_change.status == "awaiting_payment"
    assert _labels(outcome) == ["Data"]  # the time moved now; the service waits
    [moved] = FakeCalendar.get("tenant").updated
    assert moved[:4] == ("evt-1", new, new + timedelta(minutes=30), "Consulta - Maria")
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (new, "Consulta")
    [notice] = await _notices(db, world.appointment.id)
    assert notice.changed_fields == ["data"] and outcome.notice_id == str(notice.id)

    # The Pix is paid: the pending edit applies onto the row the clinic left.
    [adjustment] = await _adjustments(db)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        adjustment = await session.merge(adjustment)
        now = datetime.now(UTC)
        assert await service_change.move_adjustment(
            session,
            adjustment,
            from_status=STATUS_AWAITING_PAYMENT,
            to_status=STATUS_APPLIED,
            now=now,
        )
        fields = await service_change.apply_paid_adjustment(session, tenant, adjustment, now=now)
        await session.commit()
    assert fields is not None and "serviço" in fields
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"
    assert _utc(row.end_at) == new + timedelta(minutes=50)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
    assert deposit.reschedule_count == 0  # a clinic move never counts


async def test_paid_cheaper_service_applies_now_syncs_the_agenda_and_asks(db, provider):
    world = await _paid_world(db)

    outcome = await _edit(db, world, _req(service="Retorno"))

    change = outcome.service_change
    assert (change.status, change.diff_cents) == ("asked", 7000)
    assert change.adjustment_id is not None and change.pix_copy_paste is None
    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Serviço", "Consulta", "Retorno")
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert _utc(row.end_at) == world.start_at + timedelta(minutes=20)
    [synced] = FakeCalendar.get("tenant").updated
    assert synced[:4] == (
        "evt-1",
        world.start_at,
        world.start_at + timedelta(minutes=20),
        "Retorno - Maria",
    )
    [notice] = await _notices(db, world.appointment.id)  # exactly ONE doctor notice
    assert "serviço" in notice.changed_fields and outcome.notice_id == str(notice.id)
    assert outcome.notice_version is not None
    [adjustment] = await _adjustments(db)
    assert (adjustment.id, adjustment.status) == (change.adjustment_id, "asked")
    assert provider.called("create_pix_charge") == 0


async def test_paid_cheaper_service_and_a_new_time_make_one_notice_with_every_field(
    db, provider
):
    world = await _paid_world(db)
    new = world.start_at + timedelta(days=1, hours=1)

    outcome = await _edit(db, world, _req(service="Retorno", start_at=new))

    assert outcome.service_change.status == "asked"
    assert _labels(outcome) == ["Data", "Horário", "Serviço"]
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (new, "Retorno")
    assert _utc(row.end_at) == new + timedelta(minutes=20)
    assert FakeCalendar.get("tenant").updated[-1][:4] == (
        "evt-1",
        new,
        new + timedelta(minutes=20),
        "Retorno - Maria",
    )
    notices = await _notices(db, world.appointment.id)
    assert len(notices) == 1
    assert set(notices[0].changed_fields) == {"data", "horário", "serviço"}
    assert outcome.notice_id == str(notices[0].id)


async def test_paid_service_change_that_cannot_be_priced_is_refused_and_nothing_changes(
    db, provider
):
    world = await _paid_world(db)
    new = world.start_at + timedelta(hours=2)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Sem preço", start_at=new))

    assert (err.value.status_code, err.value.code) == (422, "service_change_blocked")
    assert err.value.extra["reason"] == "price_unavailable"
    assert "Nada foi alterado" in err.value.message
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (world.start_at, "Consulta")
    _moved, restored = FakeCalendar.get("tenant").updated  # Google put back
    assert restored[1] == world.start_at
    assert await _adjustments(db) == [] and await _notices(db, world.appointment.id) == []


async def test_paid_dearer_service_when_the_pix_fails_applies_now_and_the_clinic_collects(
    db, provider
):
    """Follow-up B (2026-10-27): the provider down never blocks the clinic's change - it stands
    now (like a difference under R$ 10) and ONE item tells the clinic to collect it."""
    from secretaria.models import PaymentAttentionItem
    from secretaria.services.payments import service_change
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    world = await _paid_world(db)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    change = outcome.service_change
    assert (change.status, change.diff_cents) == ("applied_now", 5000)
    assert change.decision == service_change.DECISION_COLLECT
    assert change.pix_copy_paste is None and change.expires_at is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"
    assert _utc(row.end_at) - _utc(row.start_at) == timedelta(minutes=50)
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == ("collect_due", 5000)
    assert change.adjustment_id == adjustment.id
    async with db() as session:
        [item] = list(await session.scalars(select(PaymentAttentionItem)))
    assert (item.reason, item.amount_cents) == ("service_diff_collect_due", 5000)
    assert len(await _notices(db, world.appointment.id)) == 1  # ONE "Consulta alterada"


async def test_paid_dearer_service_refused_for_good_is_502_and_nothing_changes(db, provider):
    """Follow-up review I2: a revoked key (401) is not an outage - nothing is waived."""
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("charge_failed", status_code=401)
    world = await _paid_world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação"))

    assert (err.value.status_code, err.value.extra["reason"]) == (502, "charge_failed")
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert await _adjustments(db) == []


async def test_a_paid_change_while_the_difference_is_still_to_collect_is_409(db, provider):
    """Follow-up review I3: no second change (not even back) while the clinic's collect item
    of the previous one is open."""
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    world = await _paid_world(db)
    await _edit(db, world, _req(service="Avaliação"))
    provider.create_error = None

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Consulta"))

    assert (err.value.status_code, err.value.extra["reason"]) == (409, "collect_pending")
    assert "Nada foi alterado" in err.value.message
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"


async def test_a_second_paid_change_while_a_difference_is_unpaid_is_409(db, provider):
    world = await _paid_world(db)
    await _edit(db, world, _req(service="Avaliação"))

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Retorno"))

    assert (err.value.status_code, err.value.extra["reason"]) == (409, "adjustment_pending")
    assert "aguardando pagamento" in err.value.message
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert len(await _adjustments(db)) == 1


async def test_paid_longer_service_that_does_not_fit_is_409_before_any_charge(db, provider):
    world = await _paid_world(db)
    FakeCalendar.get("tenant").free = False

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação"))

    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert provider.called("create_pix_charge") == 0 and await _adjustments(db) == []


def _busy_between(calendar, busy_start, busy_end) -> None:
    """The agenda has another event in [busy_start, busy_end): a window touching it is refused."""

    async def is_slot_free(start, end, *, ignore_event_id=None) -> bool:
        calendar.checked.append((start, end, ignore_event_id))
        return end <= busy_start or start >= busy_end

    calendar.is_slot_free = is_slot_free


async def _dearer_but_shorter(db):
    """Old service lasts 60 min; "Avaliação" is dearer but only 50 min."""
    world = await _paid_world(db)
    await set_appointment(db, world.appointment.id, end_at=world.start_at + timedelta(minutes=60))
    return world


async def _edit_guarded(db, world, request, guarded: list):
    async def slot_guard(start, end, professional_id):
        guarded.append((start, end))

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        return await sae.apply_staff_edit(
            session, tenant, appointment, request, calendar_for=calendar_for, slot_guard=slot_guard
        )


async def test_paid_dearer_shorter_service_with_a_new_time_checks_the_old_longer_window(
    db, provider
):
    """Until the Pix is paid the row keeps the OLD (longer) service at the new time: its tail
    must be free too - never a double booking in [new+50, new+60)."""
    world = await _dearer_but_shorter(db)
    new = world.start_at + timedelta(days=1)
    calendar = FakeCalendar.get("tenant")
    _busy_between(calendar, new + timedelta(minutes=55), new + timedelta(minutes=90))
    guarded: list = []

    with pytest.raises(StaffEditError) as err:
        await _edit_guarded(db, world, _req(service="Avaliação", start_at=new), guarded)

    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert guarded == [(new, new + timedelta(minutes=60))]  # the hold gate saw the whole window
    assert calendar.updated == [] and calendar.created == []
    assert provider.called("create_pix_charge") == 0 and await _adjustments(db) == []
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (world.start_at, "Consulta")


async def test_paid_dearer_shorter_service_with_a_new_time_and_a_free_tail_waits_for_the_pix(
    db, provider
):
    world = await _dearer_but_shorter(db)
    new = world.start_at + timedelta(days=1)
    calendar = FakeCalendar.get("tenant")
    _busy_between(calendar, new + timedelta(minutes=60), new + timedelta(minutes=90))
    guarded: list = []

    outcome = await _edit_guarded(db, world, _req(service="Avaliação", start_at=new), guarded)

    assert outcome.service_change.status == "awaiting_payment"
    assert guarded == [(new, new + timedelta(minutes=60))]
    assert calendar.checked == [(new, new + timedelta(minutes=60), "evt-1")]
    [moved] = calendar.updated
    assert moved[1:3] == (new, new + timedelta(minutes=60))
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), _utc(row.end_at)) == (new, new + timedelta(minutes=60))
    assert row.appointment_type == "Consulta"


async def test_paid_sinal_without_a_service_change_keeps_todays_behaviour(db, provider):
    world = await _paid_world(db, professional_name="Dra. Ana")
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(professional_id=new_pro, insurance="Unimed"))

    assert outcome.service_change is None
    assert _labels(outcome) == ["Médico", "Convênio"]
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.insurance) == (new_pro, "Unimed")
    assert await _adjustments(db) == [] and provider.calls == []
