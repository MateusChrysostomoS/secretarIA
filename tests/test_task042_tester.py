# ruff: noqa: F811, F401 - fixtures/helpers are imported from the sibling test module
"""TASK-042 independent tester (secretarIA side).

(j) the known-account code wait before consent: a message that is not a 6-digit code repeats
    the two-button card, keeps the state, never shows the LGPD notice, and the right code
    still verifies; the POST-consent wait keeps its old behaviour (abandons to IDLE).
(+) the retention discard: what makes a visit "not empty", and that a refusal changes nothing.

Reuses the harness of `tests/test_patient_name_step.py` (in-memory SQLite, fake brain-api calls).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.models import (
    ConsentEvent,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
)
from secretaria.models.appointment import Appointment
from secretaria.models.booking_hold import BookingHold
from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, interactive_history_body
from secretaria.services.pending_identity import (
    CODE_ACCEPTED_MESSAGE,
    EXISTING_ACCOUNT_CODE_BUTTONS,
    ClaimOutcome,
    ClaimResult,
    IdentityState,
    existing_account_code_body,
)
from secretaria.services.visit_merge import discard_empty_visit
from secretaria.workers import tasks
from secretaria.workers.portal import identity_gate
from tests.test_patient_name_step import (  # noqa: F401 - fixtures + harness
    EMAIL,
    EMAIL_MASKED,
    EXTERNAL_ID,
    LGPD_ROW,
    _age_conversation,
    _bm_turn,
    _conversation,
    _outbound,
    _patient,
    _seed_tenant,
    _wire,
    calls,
    db,
)
from tests.test_visit_merge import (  # noqa: F401 - fixtures
    KEY,
    VISIT,
    _count,
    _portal_patient,
    _tenant,
    api,
)

CARD = interactive_history_body(
    existing_account_code_body(None), [title for _, title in EXISTING_ACCOUNT_CODE_BUTTONS]
)
URL = "/internal/brain-message/visits/discard"


async def _in_known_account_code_wait(db, calls):
    """greeting -> known e-mail -> AWAITING_EMAIL_CODE, before any consent."""
    calls.claim_result = ClaimResult(
        ClaimOutcome.CLAIMED, account_exists=True, email_masked=EMAIL_MASKED
    )
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_EMAIL_CODE
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).lgpd_accepted_at is None
    return tenant


# --- (j) pre-consent known-account wait --------------------------------------------------------


@pytest.mark.parametrize(
    "stray",
    ["oi", "não chegou nada", "12345", "1234567", "abc123", "quero marcar consulta", "???"],
)
async def test_j_any_non_code_message_repeats_the_card_keeps_the_wait_and_never_shows_lgpd(
    db, calls, stray
) -> None:
    tenant = await _in_known_account_code_wait(db, calls)
    before = len(await _outbound(db, tenant))

    await _bm_turn(tenant, stray)

    new = (await _outbound(db, tenant))[before:]
    assert new == [CARD]
    assert LGPD_ROW not in await _outbound(db, tenant)
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_EMAIL_CODE
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).lgpd_accepted_at is None
    assert calls.verified == [], "a non-code is never sent to brain-api as a code"
    assert calls.code_requests == [EXTERNAL_ID], "no extra e-mail is sent for a stray message"

    calls.probe_states = [IdentityState.VERIFIED]
    await _bm_turn(tenant, "123456")
    assert calls.verified == ["123456"]
    assert CODE_ACCEPTED_MESSAGE in await _outbound(db, tenant)


async def test_j_several_strays_in_a_row_each_get_one_card_and_the_code_still_works(
    db, calls
) -> None:
    tenant = await _in_known_account_code_wait(db, calls)
    before = len(await _outbound(db, tenant))

    for text in ("oi", "alô?", "ainda aqui"):
        await _bm_turn(tenant, text)

    assert (await _outbound(db, tenant))[before:] == [CARD, CARD, CARD]
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_EMAIL_CODE
    calls.probe_states = [IdentityState.VERIFIED]
    await _bm_turn(tenant, "123456")
    assert CODE_ACCEPTED_MESSAGE in await _outbound(db, tenant)


async def test_j_a_wrong_six_digit_code_never_shows_the_lgpd_notice_either(db, calls) -> None:
    """Not a stray (it IS a code), but the same barrier: no consent notice for a known account."""
    tenant = await _in_known_account_code_wait(db, calls)

    await _bm_turn(tenant, "000000")

    assert calls.verified == ["000000"]
    assert LGPD_ROW not in await _outbound(db, tenant)
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).lgpd_accepted_at is None


async def test_j_the_wait_still_has_a_clock_exit(db, calls) -> None:
    """The card is repeated, but silence still ends the wait (it must never be a trap)."""
    tenant = await _in_known_account_code_wait(db, calls)
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)

    await _bm_turn(tenant, "oi")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state != FlowState.AWAITING_EMAIL_CODE
    assert (await _outbound(db, tenant))[-1] != CARD


async def test_j_the_log_carries_only_kind_and_length(db, calls, monkeypatch) -> None:
    # A spy on the module's logger, not capsys: setup_logging() caches each logger
    # with the stdout live on first use, so an earlier app-importing test hid the line.
    tenant = await _in_known_account_code_wait(db, calls)
    logs: list[tuple[str, dict]] = []

    class _Spy:
        def __getattr__(self, _level):
            return lambda event, **fields: logs.append((event, fields))

    monkeypatch.setattr(identity_gate, "logger", _Spy())

    await _bm_turn(tenant, "meu cpf 123.456.789-00")

    [fields] = [f for e, f in logs if e == "conversation_pending_code_unrecognized"]
    assert "kind" in fields and "length" in fields
    rendered = repr(logs)
    for leaked in ("cpf", "123.456", "789-00"):
        assert leaked not in rendered, "the text of the message reached the log"


async def test_j_the_post_consent_code_wait_keeps_its_old_behaviour_and_abandons(db, calls) -> None:
    """The wait asked AFTER consent (post-booking offer) is NOT the one that got the card.

    A non-code without a hold abandons it: back to IDLE, no card repeated, no new e-mail.
    """
    tenant = await _in_known_account_code_wait(db, calls)
    async with db() as session:
        async with session.begin():
            patient = await session.scalar(
                select(Patient).where(
                    Patient.tenant_id == tenant.id, Patient.channel == CHANNEL_BRAIN_MESSAGE
                )
            )
            patient.lgpd_accepted_at = datetime.now(UTC) - timedelta(days=1)
    before = len(await _outbound(db, tenant))

    await _bm_turn(tenant, "oi")

    new = (await _outbound(db, tenant))[before:]
    assert CARD not in new, "the pre-consent card must not leak into the post-consent wait"
    assert LGPD_ROW not in new
    assert (await _conversation(db, tenant)).flow_state != FlowState.AWAITING_EMAIL_CODE
    assert calls.code_requests == [EXTERNAL_ID]


async def test_j_a_live_hold_before_consent_keeps_the_booking_reprompt_not_the_new_card(
    db, calls
) -> None:
    """A hold is the booking gate's business: its own reprompt, not the known-account card."""
    tenant = await _in_known_account_code_wait(db, calls)
    conversation = await _conversation(db, tenant)
    patient = await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)
    start = datetime.now(UTC) + timedelta(days=1)
    async with db() as session:
        session.add(
            BookingHold(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                patient_id=patient.id,
                start_at=start,
                end_at=start + timedelta(hours=1),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()
    before = len(await _outbound(db, tenant))

    await _bm_turn(tenant, "oi")

    new = (await _outbound(db, tenant))[before:]
    assert CARD not in new
    assert LGPD_ROW not in new
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_EMAIL_CODE


# --- retention discard: edge cases ------------------------------------------------------------


async def test_a_single_empty_bodied_inbound_still_counts_as_the_patient_writing(db) -> None:
    """A button tap is stored as an INBOUND row, even with no text: it is the patient acting."""
    db_ = db
    tenant = await _tenant(db_)
    _, conversation = await _portal_patient(db_, tenant, VISIT, messages=1)
    async with db_() as session:
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="",
            )
        )
        await session.commit()
    async with db_() as session:
        assert (await discard_empty_visit(session, tenant.id, VISIT)).status == "not_empty"
    assert await _count(db_, Patient) == 1


