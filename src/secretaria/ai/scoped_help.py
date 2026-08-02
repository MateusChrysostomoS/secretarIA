"""Scoped LLM helper nodes for the deterministic flow's "Não sei" rows.

Two narrow, bounded LLM interactions — one per catalog — invoked by
services/flow_router.py when the patient taps "Não sei" on the professional
list (STEP_PROFESSIONAL_HELP) or on the service list (STEP_SERVICE_HELP):

- `run_professional_help`: "which professional fits what I described?"
- `run_service_help`: "which service fits what I need?"

They are deliberately NOT the full secretary agent (ai/graph.py) and NOT the
open-ended "Outro" hand-off (FlowState.LLM): each call is a single structured
decision — no ReAct loop, no calendar/booking tools — whose system prompt is
grounded on the tenant's REAL catalog (the same professionals/services
snapshot the flow router is holding), so it can never offer an option that
does not exist. The decision comes back as a forced tool call
(`with_structured_output`), never free text the router would have to
re-interpret:

    pick     -> `choice` names one option, verbatim from the list; the flow
                router re-validates it against its own snapshot
                (_match_professional/_match_service) and re-enters the
                deterministic flow at that option.
    clarify  -> `question` is ONE short follow-up; the router sends it and
                bumps the step to its *_FINAL variant. Exactly one clarify is
                allowed — the bound lives in the router (final_round), not in
                model goodwill.
    escalate -> the router sends a fixed "vou te conectar com a equipe"
                message and flips the conversation to human handover.

History: like run_agent, each call is stateless — the last few Message rows
are re-read from the DB so the second round sees the first round's answer and
clarifying question. `conversation_id=None` (router unit tests, dev snapshots
without an id) degrades to just the current patient message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Message, MessageSender

logger = get_logger(__name__)

# The nodes only ever need the tail of the conversation (opener + the 1-2
# answers of this help exchange, plus a little surrounding context) — far less
# than the agent's HISTORY_LIMIT of 30.
_HELP_HISTORY_LIMIT = 10


@dataclass
class ScopedHelpOutcome:
    """The node's decision, already normalized for the flow router.

    kind="pick"     -> `choice` is the option's name as the model wrote it;
                       the router still re-validates it against the real list.
    kind="clarify"  -> `question` is the single follow-up to send.
    kind="escalate" -> hand the conversation to a human.
    """

    kind: Literal["pick", "clarify", "escalate"]
    choice: str | None = None
    question: str | None = None


class _ScopedHelpDecision(BaseModel):
    """Forced-tool-call schema for one scoped-help turn (see module docstring)."""

    action: Literal["pick", "clarify", "escalate"] = Field(
        description=(
            "pick: uma opção da lista resolve o caso; clarify: falta UMA "
            "informação para decidir; escalate: fora do escopo ou impossível "
            "decidir com as opções existentes."
        )
    )
    choice: str | None = Field(
        default=None,
        description="Para action=pick: o nome EXATO da opção escolhida, copiado da lista.",
    )
    question: str | None = Field(
        default=None,
        description="Para action=clarify: UMA pergunta curta e específica ao paciente.",
    )


_decision_model: Any | None = None


def _get_decision_model() -> Any:
    """Build (once) the structured-output model both nodes share.

    Same knobs as ai/graph.py's build_agent — model name, token cap, SDK
    retries — but no tool loop: `with_structured_output` forces a single
    function call shaped like `_ScopedHelpDecision`.
    """
    global _decision_model
    if _decision_model is not None:
        return _decision_model
    s = get_settings()
    if not s.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment.")
    model = ChatOpenAI(
        model=s.OPENAI_SECRETARIA_MODEL,
        api_key=s.OPENAI_API_KEY,
        max_completion_tokens=s.OPENAI_MAX_TOKENS,
        max_retries=5,
        # Single decision call — half the agent loop's budget is plenty, and a
        # stuck socket must not pin the arq job for a whole minute.
        timeout=30,
    )
    _decision_model = model.with_structured_output(
        _ScopedHelpDecision, method="function_calling"
    )
    return _decision_model


async def _recent_history(conversation_id: UUID) -> list[BaseMessage]:
    """Last few conversation turns, chronological (graph._load_history's shape)."""
    async with async_session_factory() as session:
        rows = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(_HELP_HISTORY_LIMIT)
        )
        recent = list(rows)
    recent.reverse()
    out: list[BaseMessage] = []
    for m in recent:
        content = m.body or ""
        if not content:
            continue
        if m.sender == MessageSender.PATIENT:
            out.append(HumanMessage(content=content))
        else:
            out.append(AIMessage(content=content))
    return out


