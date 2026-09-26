"""Booking for someone else: "Essa consulta é pra você?" (both channels).

Until this module the product assumed that whoever is typing is whoever will be
attended. A son booking for his mother booked under his own name: the Google
event, the confirmation, the doctor's e-mail and the PreCheck hand-off all said
"the son". The owner decided (2026-09-23, docs/JORNADA_PACIENTE_WHATSAPP_E_PORTAL.md
§2) that every booking starts by asking who it is for.

## Where the question sits

    Agendar (or any other booking entry) -> PRA QUEM? -> [name -> authorization]
        -> professional / service -> convênio -> day -> slot -> confirm

First, before anything else, because every later question (convênio,
professional, service) is about the person who will be attended. "✅ Sim, é pra
mim" continues exactly as before; nothing else changes on that path.
"👤 Pra outra pessoa" asks the attendee's NAME, then shows the authorization
sentence with the name interpolated and requires an explicit "✅ Confirmar" -
the same explicit-accept shape the repo uses for every other sensitive consent
(`lgpd_accepted_at`). "⬅️ Cancelar" goes back to the pra-quem question. The
confirmation writes one `ConsentEvent(kind="third_party_booking_authorized")`
(`workers/tasks.py::_apply_flow_result`).

The steps themselves live in `services/flow_router.py` (STEP_AWAITING_ATTENDEE_*);
this module holds only the copy and the pure parser, like `patient_name.py`.

## Where the name goes, and why it is masked

`Conversation.flow_attendee_name` while the booking is in progress, then
`BookingHold.attendee_name` / `Appointment.attendee_name`. None of those is
`Patient.name`, so `services/pii_pseudonymization.py::load_pseudonymizer`
registers all three as the ATENDIDO identifier - that is the one line that keeps
the name away from the LLM. The raw typed answer (and any REFUSED answer) is
additionally replaced in the model's copy of the history by
`ai/graph.py::_load_history`, keyed on `is_attendee_name_question` - the same
rule-6 treatment the patient's own name gets.

The copy never echoes PART of the name: the authorization sentence interpolates
the whole stored value, which is exactly what the identifier masks.

## Scope (MVP)

Only the NAME of the attendee. No phone, CPF or birth date: no screen has a
field for them and asking would widen the third-party PII captured without the
owner asking for it.
"""

from secretaria.core.whatsapp_limits import (
    EMOJI_AFFIRMATIVE,
    EMOJI_BACK,
    EMOJI_PERSON,
    decorate,
)
from secretaria.services.patient_name import parse_patient_name

# ---------------------------------------------------------------------------
# Copy (pure)
# ---------------------------------------------------------------------------
ATTENDEE_QUESTION_BODY = "Essa consulta é pra você? 😊"

# Semantic pair, FEAT_44 style (docs/CHECKPOINT_whatsapp_text_limits.md §3).
# The "outra pessoa" label is shorter than the owner-facing spec's
# "Não, é pra outra pessoa" (23 characters): WhatsApp cuts reply-button titles
# at 20 and the tap would echo the truncated text. A test pins both <= 20.
LABEL_ATTENDEE_SELF = decorate(EMOJI_AFFIRMATIVE, "Sim, é pra mim")
LABEL_ATTENDEE_OTHER = decorate(EMOJI_PERSON, "Pra outra pessoa")

ATTENDEE_NAME_REQUEST = (
    "Certo! Qual é o *nome completo* da pessoa que vai ser atendida?\n\n✍️ É só digitar aqui."
)
ATTENDEE_NAME_INVALID = (
    "Hmm, não consegui entender o nome. 😕\n\n"
    "✍️ Pode digitar só o nome da pessoa? Assim, por exemplo: Maria Silva"
)

# The owner's sentence, verbatim (journey doc §2), capitalised as the opening
# of a message.
_AUTHORIZATION_TEMPLATE = (
    "Ao informar os dados de *{name}*, você confirma que tem autorização para "
    "compartilhar essas informações com a clínica."
)
LABEL_ATTENDEE_AUTH_CONFIRM = decorate(EMOJI_AFFIRMATIVE, "Confirmar")
LABEL_ATTENDEE_AUTH_BACK = decorate(EMOJI_BACK, "Cancelar")

CONSENT_KIND_THIRD_PARTY_BOOKING = "third_party_booking_authorized"
CONSENT_LEGAL_BASIS_THIRD_PARTY_BOOKING = (
    "TODO_LAWYER: autorização declarada pelo titular da conta para compartilhar "
    "dados de terceiro (nome do atendido) com a clínica"
)

# What the LLM reads in place of any answer to the attendee-name question.
ATTENDEE_NAME_LLM_PLACEHOLDER = "[resposta à pergunta do nome do atendido — omitida]"


def authorization_body(name: str) -> str:
    """The authorization sentence with the WHOLE stored name interpolated."""
    return _AUTHORIZATION_TEMPLATE.format(name=name)


def parse_attendee_name(body: str | None) -> str | None:
    """The attendee's name from a typed answer, or None when it is not a name.

    Same parser as the patient's own name step, with this step's own button
    labels refused as names (a stale tap on the pra-quem card must not become
    "Pra Outra Pessoa").
    """
    return parse_patient_name(
        body,
        not_names=(
            LABEL_ATTENDEE_SELF,
            LABEL_ATTENDEE_OTHER,
            LABEL_ATTENDEE_AUTH_CONFIRM,
            LABEL_ATTENDEE_AUTH_BACK,
        ),
    )


def is_attendee_name_question(bot_body: str | None) -> bool:
    """Whether an outbound row asked for the attendee's name (prefix match).

    Prefix, like `patient_name.is_name_question`: a Brain-Message card is
    stored flattened with its options appended.
    """
    return (bot_body or "").startswith((ATTENDEE_NAME_REQUEST, ATTENDEE_NAME_INVALID))
