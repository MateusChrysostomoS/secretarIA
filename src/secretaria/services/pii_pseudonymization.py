"""DB layer of the PII pseudonymization step: load, carry and persist the map.

The engine itself lives in the shared `pseudonymize-core` package (pure
computation - no DB, no I/O, no logging). What lives HERE is the part that
knows about this product: where the map is stored, what identifiers this
conversation already knows about, and which ContextVar carries the live
`Pseudonymizer` through a turn. The LangChain-facing half - masking a message
list, wrapping the agent's tools - is in `ai/pii.py`.

    inbound turn   ->  load_pseudonymizer(conversation_id)
    history        ->  scrub    -> [PACIENTE_a7f3] goes to OpenAI
    bot reply      ->  rehydrate-> "Joao da Silva" comes back before sending
    end of turn    ->  persist_pseudonymizer(...)  (map may have grown)

WHY THE PATIENT'S NAME IS REGISTERED WITH `add_identifier`, NOT `add_person_name`
--------------------------------------------------------------------------------
PreCheck uses `add_person_name`, which also registers each PART of the name so
"Joao" and "Joao da Silva" collapse onto one token. That is right for a report
about exactly one person. It is WRONG here: this conversation names other
people - the clinic's professionals - and a patient called "Ana Clara Souza"
would turn a history line about "Dra. Ana Paula" into "[PACIENTE_x] Paula",
which re-hydrates to "Ana Clara Souza Paula" on the way back to the patient.
Exact full-name matching gives up the weak win (a bare first name) to avoid
corrupting a doctor's name, which is the trade this pipeline wants.

The CLINIC name is deliberately NOT registered either: it is not personal data,
and `ai/prompts.py::secretary_system_prompt` renders it un-masked, so tokenizing
it in the history only would leave the model reading two different clinics.

BEST-EFFORT ON PURPOSE
----------------------
Neither function raises. A map that fails to load degrades to a fresh one
(still masks - only token stability is lost); a map that fails to save costs
the next turn its stable tokens. Both are strictly better than dropping a
patient's turn on the floor over a bookkeeping row - and note the failure mode
is safe in the direction that matters: nothing here can cause MORE PII to be
sent, only less token reuse.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from pseudonymize_core import Pseudonymizer
from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    BookingHold,
    Conversation,
    ConversationPiiTokenMap,
    Patient,
)

logger = get_logger(__name__)

# The live Pseudonymizer for the turn in flight. Read by the tool wrapper in
# `ai/pii.py`, which runs INSIDE the LangGraph ReAct loop - LangGraph copies the
# context for tool nodes, so a value SET before the copy is visible there, and
# mutations to the object itself (the map growing) are visible back here,
# because the copy shares the object and only rebinds the name.
_pseudonymizer_ctx: ContextVar[Pseudonymizer | None] = ContextVar("pseudonymizer_ctx", default=None)


def current_pseudonymizer() -> Pseudonymizer | None:
    """The turn's Pseudonymizer, or None outside a pseudonymized turn."""
    return _pseudonymizer_ctx.get()


# Token prefix for a third party's name (services/attendee.py).
ATTENDEE_KIND = "ATENDIDO"


