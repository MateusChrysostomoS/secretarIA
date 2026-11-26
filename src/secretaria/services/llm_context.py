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
from uuid import UUID

from secretaria.models import FlowState
from secretaria.services import flow_router as fr, flow_units
from secretaria.services.attendee import ATTENDEE_SELF
from secretaria.services.service_catalog import entry_service_id
from secretaria.services.tenant_config import ClinicFeatures, PatientIdentityStatus
from secretaria.services.unit_booking import weekday_list_pt

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
    fr.STEP_AWAITING_NO_PLAN_AT_UNIT: (
        f"o aviso de falta de convênio na unidade escolhida e as opções de seguir {_BOOKING}"
    ),
    # TASK-086 A5: the matrix leaves the service with no place for the patient's convênio.
    fr.STEP_AWAITING_PLAN_REFUSED: (
        f"o aviso de que o convênio não é atendido nesse serviço e as opções de seguir {_BOOKING}"
    ),
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
    fr.STEP_HUMAN_OFFER_INSURANCE: (
        "a pergunta se quer que chamemos alguém da equipe para explicar o pagamento do convênio"
    ),
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
    fr.STEP_EDIT_UNIT: "a escolha da unidade da consulta já marcada",
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


def _with_blocked_days(name: str, blocked: frozenset[str]) -> str:
    """ "Cirurgia (não às segundas e sextas)" - the days the service is NOT done on (TASK-087).

    The bare name when nothing is blocked, so a clinic that does not use the feature reads
    exactly what it read before. The days read like the patient's refusal sentence
    (`unit_booking.weekday_list_pt`: "às sextas e aos sábados").
    """
    days = weekday_list_pt(blocked)
    return f"{name} (não {days})" if days else name


def _professional_service_names(professional, tenant) -> str:
    """`_service_names` of one professional's services, each with its blocked days (no unit:
    the base list - a doctor without a bookable unit has no per-unit exception to read)."""
    entries = fr.professional_appointment_types(professional, tenant)
    return ", ".join(
        _with_blocked_days(
            str(entry.get("name")), flow_units.blocked_weekdays_of(professional, entry, None)
        )
        for entry in entries
        if entry.get("name")
    )


def _doctor_label(professional) -> str:
    """ "Dr. Fulano (Cardiologia)" - the specialty lets the model tell, and say, when no
    doctor of the clinic fits what the patient described (owner, 2026-10-07)."""
    specialty = str(getattr(professional, "specialty", None) or "").strip()
    return f"{professional.name} ({specialty})" if specialty else str(professional.name)


# First in the topology on purpose: `prompts_v2._bounded_state` cuts the block at a line
# boundary when it is too long, so what must survive comes before the (long) roster.
_UNIT_GUIDANCE = (
    "- Ofereça só as combinações médico × unidade × serviço listadas a seguir; se o paciente "
    "quiser outra unidade, diga em quais o serviço é feito com esse médico ou quais médicos "
    "fazem o serviço lá."
)
_NO_OPEN_UNIT_SERVICE = "sem serviço em unidade aberta no momento"


def _unit_label(unit) -> str:
    address = str(getattr(unit, "address", None) or "").strip()
    return f"{unit.name} ({address})" if address else str(unit.name)


def _units_line(roster) -> str | None:
    """ "- Unidades: Sede (Rua A, 1) | Centro (Av. B, 2)": every bookable unit of the roster,
    each address ONCE (the doctors' lines then carry only unit names), or None without any."""
    seen: dict = {}
    for professional in roster:
        for unit in flow_units.bookable_units(professional):
            seen.setdefault(unit.id, _unit_label(unit))
    return "- Unidades: " + " | ".join(seen.values()) if seen else None


