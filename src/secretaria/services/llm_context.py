"""Turn-scoped "ESTADO DA CONVERSA" block for the agent's system prompt.

The LLM used to arrive at a turn blind: free text or a "Não sei" answer reaches it
with no hint of which list the patient was on, which doctor or
service was already chosen, or what each doctor offers. This renders that state
as plain Portuguese for `ai/prompts.py::_format_conversation_state`.

PII rule: NO patient or attendee name goes in here. Only the message history is
pseudonymized before it leaves the process; this block rides in the SYSTEM
prompt, which is not. A booking for someone else is reported as a fact, never by name,
and convênio values are omitted (even catalog names can be verbatim legacy PII).

TASK-030 P2 (spec §4.7): every workflow step has a label (pra-quem, manage, the decline
reason and the legacy steps used to read as "no menu inicial"); the internal entry markers
`flow_selected_type` holds during the pra-quem steps (`__attendee_next_*__`) never reach
the model; the pra-quem answer, the chosen time and the clinic's topology are stated.
"""

from __future__ import annotations

from datetime import datetime

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.services.attendee import ATTENDEE_SELF

_BOOKING = "do agendamento"
_MANAGE = "da consulta já marcada"

_STEP_LABELS: dict[str, str] = {
    # Pra quem - the first booking question.
    fr.STEP_AWAITING_ATTENDEE_CHOICE: f'a pergunta "Essa consulta é pra você?" {_BOOKING}',
    fr.STEP_AWAITING_ATTENDEE_NAME: f"o pedido do nome de quem vai ser atendido {_BOOKING}",
    fr.STEP_AWAITING_ATTENDEE_AUTH: (
        f"a frase de autorização para marcar para outra pessoa {_BOOKING}"
    ),
    # Médico e serviço.
    fr.STEP_AWAITING_PROFESSIONAL: f"a lista de médicos {_BOOKING}",
    fr.STEP_AWAITING_SERVICE_PROFESSIONAL: (
        f"a lista de médicos para o serviço escolhido {_BOOKING}"
    ),
    fr.STEP_PROFESSIONAL_HELP: f"a ajuda para escolher o médico {_BOOKING}",
    fr.STEP_PROFESSIONAL_HELP_FINAL: f"a ajuda para escolher o médico {_BOOKING}",
    fr.STEP_AWAITING_CATALOG_SERVICE: f"a lista de serviços da clínica {_BOOKING}",
    fr.STEP_AWAITING_SERVICE: f"a lista de serviços {_BOOKING}",
    fr.STEP_SERVICE_HELP: f"a ajuda para escolher o serviço {_BOOKING}",
    fr.STEP_SERVICE_HELP_FINAL: f"a ajuda para escolher o serviço {_BOOKING}",
    fr.STEP_AWAITING_SERVICE_CONFIRM: f"o detalhe do serviço escolhido {_BOOKING}",
    fr.STEP_AWAITING_INSURANCE: f"a pergunta de convênio {_BOOKING}",
    # Dia, horário, confirmação.
    fr.STEP_AWAITING_DAY: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_DAY_RETRY: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_DAY_ESCAPE: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_SLOT: f"a escolha do horário {_BOOKING}",
    fr.STEP_AWAITING_CONFIRMATION: f"a confirmação {_BOOKING}",
    fr.STEP_AWAITING_RETRY: f"a escolha de outro horário {_BOOKING}",
    # Remarcar / cancelar.
    fr.STEP_MANAGE_PICK: f"a escolha {_MANAGE} para remarcar ou cancelar",
    fr.STEP_MANAGE_PICK_RESCHEDULE: f"a escolha {_MANAGE} a remarcar",
    fr.STEP_MANAGE_PICK_CANCEL: f"a escolha {_MANAGE} a cancelar",
    fr.STEP_MANAGE_ACTION: f"a pergunta remarcar ou cancelar {_MANAGE}",
    fr.STEP_MANAGE_CANCEL_CONFIRM: f"a confirmação do cancelamento {_MANAGE}",
    fr.STEP_MANAGE_DAY: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_DAY_RETRY: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_DAY_ESCAPE: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_SLOT: f"a escolha do novo horário {_MANAGE}",
    fr.STEP_MANAGE_CONFIRM: f"a confirmação da remarcação {_MANAGE}",
    # Depois de o médico cancelar.
    fr.STEP_DECLINE_REASON: "a pergunta do motivo de não remarcar depois que o médico cancelou",
    # TASK-038. Only read if a turn ever reaches the model mid-offer; the open card
    # itself is answered by the flow (flow_router._handle_human_offer).
    fr.STEP_HUMAN_OFFER: "a pergunta se quer que chamemos um atendente humano",
    # R6: a question during an edit is one model turn on the same draft step.
    fr.STEP_EDIT_MENU: "o menu de alterações da consulta já marcada",
    fr.STEP_EDIT_MORE: "as outras opções de alteração da consulta já marcada",
    fr.STEP_EDIT_DAY: "a escolha do novo dia da consulta já marcada",
    fr.STEP_EDIT_DAY_RETRY: "a escolha do novo dia da consulta já marcada",
    fr.STEP_EDIT_DAY_ESCAPE: "a escolha do novo dia da consulta já marcada",
    fr.STEP_EDIT_SLOT: "a escolha do novo horário da consulta já marcada",
    fr.STEP_EDIT_TIME_TOO: "a pergunta se quer mudar o horário também",
    fr.STEP_EDIT_SERVICE: "a escolha do novo serviço da consulta já marcada",
    fr.STEP_EDIT_DOCTOR: "a escolha do novo médico da consulta já marcada",
    fr.STEP_EDIT_INSURANCE: "a escolha do convênio da consulta já marcada",
    fr.STEP_EDIT_INSURANCE_OTHER: "o pedido de outro convênio da consulta já marcada",
    fr.STEP_EDIT_ATT_CHOICE: "a pergunta para quem será a consulta já marcada",
    fr.STEP_EDIT_ATT_NAME: "o pedido do nome de quem vai ser atendido",
    fr.STEP_EDIT_ATT_AUTH: "a autorização para alterar o paciente da consulta já marcada",
    fr.STEP_EDIT_CONFIRM: "a confirmação das alterações da consulta já marcada",
}


