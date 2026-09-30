"""The clinic's side of attention items and the agenda reads (spec sections 6.5, 10A, 13).

`GET/POST /tenants/me/payment-attention-items`, the `needs_attention` key of `deposit` on
`GET /calendar/events`, `GET /calendar/awaiting-payment`, and the one-per-item clinic e-mail of a
paid consultation that was cancelled by the patient. The role gate of the resolve endpoint lives in
`tests/test_payment_attention_roles.py`. The
guarantees pinned here: nothing crosses clinics (an id of another clinic is a 404, never a 403);
the output never carries a phone, a charge id or a payer; a note with a CPF or an e-mail is refused
without being echoed; the first decision is never overwritten; and resolving moves no money except
the one honest record (`cancelado_reembolsado`) the clinic asks for.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub import calendar as hub_calendar, payment_attention as hub_attention
from secretaria.api.hub.deps import get_current_tenant, require_clinic_clinician
from secretaria.core.database import get_session
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.payment_attention_item import ATTENTION_REASONS
from secretaria.models.pix_deposit import (
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_RESOLVED,
    PixDepositStatus,
)
from secretaria.services import email
from secretaria.services.payments import attention_alerts
from secretaria.services.tenant_config import set_google_refresh_token
from secretaria.workers import payments_cron
from tests.before_support import make_world
from tests.payments_support import FakeArqPool, make_sessionmaker

ITEMS = "/tenants/me/payment-attention-items"
NOTE = "Devolvi o valor no painel do provedor hoje de manhã."
BODY = {"action": "refunded_in_provider_panel", "note": NOTE, "resolved_by_name": "Dra. Ana"}
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(require_clinic_clinician, None)
    app.state.arq_pool = None


def _wire(db, tenant_id, monkeypatch=None):
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    # The resolve endpoint depends on the TASK-020 role gate. These tests are about the endpoint's
    # behaviour, so the gate is stood in for by the same tenant stub (the gate itself is proved in
    # `tests/test_payment_attention_roles.py`, with real tokens).
    app.dependency_overrides[require_clinic_clinician] = _tenant
    app.state.arq_pool = FakeArqPool()


async def _world(db, **kw):
    async with db() as session:
        world = await make_world(session, timing="before", **kw)
    _wire(db, world.tenant_id)
    return world


async def _item(
    db,
    world,
    *,
    reason="late_slot_taken",
    deposit_status=PixDepositStatus.PAID,
    fulfillment=FULFILLMENT_NEEDS_ATTENTION,
    detected_ago=timedelta(minutes=5),
    status="open",
    dedupe=None,
    deposit=None,
    **extra,
):
    async with db() as session:
        if deposit is None:
            deposit = PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant_id,
                patient_id=world.patient.id,
                provider="asaas",
                provider_charge_id=f"pay_{uuid4().hex[:8]}",
                asaas_payment_id=None,
                amount_cents=6000,
                percent_applied=30,
                paid_amount_cents=6000,
                status=deposit_status,
                fulfillment_status=fulfillment,
                paid_at=NOW,
            )
            session.add(deposit)
            await session.flush()
        item = PaymentAttentionItem(
            id=uuid4(),
            tenant_id=world.tenant_id,
            deposit_id=deposit.id,
            provider="asaas",
            provider_charge_id=deposit.provider_charge_id,
            patient_id=world.patient.id,
            reason=reason,
            status=status,
            amount_cents=6000,
            paid_amount_cents=6000,
            paid_at=NOW,
            slot_start=NOW + timedelta(days=2),
            slot_end=NOW + timedelta(days=2, minutes=30),
            service_label="Consulta",
            detected_at=NOW - detected_ago,
            dedupe_key=dedupe or f"asaas:{uuid4().hex}:{reason}",
            **extra,
        )
        session.add(item)
        await session.commit()
        return item.id, deposit.id


# ---- list ------------------------------------------------------------------------------------


async def test_the_list_is_a_whitelist_with_the_patient_as_a_display_name_only(
    client: AsyncClient, db
):
    world = await _world(db)
    await _item(db, world)
    body = (await client.get(ITEMS)).json()
    assert body["open_count"] == 1 and body["next_cursor"] is None
    (item,) = body["items"]
    assert set(item) == {
        "id", "reason", "reason_text", "patient_display_name", "service_label", "slot_start",
        "slot_end", "amount_cents", "paid_amount_cents", "paid_at", "provider",
        "suggested_refund_cents", "provider_refund_seen_at", "detected_at", "status",
        "resolution",
    }
    assert item["patient_display_name"] == "Maria"
    assert item["reason_text"] == "Pagamento recebido, mas o horário escolhido já não está livre."
    text = str(body)
    assert "5511999999999" not in text and "pay_" not in text  # no phone, no charge id


async def test_a_patient_without_a_name_is_null_so_the_screen_can_say_paciente_sem_nome(
    client: AsyncClient, db
):
    world = await _world(db)
    async with db() as session:
        patient = await session.get(type(world.patient), world.patient.id)
        patient.name = None
        await session.commit()
    await _item(db, world)
    assert (await client.get(ITEMS)).json()["items"][0]["patient_display_name"] is None


async def test_the_refund_policy_text_carries_the_suggested_value(client: AsyncClient, db):
    world = await _world(db)
    await _item(db, world, reason="refund_due_policy", suggested_refund_cents=3000)
    (item,) = (await client.get(ITEMS)).json()["items"]
    assert item["reason_text"] == (
        "Cancelamento dentro da política de devolução: valor sugerido R$ 30,00."
    )


async def test_another_clinics_items_never_appear_and_its_id_is_a_404(client: AsyncClient, db):
    world = await _world(db)
    async with db() as session:
        other = await make_world(session, timing="before")
    foreign_id, _ = await _item(db, other)
    await _item(db, world)
    body = (await client.get(ITEMS)).json()
    assert body["open_count"] == 1 and len(body["items"]) == 1
    response = await client.post(f"{ITEMS}/{foreign_id}/resolve", json=BODY)
    assert response.status_code == 404 and response.json() == {"detail": {"code": "not_found"}}
    async with db() as session:
        assert (await session.get(PaymentAttentionItem, foreign_id)).status == "open"


async def test_the_list_is_newest_first_and_pages_with_a_cursor(client: AsyncClient, db):
    world = await _world(db)
    ids = [
        (await _item(db, world, detected_ago=timedelta(minutes=m)))[0] for m in (30, 20, 10, 5)
    ]
    first = (await client.get(ITEMS, params={"limit": 3})).json()
    assert [i["id"] for i in first["items"]] == [str(i) for i in reversed(ids)][:3]
    assert first["open_count"] == 4 and first["next_cursor"]
    second = (await client.get(ITEMS, params={"limit": 3, "cursor": first["next_cursor"]})).json()
    assert [i["id"] for i in second["items"]] == [str(ids[0])]
    assert second["next_cursor"] is None


async def test_the_open_count_is_present_when_listing_resolved_and_they_stay_90_days(
    client: AsyncClient, db
):
    world = await _world(db)
    await _item(db, world)  # one open
    recent, _ = await _item(db, world, status="resolved", resolved_at=NOW - timedelta(days=10),
                            resolution_action="other", resolution_note=NOTE,
                            resolved_by_name="Dra. Ana")
    await _item(db, world, status="resolved", resolved_at=NOW - timedelta(days=120),
                resolution_action="other", resolution_note=NOTE, resolved_by_name="Dra. Ana")
    body = (await client.get(ITEMS, params={"status": "resolved"})).json()
    assert body["open_count"] == 1
    assert [i["id"] for i in body["items"]] == [str(recent)]
    assert body["items"][0]["resolution"]["note"] == NOTE


async def test_an_unknown_status_is_refused(client: AsyncClient, db):
    await _world(db)
    assert (await client.get(ITEMS, params={"status": "everything"})).status_code == 422


# ---- resolve ---------------------------------------------------------------------------------


async def test_resolving_records_the_decision_and_closes_the_deposit_story(
    client: AsyncClient, db
):
    world = await _world(db)
    item_id, deposit_id = await _item(db, world)
    response = await client.post(f"{ITEMS}/{item_id}/resolve", json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution"]["action"] == "refunded_in_provider_panel"
    assert body["resolution"]["resolved_by_name"] == "Dra. Ana"
    assert (await client.get(ITEMS)).json()["open_count"] == 0
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        stored = await session.get(PaymentAttentionItem, item_id)
    assert deposit.fulfillment_status == FULFILLMENT_RESOLVED
    assert stored.resolved_at is not None  # stamped by the SERVER clock


async def test_only_the_clinic_saying_it_refunded_writes_cancelado_reembolsado(
    client: AsyncClient, db
):
    world = await _world(db)
    item_id, deposit_id = await _item(db, world, reason="paid_then_cancelled")
    await client.post(f"{ITEMS}/{item_id}/resolve", json=BODY)
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
    assert deposit.status == PixDepositStatus.CANCELLED_REFUNDED
    assert deposit.refunded_amount_cents == 6000


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        ("late_slot_taken", "rebooked"),
        ("late_slot_taken", "kept_as_credit"),
        ("late_slot_taken", "other"),
        ("amount_over", "refunded_in_provider_panel"),  # the excess only: the consultation stands
        ("duplicate_payment", "refunded_in_provider_panel"),
    ],
)
async def test_every_other_resolution_leaves_the_money_status_alone(
    client: AsyncClient, db, reason, action
):
    world = await _world(db)
    item_id, deposit_id = await _item(db, world, reason=reason)
    body = {**BODY, "action": action}
    assert (await client.post(f"{ITEMS}/{item_id}/resolve", json=body)).status_code == 200
    async with db() as session:
        assert (await session.get(PixDeposit, deposit_id)).status == PixDepositStatus.PAID


async def test_the_deposit_stays_flagged_until_its_last_open_item_is_resolved(
    client: AsyncClient, db
):
    world = await _world(db)
    first, deposit_id = await _item(db, world, reason="amount_over")
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
    second, _ = await _item(db, world, reason="duplicate_payment", deposit=deposit)
    await client.post(f"{ITEMS}/{first}/resolve", json=BODY)
    async with db() as session:
        assert (await session.get(PixDeposit, deposit_id)).fulfillment_status == (
            FULFILLMENT_NEEDS_ATTENTION
        )
    await client.post(f"{ITEMS}/{second}/resolve", json=BODY)
    async with db() as session:
        assert (await session.get(PixDeposit, deposit_id)).fulfillment_status == (
            FULFILLMENT_RESOLVED
        )


async def test_a_second_resolve_is_409_with_the_state_and_never_overwrites_the_first(
    client: AsyncClient, db
):
    world = await _world(db)
    item_id, _ = await _item(db, world)
    await client.post(f"{ITEMS}/{item_id}/resolve", json=BODY)
    second = await client.post(
        f"{ITEMS}/{item_id}/resolve",
        json={"action": "other", "note": "Outra decisão qualquer aqui.", "resolved_by_name": "Rui"},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "already_resolved"
    assert detail["item"]["resolution"]["note"] == NOTE  # the FIRST decision stands
    assert detail["item"]["resolution"]["resolved_by_name"] == "Dra. Ana"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"note": "curta"}, "note_too_short"),
        ({"note": "x" * 501}, "note_too_long"),
        ({"note": "Paciente CPF 123.456.789-09 pediu."}, "note_has_personal_data"),
        ({"note": "Paciente CPF 12345678909 pediu."}, "note_has_personal_data"),
        ({"note": "Falei com maria@example.com ontem."}, "note_has_personal_data"),
        ({"note": None}, "note_required"),
        ({"resolved_by_name": ""}, "resolved_by_required"),
        ({"resolved_by_name": "x" * 81}, "resolved_by_too_long"),
        ({"action": "refund"}, "invalid_action"),
    ],
)
async def test_an_invalid_resolution_is_a_422_that_never_echoes_what_was_typed(
    client: AsyncClient, db, patch, code
):
    world = await _world(db)
    item_id, _ = await _item(db, world)
    response = await client.post(f"{ITEMS}/{item_id}/resolve", json={**BODY, **patch})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": code}}
    for typed in ("123.456.789-09", "12345678909", "maria@example.com"):
        assert typed not in response.text
    async with db() as session:
        assert (await session.get(PaymentAttentionItem, item_id)).status == "open"


async def test_a_note_with_dates_and_amounts_is_not_mistaken_for_a_cpf(client: AsyncClient, db):
    world = await _world(db)
    item_id, _ = await _item(db, world)
    note = "Devolvidos R$ 60,00 em 12/10 às 14:30, protocolo 88 do provedor."
    response = await client.post(f"{ITEMS}/{item_id}/resolve", json={**BODY, "note": note})
    assert response.status_code == 200


async def test_a_body_that_is_not_json_is_a_422_and_a_malformed_id_a_404(
    client: AsyncClient, db
):
    world = await _world(db)
    item_id, _ = await _item(db, world)
    bad = await client.post(
        f"{ITEMS}/{item_id}/resolve", content=b"not json", headers={"content-type": "text/plain"}
    )
    assert bad.status_code == 422 and bad.json() == {"detail": {"code": "invalid_body"}}
    assert (await client.post(f"{ITEMS}/nope/resolve", json=BODY)).status_code == 404


async def test_resolving_is_rate_limited_per_clinic_but_never_needs_redis(
    client: AsyncClient, db, monkeypatch
):
    from secretaria.main import app

    world = await _world(db)
    monkeypatch.setattr(hub_attention, "RESOLVE_LIMIT", 2)
    ids = [(await _item(db, world))[0] for _ in range(3)]
    statuses = [(await client.post(f"{ITEMS}/{i}/resolve", json=BODY)).status_code for i in ids]
    assert statuses == [200, 200, 429]
    app.state.arq_pool = None  # a Redis outage must not stop a clinic from closing an item
    extra, _ = await _item(db, world)
    assert (await client.post(f"{ITEMS}/{extra}/resolve", json=BODY)).status_code == 200


# ---- the agenda ------------------------------------------------------------------------------


class Calendar:
    events: list[dict] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def check_availability(self, start, end, max_results=50):
        return list(Calendar.events)


async def test_the_agenda_deposit_gains_needs_attention_and_keeps_its_other_keys(
    client: AsyncClient, db, monkeypatch
):
    world = await _world(db)
    async with db() as session:
        await set_google_refresh_token(session, world.tenant_id, "fake")
        flagged = Appointment(
            id=uuid4(), tenant_id=world.tenant_id, google_event_id="evt-flag",
            appointment_type="Consulta", status=AppointmentStatus.CANCELLED,
        )
        plain = Appointment(
            id=uuid4(), tenant_id=world.tenant_id, google_event_id="evt-plain",
            appointment_type="Consulta", status=AppointmentStatus.SCHEDULED,
        )
        session.add_all([flagged, plain])
        await session.flush()
        for appointment, fulfillment in ((flagged, "needs_attention"), (plain, "confirmed")):
            session.add(
                PixDeposit(
                    tenant_id=world.tenant_id, appointment_id=appointment.id, amount_cents=6000,
                    percent_applied=30, status=PixDepositStatus.PAID,
                    fulfillment_status=fulfillment,
                )
            )
        await session.commit()
    Calendar.events = [
        {"id": "evt-flag", "summary": "A", "start": NOW.isoformat(), "end": NOW.isoformat()},
        {"id": "evt-plain", "summary": "B", "start": NOW.isoformat(), "end": NOW.isoformat()},
    ]
    monkeypatch.setattr(hub_calendar, "CalendarService", Calendar)
    response = await client.get(
        "/tenants/me/calendar/events",
        params={"start": NOW.isoformat(), "end": (NOW + timedelta(days=1)).isoformat()},
    )
    by_id = {e["id"]: e["deposit"] for e in response.json()}
    assert by_id["evt-flag"] == {
        "status": "confirmado_pago", "amount_cents": 6000, "needs_attention": True
    }
    assert by_id["evt-plain"]["needs_attention"] is False
    assert set(by_id["evt-plain"]) == {"status", "amount_cents", "needs_attention"}


async def _payment_hold(db, world, *, charge_status=PixDepositStatus.AWAITING, phase="payment",
                        minutes_left=20, start=None):
    start = start or NOW + timedelta(days=2)
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(), tenant_id=world.tenant_id, patient_id=world.patient.id,
            amount_cents=6000, percent_applied=30, status=charge_status,
            provider_charge_id=f"pay_secret_{uuid4().hex[:6]}",
        )
        session.add(deposit)
        await session.flush()
        hold = BookingHold(
            id=uuid4(), tenant_id=world.tenant_id, conversation_id=world.conversation.id,
            patient_id=world.patient.id, appointment_type="Consulta", start_at=start,
            end_at=start + timedelta(minutes=30),
            expires_at=NOW + timedelta(minutes=minutes_left), phase=phase, charge_id=deposit.id,
        )
        session.add(hold)
        await session.commit()
        return hold.id


def _window(days=(0, 5)):
    return {
        "start": (NOW + timedelta(days=days[0])).isoformat(),
        "end": (NOW + timedelta(days=days[1])).isoformat(),
    }


async def test_awaiting_payment_lists_only_live_payable_reservations_without_pii(
    client: AsyncClient, db
):
    world = await _world(db)
    hold_id = await _payment_hold(db, world)
    body = (await client.get("/tenants/me/calendar/awaiting-payment", params=_window())).json()
    (row,) = body
    assert row["id"] == str(hold_id) and row["service"] == "Consulta"
    assert set(row) == {"id", "start", "end", "service", "expires_at", "professional_id"}
    text = str(body)
    assert "pay_secret" not in text and "Maria" not in text and "6000" not in text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes_left": -1},  # the window ended
        {"phase": "code"},  # waiting for a code, not for money
        {"charge_status": PixDepositStatus.PAID},  # already paid: it is (becoming) an appointment
        {"charge_status": PixDepositStatus.EXPIRED},
    ],
)
async def test_awaiting_payment_hides_holds_that_are_not_payable(
    client: AsyncClient, db, kwargs
):
    world = await _world(db)
    await _payment_hold(db, world, **kwargs)
    assert (
        await client.get("/tenants/me/calendar/awaiting-payment", params=_window())
    ).json() == []


async def test_awaiting_payment_respects_the_window_and_the_clinic(client: AsyncClient, db):
    world = await _world(db)
    async with db() as session:
        other = await make_world(session, timing="before")
    await _payment_hold(db, world, start=NOW + timedelta(days=20))
    await _payment_hold(db, other)
    assert (
        await client.get("/tenants/me/calendar/awaiting-payment", params=_window())
    ).json() == []


# ---- the clinic e-mail -----------------------------------------------------------------------


class Mailbox:
    """The clinic's inbox. `deliver=False` is an SMTP that refuses: nothing left the building.
    `meanwhile` runs INSIDE the send, which is where a second worker would sweep."""

    def __init__(self, monkeypatch, *, deliver: bool = True, meanwhile=None):
        self.sent: list[tuple] = []
        self.deliver = deliver
        self.meanwhile = meanwhile

        async def fake(to_email, clinic_name):
            if self.meanwhile is not None:
                await self.meanwhile()
            if self.deliver:
                self.sent.append((to_email, clinic_name))
            return self.deliver

        monkeypatch.setattr(email, "send_paid_cancellation_alert", fake)


async def _tick(db, now=None):
    async with db() as session:
        return await attention_alerts.alert_new_items(session, now=now or NOW)


async def _stamps(db):
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem.alerted_at)))


async def _clinic_with_mail(db):
    async with db() as session:
        return await make_world(session, timing="before", contact_email="c@example.com")


def test_only_a_patient_cancelling_a_paid_consultation_is_worth_an_email():
    # `refund_due_policy` is the item `on_appointment_cancelled` opens when the PATIENT cancels a
    # consultation whose deposit was paid; `paid_then_cancelled` (the clinic cancelled, or the
    # payment came after the cancellation) and every other reason stay on screen only.
    assert attention_alerts.EMAIL_REASONS == frozenset({"refund_due_policy"})


async def test_a_cancelled_paid_consultation_e_mails_the_clinic_once_per_item(db, monkeypatch):
    mailbox = Mailbox(monkeypatch)
    world = await _clinic_with_mail(db)
    await _item(db, world, reason="refund_due_policy")
    await _item(db, world, reason="refund_due_policy")
    report = await _tick(db)
    assert report.emailed == 2  # two items, two e-mails: one per item, never one per clinic
    assert mailbox.sent == [("c@example.com", "Clinic")] * 2
    assert all(await _stamps(db))
    assert (await _tick(db)).emailed == 0 and len(mailbox.sent) == 2  # never announced twice


async def test_a_worker_that_sweeps_while_another_is_sending_never_repeats_the_e_mail(
    db, monkeypatch
):
    ran: list[bool] = []

    async def second_worker():
        if not ran:
            ran.append(True)
            await _tick(db)  # the same sweep, at the same moment, in another worker

    mailbox = Mailbox(monkeypatch, meanwhile=second_worker)
    world = await _clinic_with_mail(db)
    await _item(db, world, reason="refund_due_policy")
    await _tick(db)
    assert len(mailbox.sent) == 1  # the claim is taken BEFORE the send, atomically
    assert (await _tick(db)).emailed == 0


@pytest.mark.parametrize(
    "reason", sorted(ATTENTION_REASONS - attention_alerts.EMAIL_REASONS)
)
async def test_every_other_reason_sends_nothing_and_stays_unmarked(db, monkeypatch, reason):
    mailbox = Mailbox(monkeypatch)
    world = await _clinic_with_mail(db)
    await _item(db, world, reason=reason)
    report = await _tick(db)
    assert report.candidates == 0 and mailbox.sent == []
    assert await _stamps(db) == [None]  # on screen only: never marked as announced


async def test_a_failed_send_releases_the_claim_and_is_tried_again(db, monkeypatch):
    mailbox = Mailbox(monkeypatch, deliver=False)
    world = await _clinic_with_mail(db)
    await _item(db, world, reason="refund_due_policy")
    report = await _tick(db)
    assert report.released == 1 and mailbox.sent == []
    assert await _stamps(db) == [None]  # nothing left the building, so it is still owed
    mailbox.deliver = True
    assert (await _tick(db)).emailed == 1 and len(mailbox.sent) == 1
    assert (await _tick(db)).emailed == 0  # and once it left, never again


async def test_a_clinic_without_a_contact_email_is_skipped_without_marking_anything(
    db, monkeypatch
):
    mailbox = Mailbox(monkeypatch)
    async with db() as session:
        world = await make_world(session, timing="before")  # no contact_email
    await _item(db, world, reason="refund_due_policy")
    report = await _tick(db)
    assert report.skipped_no_contact == 1 and mailbox.sent == []
    assert await _stamps(db) == [None]  # owed until the clinic has an address


async def test_resolved_items_are_never_announced(db, monkeypatch):
    mailbox = Mailbox(monkeypatch)
    world = await _clinic_with_mail(db)
    await _item(db, world, reason="refund_due_policy", status="resolved", resolved_at=NOW,
                resolution_action="other", resolution_note=NOTE, resolved_by_name="Dra. Ana")
    assert (await _tick(db)).candidates == 0 and mailbox.sent == []


async def test_the_email_names_no_patient_and_no_amount_and_says_whether_it_left(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: captured.update(
        to=to, subject=subject, body=body))
    monkeypatch.setattr(email, "get_settings", lambda: type("S", (), {"SMTP_HOST": "smtp"})())
    assert await email.send_paid_cancellation_alert("c@example.com", "Clinica Sol") is True
    assert captured["subject"] == "[SecretarIA] Consulta cancelada com sinal já pago — Clinica Sol"
    assert "cancelou uma consulta cujo sinal já estava pago" in captured["body"]
    assert "Abra o Chat" in captured["body"]
    assert "R$" not in captured["body"] and "Maria" not in captured["body"]

    def refuses(to, subject, body):
        raise OSError("smtp refused")

    monkeypatch.setattr(email, "_send_sync", refuses)
    assert await email.send_paid_cancellation_alert("c@example.com", "Clinica Sol") is False
    monkeypatch.setattr(email, "get_settings", lambda: type("S", (), {"SMTP_HOST": ""})())
    assert await email.send_paid_cancellation_alert("c@example.com", "Clinica Sol") is False


def test_the_attention_alert_cron_never_shares_a_minute_with_another_cron():
    from secretaria.workers import arq_worker

    minutes = payments_cron.ATTENTION_ALERT_MINUTES
    others = [
        payments_cron.EXPIRY_MINUTES,
        payments_cron.RECONCILE_MINUTES,
        payments_cron.SWEEP_MINUTES,
        {0, 15, 30, 45},  # handover
        set(range(0, 60, 5)),  # reminders
        {10},  # onboarding nudges
        {7},  # deploy parity
    ]
    for used in others:
        assert not (minutes & set(used))
    assert any(
        getattr(job, "coroutine", None) is payments_cron.run_attention_alerts
        for job in arq_worker.WorkerSettings.cron_jobs
    )