def _services_by_unit(professional, tenant) -> str | None:
    """ "Sede: Consulta, Retorno | Centro: Retorno", or None.

    None when the professional has no bookable unit (the clinic's text stays exactly today's
    for them). Per unit only the services done THERE (`flow_units.units_for_service`: a service
    without `service_id` or without marks is done at every unit of the doctor); a unit where
    nothing is done is left out; a doctor whose services are all restricted to closed units has
    no service to offer now (never the plain service list, which would imply they are bookable).
    Built from the roster snapshot - no query.
    """
    units = flow_units.bookable_units(professional)
    if not units:
        return None
    entries = fr.professional_appointment_types(professional, tenant)
    per_unit: dict = {unit.id: [] for unit in units}
    for entry in entries:
        name = entry.get("name")
        if not name:
            continue
        for unit in flow_units.units_for_service(professional, entry_service_id(entry)):
            blocked = flow_units.blocked_weekdays_of(professional, entry, unit.id)
            per_unit[unit.id].append(_with_blocked_days(str(name), blocked))
    parts = [f"{unit.name}: {', '.join(per_unit[unit.id])}" for unit in units if per_unit[unit.id]]
    return " | ".join(parts) or _NO_OPEN_UNIT_SERVICE


def _topology_lines(tenant, professionals) -> list[str]:
    # The doctors LISTED are the ones a patient may book with; how many doctors the clinic has
    # is counted on every active one (TASK-090 Adendo 1, C1): a clinic with a doctor hidden
    # from patients is never described as "um só médico"; a flagged invitee not ready to book
    # does not count (TASK-091, `booking_scope.topology_roster`).
    roster = list(professionals or [])
    multi = len(fr.topology_roster(professionals)) > 1
    if tenant is None or not hasattr(tenant, "appointment_types"):
        return []
    units_line = _units_line(roster)
    preamble = [_UNIT_GUIDANCE, units_line] if units_line is not None else []
    if multi:
        lines = ["- A clínica tem vários médicos. Especialidade e serviços de cada um:"]
        for professional in roster:
            by_unit = _services_by_unit(professional, tenant)
            if by_unit is not None:
                lines.append(f"  - {_doctor_label(professional)}: {by_unit}")
                continue
            names = _professional_service_names(professional, tenant)
            lines.append(
                f"  - {_doctor_label(professional)}: {names or 'sem serviços cadastrados'}"
            )
        return preamble + lines
    if len(roster) == 1:
        by_unit = _services_by_unit(roster[0], tenant)
        if by_unit is not None:
            return preamble + [
                f"- A clínica atende com um só médico: {_doctor_label(roster[0])}. "
                f"Unidades e serviços: {by_unit}."
            ]
        names = _professional_service_names(roster[0], tenant)
        return [
            f"- A clínica atende com um só médico: {_doctor_label(roster[0])}. "
            f"Serviços: {names or 'sem serviços cadastrados'}."
        ]
    names = _service_names(fr.active_appointment_types(tenant))
    return [f"- Serviços da clínica: {names}."] if names else []


def _current_unit_name(current: dict, professionals) -> str | None:
    """The name of the edit draft's unit, from the roster snapshot (no query), or None."""
    unit_id = str(current.get("unit_id") or "")
    if not unit_id:
        return None
    owner_id = str(current.get("professional_id") or "")
    # Every active doctor: the consultation's own (possibly hidden) doctor names his unit.
    roster = fr.everyone_of(professionals)
    # The draft's doctor first (ids ride the JSON draft as strings), then anyone else.
    roster.sort(key=lambda professional: str(professional.id) != owner_id)
    for professional in roster:
        for unit in flow_units.offered_units(professional):
            if str(unit.id) == unit_id:
                return str(unit.name)
    return None


def _professional_name(professionals, raw_id) -> str | None:
    """The roster name of a professional id carried as a string (draft JSON), or None."""
    for professional in professionals or []:
        if str(professional.id) == str(raw_id or ""):
            return str(professional.name)
    return None


def _edit_pending_lines(stage: dict, current: dict, professionals) -> list[str]:
    """Follow-up final review: what "Alterar dados" holds but has NOT applied yet (a new doctor
    and/or service waiting for a unit or a service choice) and the unit an open list is for.
    Empty when nothing is staged - a clinic without units never stages any of it."""
    lines: list[str] = []
    parts: list[str] = []
    doctor = _professional_name(professionals, stage.get("pending_doctor"))
    if doctor:
        parts.append(f"novo médico {doctor}")
    service = stage.get("pending_service")
    if service and not _is_internal_marker(service):
        parts.append(f"novo serviço {service}")
    if parts:
        tail = ", falta escolher a unidade" if stage.get("unit_purpose") else ""
        lines.append(
            "- Escolha pendente na alteração (ainda não aplicada): "
            + " e ".join(parts)
            + tail
            + "."
        )
    if stage.get("at_unit"):
        owner = stage.get("pending_doctor") or current.get("professional_id")
        listed = _current_unit_name(
            {"unit_id": stage["at_unit"], "professional_id": owner}, professionals
        )
        if listed:
            lines.append(f"- Unidade da lista aberta: {listed}")
    return lines