async def test_a_refusal_changes_nothing_at_all_not_even_the_token_map(db) -> None:
    db_ = db
    tenant = await _tenant(db_)
    patient, conversation = await _portal_patient(db_, tenant, VISIT, messages=2)
    async with db_() as session:
        session.add(ConversationPiiTokenMap(conversation_id=conversation.id, tokens={"x": "y"}))
        session.add(
            Appointment(tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-9")
        )
        await session.commit()

    async with db_() as session:
        assert (await discard_empty_visit(session, tenant.id, VISIT)).status == "not_empty"
        await session.commit()

    assert await _count(db_, Patient) == 1
    assert await _count(db_, Conversation) == 1
    assert await _count(db_, Message) == 2
    assert await _count(db_, ConversationPiiTokenMap) == 1
    assert await _count(db_, Appointment) == 1
    assert await _count(db_, ConsentEvent) == 1


async def test_another_patients_message_or_hold_does_not_block_this_visit(db) -> None:
    db_ = db
    tenant = await _tenant(db_)
    await _portal_patient(db_, tenant, VISIT, messages=1)
    _, other_conv = await _portal_patient(db_, tenant, "visit-other", messages=1)
    now = datetime.now(UTC)
    async with db_() as session:
        session.add(
            Message(
                conversation_id=other_conv.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
            )
        )
        await session.commit()

    async with db_() as session:
        result = await discard_empty_visit(session, tenant.id, VISIT)
        await session.commit()

    assert result.status == "discarded"
    assert await _count(db_, Patient) == 1, "only the empty visit went"
    _ = now


async def test_the_route_409_leaves_the_visit_and_a_second_call_still_409s(api, db) -> None:
    db_ = db
    tenant = await _tenant(db_)
    _, conversation = await _portal_patient(db_, tenant, VISIT, messages=1)
    async with db_() as session:
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
            )
        )
        await session.commit()
    body = {"tenant_id": str(tenant.id), "external_id": VISIT}

    first = await api.client.post(URL, headers=KEY, json=body)
    second = await api.client.post(URL, headers=KEY, json=body)

    assert first.status_code == second.status_code == 409
    assert await _count(db_, Patient) == 1
    assert await _count(db_, Message) == 2
