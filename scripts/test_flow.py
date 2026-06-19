"""Terminal harness for the deterministic flow router (cancel/reschedule).

Unlike scripts/test_agent.py (which drives the LangGraph LLM agent), this
exercises the ZERO-LLM `flow_router.route()` exactly as the arq worker calls it,
but with in-memory conversation state, a fake calendar and sample appointments.
No DB, no WhatsApp, no OpenAI/Google credentials required.

It reads messages from stdin (one per line), so it works both interactively:

    uv run python scripts/test_flow.py

and scripted (pipe a sequence of taps):

    printf 'Remarcar/Cancelar\n...\n' | uv run python scripts/test_flow.py

Slot/list rows are printed as the exact string to type back (WhatsApp echoes a
list-row tap as "<label> (<iso>)"); just copy that line.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble
from secretaria.models import FlowState
from secretaria.services.flow_router import MenuBubble, route

TZ = ZoneInfo("America/Sao_Paulo")


def _tenant() -> SimpleNamespace:
    return SimpleNamespace(
        initial_flows={"enabled": True},  # default menu + manage label
        appointment_types=[
            {"name": "Primeira Consulta", "duration_min": 40, "is_active": True,
             "sort_order": 0, "price": "R$ 250"},
            {"name": "Retorno", "duration_min": 30, "is_active": True, "sort_order": 1},
        ],
        appointment_duration_min=30,
        business_hours={
            d: [{"start": "08:00", "end": "18:00"}]
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday")
        },
    )


def _sample_appointments() -> list[dict]:
    base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    a1 = base + timedelta(days=3, hours=14)
    a2 = base + timedelta(days=6, hours=9)
    return [
        {"id": "a1", "google_event_id": "evt-a1", "appointment_type": "Primeira Consulta",
         "start_at": a1, "end_at": a1 + timedelta(minutes=40)},
        {"id": "a2", "google_event_id": "evt-a2", "appointment_type": "Retorno",
         "start_at": a2, "end_at": a2 + timedelta(minutes=30)},
    ]


class FakeCalendar:
    """In-memory calendar: free slots are 08:00/09:00/10:00 on any requested day."""

    def __init__(self) -> None:
        self.tzinfo = TZ

    async def list_free_slots(self, day, slot_minutes=30, max_slots=8):
        d = day.date()
        out = []
        for hour in (8, 9, 10):
            start = datetime.combine(d, datetime.min.time()).replace(hour=hour)
            end = start + timedelta(minutes=slot_minutes)
            out.append({"start": start.isoformat(timespec="minutes"),
                        "end": end.isoformat(timespec="minutes"),
                        "label": f"{hour:02d}:00"})
        return out[:max_slots]

    async def cancel_event(self, event_id):
        print(f"   [calendar] cancel_event({event_id})")

    async def update_event(self, event_id, start, end):
        print(f"   [calendar] update_event({event_id}, {start:%d/%m %H:%M} -> {end:%H:%M})")
        return {"id": event_id}


def _print_bubbles(bubbles: list) -> None:
    for b in bubbles:
        if isinstance(b, MenuBubble):
            print(f"\n👁️  {b.body}")
            print("    botões: " + " | ".join(b.labels))
        elif isinstance(b, ButtonBubble):
            print(f"\n👁️  {b.body}")
            print(f"    botões: {b.confirm_label} | {b.cancel_label}")
        elif isinstance(b, SlotsBubble):
            print(f"\n👁️  {b.body}")
            for rid, label in b.rows:
                if rid.startswith("slot|"):
                    print(f"    • {label} ({rid.split('|', 1)[1]})   <- digite esta linha")
                else:
                    print(f"    • {label}")
        elif isinstance(b, TextBubble):
            print(f"\n👁️  {b.body}")


def _apply(conv: SimpleNamespace, upcoming: list[dict], result) -> None:
    """Mirror the worker's _apply_flow_result against in-memory state."""
    conv.flow_state = result.flow_state
    conv.flow_step = result.flow_step
    conv.flow_selected_type = result.flow_selected_type
    conv.flow_selected_day = result.flow_selected_day
    conv.flow_selected_slot = result.flow_selected_slot
    if result.appointment_cancel_id:
        upcoming[:] = [a for a in upcoming if a["google_event_id"] != result.appointment_cancel_id]
    if result.appointment_reschedule:
        r = result.appointment_reschedule
        for a in upcoming:
            if a["google_event_id"] == r["google_event_id"]:
                a["start_at"], a["end_at"] = r["start_at"], r["end_at"]