_SCOPED_RULES = (
    "Regras:\n"
    "- Você SÓ pode indicar opções da lista acima. Nunca invente, sugira ou "
    "mencione qualquer opção fora dela.\n"
    "- Se a descrição do paciente já permite escolher com confiança, use "
    "action=pick com o nome EXATO de uma opção da lista.\n"
    "- Se falta UMA informação para decidir, use action=clarify com uma "
    "pergunta curta e específica.\n"
    "- Se o pedido não se encaixa em nenhuma opção da lista, ou foge desse "
    "escopo (preços de outras coisas, endereço, assuntos gerais), use "
    "action=escalate.\n"
)
_FINAL_ROUND_RULE = (
    "- Esta é a ÚLTIMA troca: não faça mais perguntas. Use action=pick se "
    "possível, senão action=escalate.\n"
)


def _professional_options_block(professionals: list) -> str:
    lines = []
    for p in professionals:
        parts = [str(p.name)]
        specialty = getattr(p, "specialty", None)
        if specialty:
            parts.append(str(specialty))
        # `about` is the hub-editable patient-facing bio — the same matching
        # signal the multi_professional plugin's list_professionals exposes.
        # context_doctor_message stays persona-only, never used here.
        about = getattr(p, "about", None)
        if about:
            parts.append(str(about))
        lines.append("- " + " — ".join(parts))
    return "\n".join(lines)


def _service_options_block(services: list[dict]) -> str:
    lines = []
    for service in services:
        parts = [str(service.get("name", ""))]
        price = service.get("price")
        if price:
            parts.append(str(price))
        description = service.get("description") or service.get("long_description")
        if description:
            parts.append(str(description))
        lines.append("- " + " — ".join(parts))
    return "\n".join(lines)


def _professional_help_prompt(professionals: list, final_round: bool) -> str:
    return (
        "Você é a secretária de agendamento de uma clínica. Sua ÚNICA tarefa "
        "agora é ajudar o paciente a escolher o profissional certo da lista "
        "abaixo, a partir do que ele descreveu (sintoma, motivo da consulta, "
        "necessidade).\n\n"
        "Profissionais disponíveis (os ÚNICOS que existem):\n"
        f"{_professional_options_block(professionals)}\n\n"
        f"{_SCOPED_RULES}{_FINAL_ROUND_RULE if final_round else ''}"
    )


def _service_help_prompt(services: list[dict], final_round: bool) -> str:
    return (
        "Você é a secretária de agendamento de uma clínica. Sua ÚNICA tarefa "
        "agora é ajudar o paciente a escolher o serviço certo da lista abaixo, "
        "a partir do que ele descreveu que precisa.\n\n"
        "Serviços disponíveis (os ÚNICOS que existem):\n"
        f"{_service_options_block(services)}\n\n"
        f"{_SCOPED_RULES}{_FINAL_ROUND_RULE if final_round else ''}"
    )


def _normalize(decision: _ScopedHelpDecision, final_round: bool) -> ScopedHelpOutcome:
    """Map the raw decision onto the outcome the router acts on.

    Malformed combinations (pick without a choice, clarify without a question)
    and a clarify on the final round all collapse to escalate — the bound is
    enforced HERE, not left to the prompt.
    """
    if decision.action == "pick" and (decision.choice or "").strip():
        return ScopedHelpOutcome(kind="pick", choice=decision.choice.strip())
    if (
        decision.action == "clarify"
        and not final_round
        and (decision.question or "").strip()
    ):
        return ScopedHelpOutcome(kind="clarify", question=decision.question.strip())
    return ScopedHelpOutcome(kind="escalate")


async def _run(
    system_prompt: str,
    conversation_id: UUID | None,
    patient_message: str,
    final_round: bool,
) -> ScopedHelpOutcome:
    history: list[BaseMessage] = []
    if conversation_id is not None:
        history = await _recent_history(conversation_id)
    if not history:
        history = [HumanMessage(content=patient_message)]
    decision = await _get_decision_model().ainvoke(
        [SystemMessage(content=system_prompt), *history]
    )
    return _normalize(decision, final_round)


async def run_professional_help(
    *,
    conversation_id: UUID | None,
    professionals: list,
    patient_message: str,
    final_round: bool,
) -> ScopedHelpOutcome:
    """One scoped-help turn for "which professional fits my case?".

    `professionals` is the router's active-roster snapshot — the ONLY options
    the model is shown. Empty roster short-circuits to escalate without an
    LLM call.
    """
    if not professionals:
        return ScopedHelpOutcome(kind="escalate")
    return await _run(
        _professional_help_prompt(professionals, final_round),
        conversation_id,
        patient_message,
        final_round,
    )


async def run_service_help(
    *,
    conversation_id: UUID | None,
    services: list[dict],
    patient_message: str,
    final_round: bool,
) -> ScopedHelpOutcome:
    """One scoped-help turn for "which service fits what I need?".

    `services` is the already-scoped catalog (the selected professional's own
    when the multi-doctor branch is active, else the tenant's) — the ONLY
    options the model is shown. Empty catalog short-circuits to escalate.
    """
    if not services:
        return ScopedHelpOutcome(kind="escalate")
    return await _run(
        _service_help_prompt(services, final_round),
        conversation_id,
        patient_message,
        final_round,
    )
