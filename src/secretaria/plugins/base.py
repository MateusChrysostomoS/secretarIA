"""PluginSpec: the shape of one optional, entitlement-gated capability."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from secretaria.models import Appointment, Patient, Tenant


@dataclass(frozen=True)
class PluginSpec:
    """One optional capability, gated by ANY of a set of entitlement keys.

    id: the addon id (e.g. "pix_whatsapp") or tier name (e.g. "bronze_1")
        this plugin represents; also its key in `registry.REGISTRY`.
    entitlement_keys: what `services.entitlements_client.is_entitled` checks —
        ANY-OF semantics (`registry.enabled_plugins` enables the plugin when
        the tenant is entitled to at least one key in this tuple). Usually a
        single-element tuple equal to `(id,)`; a tuple is needed when a
        capability is reachable via more than one path (e.g. a tier AND an
        addon that both unlock it — cumulative tiers already imply higher
        tiers via `is_entitled`, so a higher tier never needs to be listed
        explicitly here).
    agent_tools: LangChain tool objects appended to the agent's tool list
        (ai/graph.py:build_agent) when this plugin is enabled for a tenant.
        Empty by default — not every plugin adds an agent tool.
    post_booking: called by `registry.run_post_booking` (from
        plugins/post_booking.py:run_post_booking_hooks, an arq job ENQUEUED —
        never awaited inline — from both booking commit points:
        ai/tools.py:_persist_appointment and workers/tasks.py:_apply_flow_result)
        with one `PostBookingContext`, strictly AFTER the appointment is
        already committed. Entirely off the hot path: a hook is free to do
        real I/O (an EHR push, a Pix charge send, an analytics write) without
        costing the patient's booking confirmation any latency. Unlike
        `on_inbound` there is no short-circuit — every entitled plugin with a
        hook runs, each independently fail-open (registry.run_post_booking
        wraps each call in its own try/except). None (the default) means this
        plugin never reacts to a booking.
    on_inbound: called by `registry.run_on_inbound` (from
        workers/tasks.py:_send_bot_reply, after the entitlement gate and
        before the flow router) with one `InboundContext`. Returning True
        means "handled — suppress the normal bot reply for this turn"; the
        first plugin (in registration order) whose hook returns True wins.
        None (the default) means this plugin never intercepts inbound turns.
    """

    id: str
    entitlement_keys: tuple[str, ...]
    agent_tools: tuple[Any, ...] = ()
    post_booking: Callable[..., Any] | None = None
    on_inbound: Callable[["InboundContext"], Any] | None = None


@dataclass(frozen=True)
class InboundContext:
    """What an `on_inbound` hook needs to decide + act on one inbound turn.

    Hooks run inside workers/tasks.py:_send_bot_reply, strictly AFTER the
    entitlement gate (so `tenant` is always non-None here) and BEFORE the
    deterministic flow router / LLM.

    `tenant` is a DETACHED ORM instance carrying only already-loaded scalar
    columns (the session that fetched it has closed by the time hooks run) —
    safe to read (`tenant.timezone`, `tenant.business_hours`, ...), but a hook
    must never touch a lazy relationship on it. A hook that needs to mutate
    DB state (e.g. flip handover) opens its OWN `async_session_factory()`
    session and re-fetches by id, exactly like the rest of workers/tasks.py's
    fire-and-forget send paths (see `_handle_calendar_unavailable`).
    """

    tenant: "Tenant"
    conversation_id: UUID
    patient_wa_id: str
    inbound_body: str
    waba_token: str | None
    redis: Any = None


@dataclass(frozen=True)
class PostBookingContext:
    """What a `post_booking` hook needs to react to one freshly booked appointment.

    Built by plugins/post_booking.py:run_post_booking_hooks — the arq job
    enqueued off BOTH booking commit points (ai/tools.py:_persist_appointment
    and workers/tasks.py:_apply_flow_result) — and passed to
    `registry.run_post_booking`. Unlike `InboundContext`, this runs entirely
    OFF the hot path: arriving here already cost nothing to the patient's
    booking confirmation, so a hook is free to do real I/O (an EHR push, a
    Pix charge send) without any latency budget.

    `tenant`/`patient`/`appointment` are DETACHED ORM instances (the session
    that loaded them has closed) — safe to read scalar columns, never a lazy
    relationship. `patient` is None for an appointment with no associated
    patient (e.g. a block-slot created via the doctor hub). `source`
    distinguishes which booking path created the appointment — "agent"
    (ai/tools.py's LangChain tools) or "flow" (the deterministic flow
    router) — used today only by analytics_bi's event payload.
    """

    tenant: "Tenant"
    patient: "Patient | None"
    appointment: "Appointment"
    waba_token: str | None
    source: Literal["agent", "flow"]
