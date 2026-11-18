"""TASK-085 R / A1 - with a price notice pending, the FIRST message of any Portal entry is the
price action (D-R14), never the ordinary opening card.

The defect (U7, e2e A1): opening the e-mail's link fires TWO entries - the account opening
(`/patient-access/pending`, no `entry_context`) and the Portal's `/enter` (`reminder_link`) - with
different ledger keys (`brain_message_enter:...` vs `reminder_entry:...`). Past the 30-minute quiet
window the ordinary one sent the usual card (sometimes of ANOTHER consultation) before or next to
the price message.

The fix (secretarIA only): an ordinary entry keeps EVERY ordinary guard (quiet window, flow in
progress, human, consent / identity); when they all pass and the patient has exactly ONE PENDING
`price_change` notice (this clinic, this patient, unanswered, its consultation live, ahead, not
suspended, of its current start), it sends that notice's price action instead of the card, under
the link's own ledger key. On the same conversation state the ledger deduplicates the two entries;
in sequence, the later link entry is silenced by UB2's `_price_notice_already_shown` (or the later
ordinary entry by the quiet window). With SEVERAL pending notices the ordinary entry stays silent
(it cannot know which e-mail was clicked) and the link speaks alone (review M1). A clinic without
the sinal is never looked up (L3). Another clinic is never affected.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    FlowState,
    HandoverState,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import price_change
from secretaria.workers.portal import open as open_mod
from secretaria.workers.portal.open import process_brain_message_enter
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import add_appointment, consent, set_conversation
from tests._reminders_v2 import seed_world
from tests.payments_support import FakeRedis
from tests.test_sinal_trocas_ub2_entry import (  # noqa: F401 - fixtures
    add_notice,
    bodies,
    portal,
    provider,
    reprice,
    seed,
)

PRICE_PIX = "O valor da sua consulta (Consulta, "


async def quiet_world(portal):  # noqa: F811
    """Reminders v2 ON (the ordinary opening is the consultation's "LEMBRE-SE" card), the
    conversation idle for a day (past the quiet window), a PAID sinal of R$ 60 at R$ 200, the
    clinic raised the price to R$ 300 and e-mailed the notice."""
    world, _deposit = await seed(
        portal, v2=True, last_inbound_at=datetime.now(UTC) - timedelta(days=1)
    )
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")
    return world, rid


async def enter_plain(world) -> None:
    """The account opening (`_open_with_account` -> `/internal/brain-message/open`): no context."""
    await process_brain_message_enter(
        {"redis": FakeRedis()}, str(world.tenant.id), world.patient.external_id
    )


async def enter_link(world, rid) -> None:
    """The Portal's `/enter` with the e-mail's `lembrete=`."""
    await process_brain_message_enter(
        {"redis": FakeRedis()},
        str(world.tenant.id),
        world.patient.external_id,
        entry_context={"source": "reminder_link", "reminder_id": str(rid)},
    )


def interleave(monkeypatch, other):
    """Both entries decide on the SAME conversation state: right after the first one claims its
    ledger key (before it writes anything), the other runs whole."""
    real = open_mod._claim_event
    state = {"done": False}

    async def _claim(key: str) -> bool:
        won = await real(key)
        if won and not state["done"]:
            state["done"] = True
            await other()
        return won

    monkeypatch.setattr(open_mod, "_claim_event", _claim)


async def answered_at(portal, rid):  # noqa: F811
    async with portal() as session:
        return (await session.get(AppointmentReminder, rid)).answered_at


async def test_both_entries_decide_the_same_ledger_key(portal, provider):  # noqa: F811
    world, rid = await quiet_world(portal)

    plain = await open_mod._brain_message_entry_decision(world.tenant.id, world.patient.external_id)
    link = await open_mod._brain_message_entry_decision(
        world.tenant.id,
        world.patient.external_id,
        entry_context=open_mod.ConversationEntryContext(source="reminder_link", reminder_id=rid),
    )

    assert plain is not None and link is not None
    assert plain.ledger_key == link.ledger_key
    assert plain.price_change_reminder_id == link.price_change_reminder_id == rid
    # Review L1: the ordinary entry keeps its own source (the fallback card resets to MENU).
    assert plain.source == "portal_entry" and link.source == "reminder_link"
    assert plain.appointment_id is None


async def test_ordinary_then_link_sends_only_the_price(portal, provider):  # noqa: F811
    world, rid = await quiet_world(portal)

    await enter_plain(world)
    await enter_link(world, rid)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX) and "COPYDIFF" in body
    assert provider.called("create_pix_charge") == 1


async def test_ordinary_then_link_on_the_same_state_sends_only_the_price(
    portal,  # noqa: F811
    provider,  # noqa: F811
    monkeypatch,
):
    world, rid = await quiet_world(portal)
    interleave(monkeypatch, lambda: enter_link(world, rid))

    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX)
    assert provider.called("create_pix_charge") == 1


async def test_link_then_ordinary_sends_only_the_price(portal, provider):  # noqa: F811
    world, rid = await quiet_world(portal)

    await enter_link(world, rid)
    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX)
    assert provider.called("create_pix_charge") == 1


async def test_link_then_ordinary_on_the_same_state_sends_one_price_and_no_card(
    portal,  # noqa: F811
    provider,  # noqa: F811
    monkeypatch,
):
    """The e2e order: the explicit entry claimed first, the ordinary one decided on the same
    (idle) conversation - it must not send the card."""
    world, rid = await quiet_world(portal)
    interleave(monkeypatch, lambda: enter_plain(world))

    await enter_link(world, rid)

    sent = await bodies(portal, world)
    assert len(sent) == 1 and sent[0].startswith(PRICE_PIX)
    assert not any(body.startswith("LEMBRE-SE:") for body in sent)
    assert provider.called("create_pix_charge") == 1


async def test_an_ordinary_entry_with_a_pending_notice_opens_with_the_price(portal, provider):  # noqa: F811
    """Without the link at all (the Portal opened by hand): the price action comes first."""
    world, rid = await quiet_world(portal)

    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX)
    assert await answered_at(portal, rid) is not None


async def test_inside_the_quiet_window_the_ordinary_entry_is_silent_and_the_link_acts(
    portal,  # noqa: F811
    provider,  # noqa: F811
):
    """Orchestrator follow-up: an ordinary entry keeps the ordinary guards - a refresh mid-chat
    never brings the price message. The link (which ignores the quiet window) acts, once."""
    world, _deposit = await seed(portal)  # last message a minute ago
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")

    await enter_plain(world)

    assert await bodies(portal, world) == [] and provider.calls == []
    assert await answered_at(portal, rid) is None

    await enter_link(world, rid)
    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX)
    assert provider.called("create_pix_charge") == 1
    assert await answered_at(portal, rid) is not None


async def test_a_flow_in_progress_keeps_the_ordinary_entry_silent(portal, provider):  # noqa: F811
    """Mid-booking (past the quiet window, inside the reactivation gap): the ordinary entry stays
    silent even with a pending notice - never cut a booking in half. The link still acts."""
    world, _deposit = await seed(
        portal, v2=True, last_inbound_at=datetime.now(UTC) - timedelta(hours=2)
    )
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")
    await set_conversation(portal, world, flow_state=FlowState.SERVICE_CATALOG)

    await enter_plain(world)

    assert await bodies(portal, world) == [] and provider.calls == []
    assert await answered_at(portal, rid) is None

    await enter_link(world, rid)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX)
    assert provider.called("create_pix_charge") == 1


async def _second_notice(portal, world, *, sent_at):  # noqa: F811
    """A second consultation (no sinal) of the same patient with its own pending notice."""
    second = await add_appointment(portal, world, start_at=datetime.now(UTC) + timedelta(days=8))
    async with portal() as session:
        row = AppointmentReminder(
            tenant_id=world.tenant.id,
            appointment_id=second.id,
            patient_id=world.patient.id,
            kind=REMINDER_KIND_PRICE_CHANGE,
            appointment_start_at=second.start_at,
            due_at=sent_at,
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_EMAIL,
            with_prompt=False,
            sent_at=sent_at,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_with_two_pending_notices_only_the_clicked_link_speaks(portal, provider):  # noqa: F811
    """Review M1 (the reviewer's probe): two notices pending, the ordinary entry arrives first,
    then the link to the notice an ordinary pick would NOT have chosen (the older one). The
    ordinary entry stays silent - no price, no card - and exactly one price message goes out,
    the clicked notice's."""
    world, clicked = await quiet_world(portal)
    async with portal() as session:
        row = await session.get(AppointmentReminder, clicked)
        row.sent_at = datetime.now(UTC) - timedelta(days=2)
        await session.commit()
    other = await _second_notice(portal, world, sent_at=datetime.now(UTC))

    await enter_plain(world)

    assert await bodies(portal, world) == [] and provider.calls == []

    await enter_link(world, clicked)

    [body] = await bodies(portal, world)
    assert body.startswith(PRICE_PIX) and "COPYDIFF" in body
    assert provider.called("create_pix_charge") == 1
    assert await answered_at(portal, clicked) is not None
    assert await answered_at(portal, other) is None


async def test_with_two_pending_notices_the_ordinary_decision_is_silence(portal, provider):  # noqa: F811
    world, _rid = await quiet_world(portal)
    await _second_notice(portal, world, sent_at=datetime.now(UTC))

    assert (
        await open_mod._brain_message_entry_decision(world.tenant.id, world.patient.external_id)
        is None
    )


async def _assert_no_takeover(portal, world, rid) -> None:  # noqa: F811
    decision = await open_mod._brain_message_entry_decision(
        world.tenant.id, world.patient.external_id
    )
    if decision is not None:
        assert decision.price_change_reminder_id is None
        assert decision.ledger_key.startswith("brain_message_enter:")
    await enter_plain(world)
    sent = await bodies(portal, world)
    assert not any(body.startswith("O valor da sua consulta") for body in sent)
    assert price_change.PRICE_CLOSED_TEXT not in sent
    assert await answered_at(portal, rid) is None


async def test_a_cancelled_consultation_notice_does_not_take_over(portal, provider):  # noqa: F811
    """Review L2."""
    world, rid = await quiet_world(portal)
    async with portal() as session:
        (await session.get(Appointment, world.appointment.id)).status = AppointmentStatus.CANCELLED
        await session.commit()

    await _assert_no_takeover(portal, world, rid)
    assert provider.calls == []


async def test_a_started_consultation_notice_does_not_take_over(portal, provider):  # noqa: F811
    """Review L2: the consultation (and its notice's version) already started."""
    world, rid = await quiet_world(portal)
    started = datetime.now(UTC) - timedelta(minutes=10)
    async with portal() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = started
        appointment.end_at = started + timedelta(minutes=30)
        (await session.get(AppointmentReminder, rid)).appointment_start_at = started
        await session.commit()

    await _assert_no_takeover(portal, world, rid)
    assert provider.calls == []


async def test_a_suspended_consultation_notice_does_not_take_over(portal, provider):  # noqa: F811
    """Review L2: the consultation waits for the Pix of a change's difference (D-R4)."""
    world, rid = await quiet_world(portal)
    async with portal() as session:
        deposit_id = await session.scalar(
            select(PixDeposit.id).where(PixDeposit.appointment_id == world.appointment.id)
        )
        session.add(
            DepositAdjustment(
                tenant_id=world.tenant.id,
                deposit_id=deposit_id,
                appointment_id=world.appointment.id,
                kind=KIND_INCREASE,
                mode=MODE_SUSPEND,
                status=STATUS_AWAITING_PAYMENT,
                old_service="Consulta",
                new_service="Exame",
                diff_cents=3000,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        await session.commit()

    await _assert_no_takeover(portal, world, rid)
    assert provider.calls == []


async def test_a_notice_of_a_moved_consultation_does_not_take_over(portal, provider):  # noqa: F811
    """Review L2: the consultation's start changed and the notice was not (yet) invalidated -
    it belongs to an older version."""
    world, rid = await quiet_world(portal)
    moved = world.start_at + timedelta(days=1)
    async with portal() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = moved
        appointment.end_at = moved + timedelta(minutes=30)
        await session.commit()

    await _assert_no_takeover(portal, world, rid)
    assert provider.calls == []


async def test_a_clinic_without_the_sinal_is_not_looked_up(portal, provider, monkeypatch):  # noqa: F811
    """Review L3: the clinic switched the sinal off with a notice still pending - the ordinary
    entry keeps today's opening and never even looks for notices."""
    world, rid = await quiet_world(portal)
    async with portal() as session:
        (await session.get(Tenant, world.tenant.id)).pix_deposit_enabled = False
        await session.commit()

    async def _never(*args, **kwargs):
        raise AssertionError("pending notices looked up for a clinic without the sinal")

    monkeypatch.setattr(open_mod, "_pending_price_notices", _never)

    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith("LEMBRE-SE:")
    assert await answered_at(portal, rid) is None


async def test_an_answered_notice_leaves_the_ordinary_opening(portal, provider):  # noqa: F811
    world, rid = await quiet_world(portal)
    async with portal() as session:
        row = await session.get(AppointmentReminder, rid)
        row.answered_at = datetime.now(UTC) - timedelta(hours=3)
        row.answer = "opened"
        await session.commit()

    await enter_plain(world)

    [body] = await bodies(portal, world)
    assert body.startswith("LEMBRE-SE:")
    assert provider.calls == []


async def test_an_invalidated_notice_leaves_the_ordinary_opening(portal, provider):  # noqa: F811
    """A notice of an older version (rescheduled: `invalidated_at`) is not pending."""
    world, rid = await quiet_world(portal)
    async with portal() as session:
        row = await session.get(AppointmentReminder, rid)
        row.invalidated_at = datetime.now(UTC)
        await session.commit()

    decision = await open_mod._brain_message_entry_decision(
        world.tenant.id, world.patient.external_id
    )

    assert decision is not None and decision.price_change_reminder_id is None
    assert decision.ledger_key.startswith("brain_message_enter:")


async def quiet_clinic_b(portal):  # noqa: F811
    world_b = await seed_world(
        portal,
        v2=True,
        channel=CHANNEL_BRAIN_MESSAGE,
        phone_number_id=None,
        start_at=datetime.now(UTC) + timedelta(days=4),
        last_inbound_at=datetime.now(UTC) - timedelta(days=1),
    )
    await consent(portal, world_b)
    return world_b


async def test_another_clinics_notice_never_takes_over_the_entry(portal, provider):  # noqa: F811
    """Clinic A has a pending notice for the same Portal account; entering clinic B gets B's own
    opening, and A's notice stays unanswered."""
    world_a, rid_a = await quiet_world(portal)
    world_b = await quiet_clinic_b(portal)
    async with portal() as session:
        patient_b = await session.get(Patient, world_b.patient.id)
        patient_b.external_id = world_a.patient.external_id
        await session.commit()

    await process_brain_message_enter(
        {"redis": FakeRedis()}, str(world_b.tenant.id), world_a.patient.external_id
    )

    [body] = await bodies(portal, world_b)
    assert body.startswith("LEMBRE-SE:")
    assert await bodies(portal, world_a) == []
    assert await answered_at(portal, rid_a) is None
    assert provider.calls == []


async def test_a_row_of_another_clinics_consultation_is_ignored(portal, provider):  # noqa: F811
    """A clinic-B `price_change` row pointing at clinic A's consultation never hijacks B's entry
    (nor touches A's money)."""
    world_a, _deposit = await seed(portal)
    await reprice(portal, world_a, "R$ 300,00")
    world_b = await quiet_clinic_b(portal)
    async with portal() as session:
        session.add(
            AppointmentReminder(
                tenant_id=world_b.tenant.id,
                appointment_id=world_a.appointment.id,
                patient_id=world_b.patient.id,
                kind=REMINDER_KIND_PRICE_CHANGE,
                appointment_start_at=world_a.start_at,
                due_at=datetime.now(UTC),
                status=REMINDER_STATUS_SENT,
                channel=REMINDER_CHANNEL_EMAIL,
                with_prompt=False,
                sent_at=datetime.now(UTC),
            )
        )
        await session.commit()

    await process_brain_message_enter(
        {"redis": FakeRedis()}, str(world_b.tenant.id), world_b.patient.external_id
    )

    [body] = await bodies(portal, world_b)
    assert body.startswith("LEMBRE-SE:")
    assert provider.calls == []


async def test_a_human_owned_conversation_stays_silent_with_a_pending_notice(
    portal,  # noqa: F811
    provider,  # noqa: F811
):
    world, rid = await quiet_world(portal)
    await set_conversation(portal, world, handover_state=HandoverState.HUMAN_ACTIVE)

    await enter_plain(world)

    assert await bodies(portal, world) == [] and provider.calls == []
    assert await answered_at(portal, rid) is None


async def test_consent_pending_stays_silent_with_a_pending_notice(portal, provider):  # noqa: F811
    world, rid = await quiet_world(portal)
    async with portal() as session:
        patient = await session.get(Patient, world.patient.id)
        patient.lgpd_accepted_at = None
        await session.commit()

    await enter_plain(world)

    assert await bodies(portal, world) == [] and provider.calls == []
    assert await answered_at(portal, rid) is None
