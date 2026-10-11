"""Run the real deterministic booking flow against an in-memory clinic, with NO side effects.

The manager edits the clinic's context on the left and, on the right, taps through "what the
patient would see after Agendar". This module is the engine behind that panel (TASK-027):

  * it calls the REAL `flow_router.route()` - the preview cannot drift from production;
  * it never calls `_apply_flow_result`, so nothing is persisted, no WhatsApp/Portal message is
    sent, no hold is taken, no usage event is emitted, no PreCheck hand-off is enqueued;
  * the calendar is a `CalendarService` subclass that keeps the real opening-hours arithmetic
    (`_windows_for_day` / `_walk_free_slots`) but never touches Google: nothing is busy and every
    write is a no-op that returns a fake event;
  * the conversation state is a plain JSON dict that travels in the browser (stateless server).

Out of scope on purpose: the free-text LLM path (a turn that would be handed to the model is
reported as a note, not run) and any step that needs a stored patient (identity, existing
appointments).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.models import FlowState
from secretaria.schemas.webhook import inbound_routing_text
from secretaria.services.calendar import CalendarService
from secretaria.services.flow_router import LABEL_BOOK, route
from secretaria.workers.shared.bubbles import _bubble_history_body, _bubble_interactive

SIMULATED_EVENT_ID = "simulation"
SIMULATION_PATIENT = "Paciente (simulação)"

STATE_FIELDS = (
    "flow_state",
    "flow_step",
    "flow_selected_type",
    "flow_selected_day",
    "flow_selected_slot",
    "flow_selected_professional_id",
    "flow_selected_insurance",
    "flow_managing_appointment_id",
    "flow_attendee_name",
)


class SimCalendar(CalendarService):
    """Real opening-hours arithmetic, no Google. Nothing is ever busy; writes are no-ops."""

    def __init__(self, *, timezone: str, business_hours: dict | None, slot_minutes: int) -> None:
        self._tz = ZoneInfo(timezone)
        self._business_hours = business_hours or {}
        self._default_slot_minutes = slot_minutes or 30
        self.writes: list[str] = []  # every attempted write, so tests can prove none escaped

    async def check_availability(self, start, end, max_results: int = 0) -> list[dict]:
        return []

    async def create_event(self, start, end, summary, description="", reminders=None) -> dict:
        self.writes.append("create_event")
        return {
            "id": SIMULATED_EVENT_ID,
            "htmlLink": "#",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    async def update_event(self, event_id, start, end) -> dict:
        self.writes.append("update_event")
        return {"id": event_id}

    async def cancel_event(self, event_id) -> None:
        self.writes.append("cancel_event")


def initial_state() -> dict[str, Any]:
    state: dict[str, Any] = dict.fromkeys(STATE_FIELDS)
    state["flow_state"] = FlowState.IDLE.value
    return state


def _conversation(state: dict[str, Any]) -> SimpleNamespace:
    data = dict(state)
    data["flow_state"] = FlowState(data.get("flow_state") or FlowState.IDLE.value)
    for key in ("flow_selected_professional_id", "flow_managing_appointment_id"):
        if data.get(key):
            data[key] = UUID(str(data[key]))
    fields = {key: data.get(key) for key in STATE_FIELDS}
    return SimpleNamespace(id=UUID(int=0), patient_id=None, **fields)


def _state_from(result) -> dict[str, Any]:
    state = {key: getattr(result, key, None) for key in STATE_FIELDS}
    state["flow_state"] = result.flow_state.value
    for key in ("flow_selected_professional_id", "flow_managing_appointment_id"):
        if state.get(key) is not None:
            state[key] = str(state[key])
    return state


def _render(bubble) -> dict[str, Any]:
    interactive = _bubble_interactive(bubble)
    if interactive is not None:
        return interactive  # {"kind": "buttons"|"list", body, options, ...} - the wire format
    return {"kind": "text", "body": _bubble_history_body(bubble)}


@dataclass(frozen=True)
class SimulationTurn:
    bubbles: list[dict[str, Any]]
    state: dict[str, Any]
    finished: bool
    note: str | None = None


_NOTES = {
    "delegate_llm": "Neste ponto a conversa passaria para a IA livre. A simulação não a executa.",
    "calendar_unavailable": "Neste ponto o paciente veria que a agenda está indisponível.",
    "handover": "Neste ponto a conversa seria passada para um atendente humano.",
    "professional_config_incomplete": (
        "Este profissional está com a configuração incompleta (serviços ou horários)."
    ),
}


async def simulate_turn(
    *,
    tenant: Any,
    professionals: list[Any] | None,
    calendar_for: Callable[[str | None], CalendarService],
    state: dict[str, Any] | None,
    tap: dict[str, str] | None = None,
    text: str | None = None,
) -> SimulationTurn:
    """One patient turn. `state=None` starts the flow as if the patient had tapped Agendar."""
    current = state or initial_state()
    if state is None:
        inbound = LABEL_BOOK
    elif tap:
        inbound = inbound_routing_text(tap.get("title"), tap.get("id")) or ""
    else:
        inbound = text or ""

    conversation = _conversation(current)
    selected = current.get("flow_selected_professional_id")
    result = await route(
        conversation,
        tenant,
        calendar_for(selected),
        inbound,
        SIMULATION_PATIENT,
        upcoming_appointments=[],
        professionals=professionals,
        gate=None,
    )
    bubbles = [_render(b) for b in result.bubbles]
    finished = result.appointment is not None
    note = _NOTES.get(result.action)
    if finished:
        note = "Consulta simulada: nada foi agendado de verdade."
    return SimulationTurn(bubbles=bubbles, state=_state_from(result), finished=finished, note=note)
