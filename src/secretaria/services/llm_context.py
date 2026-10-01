"""Turn-scoped "ESTADO DA CONVERSA" block for the agent's system prompt.

The LLM used to arrive at a turn blind: a tap on "Outro"/"Não sei" reaches it as
the bare text, with no hint of which list the patient was on, which doctor or
service was already chosen, or what each doctor offers. This renders that state
as plain Portuguese for `ai/prompts.py::_format_conversation_state`.

PII rule: NO patient or attendee name goes in here. Only the message history is
pseudonymized before it leaves the process; this block rides in the SYSTEM
prompt, which is not. A booking for someone else is reported as a boolean.
"""

from __future__ import annotations

from secretaria.models import FlowState
from secretaria.services import flow_router as fr

_STEP_LABELS: dict[str, str] = {
    fr.STEP_AWAITING_PROFESSIONAL: "a lista de médicos",
    fr.STEP_PROFESSIONAL_HELP: "a ajuda para escolher o médico",
    fr.STEP_PROFESSIONAL_HELP_FINAL: "a ajuda para escolher o médico",
    fr.STEP_AWAITING_SERVICE: "a lista de serviços",
    fr.STEP_SERVICE_HELP: "a ajuda para escolher o serviço",
    fr.STEP_SERVICE_HELP_FINAL: "a ajuda para escolher o serviço",
    fr.STEP_AWAITING_SERVICE_CONFIRM: "o detalhe do serviço escolhido",
    fr.STEP_AWAITING_INSURANCE: "a pergunta de convênio",
    fr.STEP_AWAITING_DAY: "a escolha do dia",
    fr.STEP_AWAITING_DAY_ESCAPE: "a escolha do dia",
    fr.STEP_AWAITING_DAY_RETRY: "a escolha do dia",
    fr.STEP_AWAITING_SLOT: "a escolha do horário",
    fr.STEP_AWAITING_CONFIRMATION: "a confirmação do agendamento",
    fr.STEP_AWAITING_RETRY: "a escolha de outro horário",
}


def _where(state: FlowState | None, step: str | None) -> str:
    if state == FlowState.LLM:
        return "conversa livre já em andamento (veja o histórico)"
    if step in _STEP_LABELS:
        return (
            f"{_STEP_LABELS[step]} do agendamento — e escolheu 'Outro', 'Não sei' "
            "ou escreveu por conta própria"
        )
    return "no menu inicial — tocou em 'Outro' ou escreveu livremente"


def build_conversation_state(conversation, tenant, professionals) -> str | None:
    """The state block, or None when there is no conversation snapshot."""
    if conversation is None:
        return None
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
    if getattr(conversation, "flow_selected_type", None):
        lines.append(f"- Serviço já escolhido: {conversation.flow_selected_type}")
    plans = fr.insurance_plan_names(tenant) if tenant is not None else []
    selected_insurance = getattr(conversation, "flow_selected_insurance", None)
    if selected_insurance:
        # Even catalog names can be verbatim legacy PII; SYSTEM is not scrubbed.
        lines.append("- Convênio já informado (valor omitido).")
    if getattr(conversation, "flow_selected_day", None):
        lines.append(f"- Dia já escolhido: {conversation.flow_selected_day}")
    if getattr(conversation, "flow_attendee_name", None):
        lines.append("- A consulta é para OUTRA pessoa (o nome dela é omitido de propósito).")
    if plans:
        lines.append(f"- Convênios cadastrados na clínica: {len(plans)} (valores omitidos).")
    if len(professionals or []) > 1:
        lines.append("- Serviços de cada médico:")
        for p in professionals:
            names = [
                str(t.get("name"))
                for t in fr.professional_appointment_types(p, tenant)
                if t.get("name")
            ]
            services = ", ".join(names) if names else 'sem serviços cadastrados'
            lines.append(f"  - {p.name}: {services}")
    return "\n".join(lines)