async def _demo() -> None:
    """Scripted run of the full cancel + reschedule flows (dynamic dates)."""
    tenant, calendar, upcoming = _tenant(), FakeCalendar(), _sample_appointments()
    conv = SimpleNamespace(
        flow_state=FlowState.IDLE, flow_step=None, flow_selected_type=None,
        flow_selected_day=None, flow_selected_slot=None, patient_id="p1",
    )

    async def step(text):
        print(f"\nVocê> {text}")
        result = await route(conv, tenant, calendar, text, "João", upcoming_appointments=upcoming)
        if result.action == "delegate_llm":
            print("\n[router] -> delegate_llm")
        elif result.action == "calendar_unavailable":
            print("\n[router] -> calendar_unavailable")
        else:
            _print_bubbles(result.bubbles)
        _apply(conv, upcoming, result)
        print(f"   [estado: {conv.flow_state} / {conv.flow_step}]")
        return result

    def tap_first_row(result):
        rid, label = result.bubbles[0].rows[0]
        return f"{label} ({rid.split('|', 1)[1]})" if rid.startswith("slot|") else label

    print("\n" + "#" * 64 + "\n# FLUXO 1: CANCELAR\n" + "#" * 64)
    r = await step("Remarcar/Cancelar")
    r = await step(tap_first_row(r))   # escolhe a 1ª consulta
    await step("Cancelar")
    await step("Sim")
    print(f"\n>>> Consultas restantes: {[a['appointment_type'] for a in upcoming]}")

    print("\n" + "#" * 64 + "\n# FLUXO 2: REMARCAR\n" + "#" * 64)
    r = await step("Remarcar/Cancelar")
    r = await step(tap_first_row(r))   # escolhe a consulta restante
    await step("Remarcar")
    r = await step("segunda")          # novo dia
    r = await step(tap_first_row(r))   # escolhe 1º horário livre
    await step("Confirmar")
    print(f"\n>>> Consulta remarcada para: {upcoming[0]['start_at']:%d/%m %H:%M}")


async def main() -> None:
    if "--demo" in sys.argv:
        await _demo()
        return
    tenant = _tenant()
    calendar = FakeCalendar()
    upcoming = _sample_appointments()
    conv = SimpleNamespace(
        flow_state=FlowState.IDLE, flow_step=None, flow_selected_type=None,
        flow_selected_day=None, flow_selected_slot=None, patient_id="p1",
    )

    print("=" * 64)
    print("SecretarIA - flow router terminal (cancelar/remarcar, ZERO LLM)")
    print("Consultas de exemplo:")
    for a in upcoming:
        print(f"  - {a['appointment_type']} em {a['start_at']:%d/%m %H:%M}")
    print("Comece digitando: Remarcar/Cancelar     ('sair' encerra)")
    print("=" * 64)

    for raw in sys.stdin:
        user = raw.strip()
        if not user:
            continue
        if user.lower() in {"sair", "exit", "quit"}:
            break
        print(f"\nVocê> {user}")
        result = await route(conv, tenant, calendar, user, "João", upcoming_appointments=upcoming)
        if result.action == "delegate_llm":
            print("\n[router] -> delegate_llm (cairia no agente LLM aqui)")
        elif result.action == "calendar_unavailable":
            print("\n[router] -> calendar_unavailable (handoff humano)")
        else:
            _print_bubbles(result.bubbles)
        _apply(conv, upcoming, result)
        print(f"\n   [estado: {conv.flow_state} / {conv.flow_step}]")


if __name__ == "__main__":
    asyncio.run(main())