def _where(state: FlowState | None, step: str | None) -> str:
    if state == FlowState.LLM:
        return "conversa livre já em andamento (veja o histórico)"
    label = _STEP_LABELS.get(step or "")
    if label:
        return f"{label} — e escolheu 'Outro', 'Não sei' ou escreveu por conta própria"
    # "Outro" never lands here any more: the flow answers the tap with OTHER_OPENER
    # and the model first runs in LLM mode (the branch above).
    return "no menu inicial — escreveu livremente"


def _is_internal_marker(value: object) -> bool:
    """`flow_selected_type` holds `__attendee_next_*__` during the pra-quem steps: an entry
    marker, never a service the patient chose - and never something the model may read."""
    text = str(value or "")
    return text.startswith("__") and text.endswith("__")


def _pra_quem_line(attendee: str | None) -> str:
    if attendee is None:
        # Stated so the model does not ask it itself: "Outro" used to open with
        # "Essa consulta é pra você?" because of this very line (owner, 2026-10-06).
        return (
            "- Pra quem é a consulta: ainda não respondido. Não pergunte isso você: "
            "o fluxo guiado pergunta na hora de marcar."
        )
    if attendee == ATTENDEE_SELF:
        return "- Pra quem é a consulta: para o próprio paciente."
    return "- Pra quem é a consulta: OUTRA pessoa (o nome dela é omitido de propósito)."


def _slot_text(slot: object) -> str | None:
    if not slot:
        return None
    try:
        return datetime.fromisoformat(str(slot)).strftime(fr.WHEN_FORMAT)
    except ValueError:
        return None


def _service_names(services: list[dict]) -> str:
    return ", ".join(str(s.get("name")) for s in services if s.get("name"))


def _doctor_label(professional) -> str:
    """ "Dr. Fulano (Cardiologia)" - the specialty lets the model tell, and say, when no
    doctor of the clinic fits what the patient described (owner, 2026-10-07)."""
    specialty = str(getattr(professional, "specialty", None) or "").strip()
    return f"{professional.name} ({specialty})" if specialty else str(professional.name)


def _topology_lines(tenant, professionals) -> list[str]:
    roster = list(professionals or [])
    if tenant is None or not hasattr(tenant, "appointment_types"):
        return []
    if len(roster) > 1:
        lines = ["- A clínica tem vários médicos. Especialidade e serviços de cada um:"]
        for professional in roster:
            names = _service_names(fr.professional_appointment_types(professional, tenant))
            lines.append(
                f"  - {_doctor_label(professional)}: {names or 'sem serviços cadastrados'}"
            )
        return lines
    if len(roster) == 1:
        names = _service_names(fr.professional_appointment_types(roster[0], tenant))
        return [
            f"- A clínica atende com um só médico: {_doctor_label(roster[0])}. "
            f"Serviços: {names or 'sem serviços cadastrados'}."
        ]
    names = _service_names(fr.active_appointment_types(tenant))
    return [f"- Serviços da clínica: {names}."] if names else []


def build_conversation_state(conversation, tenant, professionals) -> str | None:
    """The state block, or None when there is no conversation snapshot."""
    if conversation is None:
        return None
    raw_edit = getattr(conversation, "flow_edit_draft", None)
    current = {}
    if (
        getattr(conversation, "flow_state", None) == FlowState.EDIT_BOOKING
        and isinstance(raw_edit, dict)
    ):
        candidate = raw_edit.get("current")
        if isinstance(candidate, dict):
            current = candidate
    lines = [
        "- Onde o paciente estava: "
        + _where(
            getattr(conversation, "flow_state", None), getattr(conversation, "flow_step", None)
        )
    ]
    professional = fr._find_professional_by_id(
        professionals, getattr(conversation, "flow_selected_professional_id", None)
    )
    if professional is not None:
        lines.append(f"- Médico já escolhido: {professional.name}")
    selected_type = current.get("service") or getattr(conversation, "flow_selected_type", None)
    if selected_type and not _is_internal_marker(selected_type):
        lines.append(f"- Serviço já escolhido: {selected_type}")
    attendee = (
        current.get("attendee_name") or ATTENDEE_SELF
        if current
        else getattr(conversation, "flow_attendee_name", None)
    )
    lines.append(_pra_quem_line(attendee))
    if current.get("insurance") or getattr(conversation, "flow_selected_insurance", None):
        # Even catalog names can be verbatim legacy PII; SYSTEM is not scrubbed.
        lines.append("- Convênio já informado (valor omitido).")
    if getattr(conversation, "flow_selected_day", None):
        lines.append(f"- Dia já escolhido: {conversation.flow_selected_day}")
    when = _slot_text(current.get("start_at") or getattr(conversation, "flow_selected_slot", None))
    if when:
        lines.append(f"- Horário já escolhido: {when}")
    plans = fr.insurance_plan_names(tenant) if tenant is not None else []
    if plans:
        lines.append(f"- Convênios cadastrados na clínica: {len(plans)} (valores omitidos).")
    lines.extend(_topology_lines(tenant, professionals))
    return "\n".join(lines)