def build_conversation_state(conversation, tenant, professionals) -> str | None:
    """The state block, or None when there is no conversation snapshot."""
    if conversation is None:
        return None
    raw_edit = getattr(conversation, "flow_edit_draft", None)
    current = {}
    stage = {}
    if (
        getattr(conversation, "flow_state", None) == FlowState.EDIT_BOOKING
        and isinstance(raw_edit, dict)
    ):
        candidate = raw_edit.get("current")
        if isinstance(candidate, dict):
            current = candidate
            staged = raw_edit.get("stage")
            stage = staged if isinstance(staged, dict) else {}
    lines = [
        "- Onde o paciente estava: "
        + _where(
            getattr(conversation, "flow_state", None), getattr(conversation, "flow_step", None)
        )
    ]
    # `_find_owner_by_id`: a hidden doctor is only ever on the snapshot while HIS existing
    # consultation is managed or edited (the turn drops him otherwise), and then he is named.
    professional = fr._find_owner_by_id(
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
    unit_name = _current_unit_name(current, professionals) if current else None
    if unit_name:
        lines.append(f"- Unidade atual da consulta: {unit_name}")
    if current:
        lines.extend(_edit_pending_lines(stage, current, professionals))
    elif getattr(conversation, "flow_selected_unit_id", None):
        # A booking in progress whose unit was already picked (follow-up final review).
        chosen = _current_unit_name(
            {
                "unit_id": conversation.flow_selected_unit_id,
                "professional_id": getattr(conversation, "flow_selected_professional_id", None),
            },
            professionals,
        )
        if chosen:
            lines.append(f"- Unidade já escolhida: {chosen}")
    plans = fr.insurance_plan_names(tenant) if tenant is not None else []
    if plans:
        lines.append(
            f"- Convênios cadastrados na clínica: {len(plans)} (nomes omitidos aqui; para dizer "
            "quais, use list_accepted_insurances)."
        )
    lines.extend(_topology_lines(tenant, professionals))
    return "\n".join(lines)


def selected_unit_id(conversation) -> UUID | None:
    """The unit this conversation already has (TASK-094 D4): the appointment being edited, else
    the booking in progress. None without one - the prompt then lists every usable unit."""
    if conversation is None:
        return None
    raw_edit = getattr(conversation, "flow_edit_draft", None)
    if getattr(conversation, "flow_state", None) == FlowState.EDIT_BOOKING and isinstance(
        raw_edit, dict
    ):
        current = raw_edit.get("current")
        if isinstance(current, dict) and current.get("unit_id"):
            try:
                return UUID(str(current["unit_id"]))
            except ValueError:
                return None
    raw = getattr(conversation, "flow_selected_unit_id", None)
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except ValueError:
        return None


def build_patient_identity(
    patient_name: str | None, patient_email: str | None, *, email_on_account: bool = False
) -> PatientIdentityStatus:
    """Which identity data we hold for the patient of this turn (TASK-072).

    Booleans only: the system prompt is not pseudonymized, so no value goes in it.
    `email_on_account`: the channel proves an e-mail before the first message (the Portal's
    inline identity), so the address exists on the account even when `Patient.email` - a
    booking-time copy - is still empty.
    """
    return PatientIdentityStatus(
        name_known=bool((patient_name or "").strip()),
        email_known=email_on_account or bool((patient_email or "").strip()),
    )


def build_clinic_features(tenant, flow_tenant=None) -> ClinicFeatures | None:
    """Which optional features are ON for the clinic (TASK-072), or None without a tenant.

    `tenant` is the `Tenant` row (the deposit switch lives there); `flow_tenant` is the
    worker's flow snapshot, whose `insurance_plans` carry the clinic's real convênio rows.
    """
    if tenant is None:
        return None
    plans = fr.insurance_plan_names(flow_tenant if flow_tenant is not None else tenant)
    return ClinicFeatures(
        deposit_enabled=bool(getattr(tenant, "pix_deposit_enabled", False)),
        insurance_plan_count=len(plans),
        online_booking=fr.flows_enabled(tenant),
    )