async def load_pseudonymizer(conversation_id: UUID) -> Pseudonymizer:
    """Rebuild this conversation's Pseudonymizer, seeded with its stored map.

    Re-seeding is what makes tokens stable across turns: the phone a patient
    typed on Monday keeps its Monday token on Friday, so the model reads one
    person instead of two strangers.

    The patient's own full name is (re-)registered on every load rather than
    trusted to the stored map, because the name can be filled in AFTER the
    first turn (WhatsApp profile name, or the booking flow asking for it).
    Re-registering an already-known value is free - `add_identifier` reuses the
    token it already emitted for that exact string.
    """
    tokens: dict[str, str] = {}
    patient_name: str | None = None
    patient_phone: str | None = None
    attendee_names: list[str] = []
    try:
        async with async_session_factory() as session:
            row = await session.get(ConversationPiiTokenMap, conversation_id)
            if row is not None and isinstance(row.tokens, dict):
                tokens = dict(row.tokens)
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None and conversation.patient_id is not None:
                patient = await session.get(Patient, conversation.patient_id)
                if patient is not None:
                    patient_name = patient.name
                    patient_phone = patient.wa_id
            attendee_names = await _attendee_names(session, conversation)
    except Exception as exc:
        # Degrade to a fresh map rather than dropping the turn - see the
        # module docstring. Masking still happens; only reuse is lost.
        logger.warning(
            "pii_load_map_failed",
            conversation_id=str(conversation_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return Pseudonymizer(tokens=tokens)

    # A restored map only REUSES tokens; it does not re-create the masking
    # rules. Every attendee name this conversation ever captured is therefore
    # re-registered from the map itself (see `remember_attendee_name`), which
    # keeps it masked after a "Cancelar" or an abandoned booking left no row.
    attendee_names = sorted(
        set(attendee_names)
        | {value for token, value in tokens.items() if token.startswith(f"[{ATTENDEE_KIND}_")}
    )
    p = Pseudonymizer(tokens=tokens)
    p.add_identifier("PACIENTE", patient_name)
    # The WhatsApp id is already shaped like a BR phone, so PHONE_RE would
    # catch most spellings of it anyway; registering it pins ONE token to the
    # canonical form the DB holds instead of minting a new one per spelling.
    p.add_identifier("TELEFONE", patient_phone)
    # The name of a third party the patient booked for ("Essa consulta é pra
    # você?" -> "Pra outra pessoa", services/attendee.py). It lives in three
    # columns none of which is Patient.name, so it is registered here, next to
    # the patient's own name, and nowhere else.
    for name in attendee_names:
        p.add_identifier(ATTENDEE_KIND, name)
    return p


async def remember_attendee_name(conversation_id: UUID, name: str) -> None:
    """Pin a just-captured attendee name into this conversation's token map.

    Called the moment the name is captured (the authorization card), BEFORE
    any appointment row exists: the card and the recap put the name in the
    message history, and "Cancelar" or an abandoned booking clears the flow
    field without ever writing a row that `_attendee_names` could find.
    Best-effort like `persist_pseudonymizer`; never raises.
    """
    try:
        p = await load_pseudonymizer(conversation_id)
        p.add_identifier(ATTENDEE_KIND, name)
        await persist_pseudonymizer(conversation_id, p)
    except Exception as exc:  # pragma: no cover - both callees already swallow
        logger.warning("pii_remember_attendee_failed", error_type=type(exc).__name__)


async def _attendee_names(session, conversation: Conversation | None) -> list[str]:
    """Every third-party attendee name this conversation has ever carried.

    The in-progress one (`Conversation.flow_attendee_name`, cleared when the
    booking ends), a held one (`BookingHold`) and every past booking's
    (`Appointment`): the name stays in the message history - the authorization
    sentence, the confirmation - long after the flow field is cleared, so the
    appointment rows are what keep masking it on every later turn.
    """
    if conversation is None:
        return []
    names = {conversation.flow_attendee_name}
    for model in (Appointment, BookingHold):
        rows = await session.scalars(
            select(model.attendee_name).where(
                model.conversation_id == conversation.id,
                model.attendee_name.is_not(None),
            )
        )
        names.update(rows)
    return sorted(n for n in names if n)


async def persist_pseudonymizer(conversation_id: UUID, p: Pseudonymizer) -> None:
    """Upsert `p.tokens` for this conversation. Best-effort, never raises.

    Call AFTER everything has been masked: the map grows during `scrub` (a
    phone or e-mail found in free text becomes a new entry), so saving early
    would save an incomplete key.
    """
    if not p.tokens:
        # Nothing was ever tokenized for this conversation - do not create a
        # row (and do not blank an existing one, which cannot happen: the map
        # only ever grows within a turn, so an empty map means an empty load).
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.get(ConversationPiiTokenMap, conversation_id)
                if row is None:
                    session.add(
                        ConversationPiiTokenMap(
                            conversation_id=conversation_id, tokens=dict(p.tokens)
                        )
                    )
                else:
                    # New dict, not an in-place mutation: a plain JSON column
                    # does not track changes made inside the object it holds.
                    row.tokens = dict(p.tokens)
    except Exception as exc:
        logger.warning(
            "pii_persist_map_failed",
            conversation_id=str(conversation_id),
            error=str(exc),
            error_type=type(exc).__name__,
            token_count=len(p.tokens),
        )
