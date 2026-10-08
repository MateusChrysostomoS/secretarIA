"""greeting - split out of workers/tasks.py (TASK-023)."""

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.whatsapp_limits import (
    EMOJI_SCHEDULE,
    decorate,
    truncate_plain,
)
from secretaria.models import (
    AppointmentStatus,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services.booking_scope import (
    sole_active_professional,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
)
from secretaria.services.flow_router import (
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    flows_enabled,
    manage_label,
    menu_buttons_for,
)
from secretaria.services.greeting_template import (
    clinic_description_budget,
    render_greeting,
)
from secretaria.services.insurance_catalog import (
    TenantInsurance,
)
from secretaria.services.patient_context import (
    PatientOpeningContext,
    PatientOpeningState,
)
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
    resolve_entries,
)
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
    professional_business_hours,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.text import (
    GREETING_ACTION_HINT,
    GREETING_BRIEF_HEADER,
    GREETING_BRIEF_KEEP,
    GREETING_DETAIL_MAX_CHARS,
    GREETING_REQUIREMENTS_HEADER,
    GREETING_REQUIREMENTS_KEEP,
    JUST_HAD_CONSULT_ATTENDED_LINE,
    JUST_HAD_CONSULT_NEUTRAL_LINE,
    _as_utc,
    _render_greeting_template,
)


def _menu_vocabulary(tenant: Tenant) -> list[str]:
    """Every menu label this clinic can show — never a name (`parse_patient_name`).

    Both menus (single- and multi-doctor) and the manage label, because the name
    question can be answered after either was seen, and a clinic may rename all
    of them in `initial_flows`.
    """
    return [
        *menu_buttons_for(tenant, False),
        *menu_buttons_for(tenant, True),
        manage_label(tenant),
    ]

def _asks_name_at_first_contact(channel: str, patient: Patient, is_returning_patient: bool) -> bool:
    """Whether a first contact on `channel` is followed by the name question.

    The WhatsApp half of the name step's channel decision (the Brain-Message
    half is the "e-mail is new" branch of the claim in `_send_bot_reply`,
    because on the Portal the e-mail comes first and decides who is new).

      * Brain-Message: never HERE — the question waits for the e-mail.
      * WhatsApp, a Patient row created by this very message: ALWAYS, even
        though the row was just created with Meta's `profile.name` — the owner
        does not trust it (2026-09-20) and the typed answer replaces it.
      * WhatsApp, a Patient row that already existed (a first contact again
        only because the history was wiped): only if we still have no name.
        A returning patient whose name we already hold is never asked again.
    """
    if channel == CHANNEL_BRAIN_MESSAGE:
        return False
    return not is_returning_patient or not patient.name

def _first_contact_reply(
    *,
    tenant: Tenant,
    conversation_id: UUID,
    patient_ref: str,
    channel: str,
    inbound_body: str = "",
    ask_name: bool = False,
) -> _ReplyContext:
    """The very first thing a clinic says to a patient who owes consent.

    ONE spelling of the first-contact turn, because there are now two ways to
    reach it and only one of them involves the patient speaking:

      * `_route_inbound_turn`, when a first message arrives;
      * `_open_brain_message_conversation`, when the Portal link is opened and
        the automation speaks first (`POST /internal/brain-message/open`).

    Keeping it here is what makes that second entry point a new TRIGGER rather
    than a second greeting machine: change the frame, the button policy or the
    probe once and both doors produce the identical pair of messages.

    The frame goes out BUTTON-FREE and the consent notice follows it. Offering
    [Agendar] here would invite a tap the gate is about to refuse, and would
    put two button messages back to back with different jobs.

    On Brain-Message `_send_bot_reply` asks brain-api whether this visitor
    still owes an address and, if so, sends the e-mail question in the slot the
    consent notice would have taken. `send_consent_notice` stays True as the
    fallback for every other channel and for an unavailable/unknown probe. A
    VERIFIED account is handled separately and skips account LGPD, as the
    brain-api contract requires.

    `ask_name` (WhatsApp, `_asks_name_at_first_contact`): the name question
    takes the consent notice's slot instead, and the notice follows the answer.
    `_send_bot_reply` moves the state to AWAITING_NAME when it asks.
    """
    return _ReplyContext(
        channel=channel,
        conversation_id=conversation_id,
        tenant_id=tenant.id,
        patient_ref=patient_ref,
        inbound_body=inbound_body,
        greeting_override=render_greeting(tenant.clinic_name, _fit_clinic_description(tenant)),
        greeting_buttons=[],
        send_consent_notice=not ask_name,
        send_name_request=ask_name,
        probe_pending_identity=(channel == CHANNEL_BRAIN_MESSAGE),
    )

def _select_greeting(
    tenant: Tenant,
    patient: Patient,
    is_first_contact: bool,
    is_returning_patient: bool,
) -> str | None:
    """Pick the greeting to send on first contact, or None.

    Returning patients (seen before) still get their clinic's own
    `returning_greeting_message` with the `{{name}}` placeholder filled — that
    one is a short "good to see you again" line and stays clinic-authored.

    Everyone else gets the PRODUCT FRAME (services/greeting_template.py),
    which is why this branch can no longer return None: the frame carries the
    automated-assistant disclosure, the no-medical-advice line and the
    emergency escape, so a clinic that configured nothing must still send it.
    Before this round it returned None for exactly that clinic and the LLM
    improvised an opener carrying none of those obligations.
    """
    if not is_first_contact:
        return None
    returning = (tenant.returning_greeting_message or "").strip()
    if is_returning_patient and returning:
        return _render_greeting_template(returning, patient.name)
    # The name only ever rides on the Portal: WhatsApp asks for it right AFTER
    # this greeting (AWAITING_NAME), and its 1024-char budget is measured on
    # the no-name frame.
    return render_greeting(
        tenant.clinic_name,
        _fit_clinic_description(tenant),
        patient_name=patient.name if patient.channel == CHANNEL_BRAIN_MESSAGE else None,
    )

def _fit_clinic_description(tenant: Tenant) -> str:
    """The clinic's description, cut to what still fits for THIS clinic.

    `services/tenant_config.py` already refuses an over-budget description on
    save, so this is normally a no-op. It is not redundant: the budget shrinks
    when `clinic_name` GROWS, and renaming a clinic goes through a different
    path that never revalidates the description. Without this, a rename could
    push the rendered greeting past 1024 chars — and `send_buttons` does not
    truncate, so Meta 400s and the patient receives NOTHING.
    """
    description = (tenant.clinic_description or "").strip()
    return truncate_plain(description, clinic_description_budget(tenant.clinic_name))

def _greeting_buttons_for(
    tenant: Tenant,
    greeting_override: str | None,
    opening_context: PatientOpeningContext | None = None,
) -> list[str]:
    """Buttons to attach to a greeting: a FIXED, product-defined set.

    NEVER the clinic's own free text: `tenant.greeting_buttons` is no longer
    read here (nor anywhere else) - see docs/CHECKPOINT_fixed_greeting_buttons.md.
    Every button this function can return has a deterministic route() entry
    (flow_router.py's LABEL_BOOK/LABEL_RESCHEDULE/LABEL_CANCEL_APPT/
    LABEL_OTHER matches) and never falls through to the LLM on its own tap.

    WhatsApp caps interactive messages at 3 buttons. When the patient has a
    live upcoming appointment (HAS_UPCOMING/_SOON, from `opening_context`)
    AND flows are enabled, the two most useful actions - Remarcar/Cancelar -
    win the two deterministic slots, with "Outro" filling the third for
    anything else (unchanged). Every other case - including a flows-DISABLED
    tenant - gets [Agendar, Outro].

    That pair used to be a trio, with "Gerenciar consulta" in the middle. It
    is gone from THIS branch on purpose: this branch is precisely the patient
    with nothing booked, for whom the button could only ever reach
    `_enter_manage`'s dead end ("Você não tem nenhuma consulta agendada no
    momento.") - a card offering an action the system already knows is
    impossible. The patient who DOES have something booked never saw it
    either: they get the Remarcar/Cancelar trio above. Nothing became
    unreachable - `route()` still dispatches the label from an older thread's
    button or from typed text, which is also why LABEL_MANAGE_APPOINTMENT
    stays in `_GREETING_ACTION_IDS`.

    For a flows-enabled tenant route() dispatches each label deterministically
    ("Outro" deliberately to the LLM); for a flows-disabled one, an Agendar
    tap is caught by `_persist_inbound_message`'s greeting-button
    short-circuit and degrades to a fixed "contact us" reply
    (`_handle_greeting_button_unavailable`), while "Outro" falls through to
    that tenant's normal all-LLM path - for that cohort the LLM IS the
    product, so "anything else" belongs there. Empty without a greeting.
    """
    if greeting_override is None:
        return []
    if (
        flows_enabled(tenant)
        and opening_context is not None
        and opening_context.state
        in (PatientOpeningState.HAS_UPCOMING_SOON, PatientOpeningState.HAS_UPCOMING)
        and opening_context.future_appointments
    ):
        return [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_OTHER]
    # `Agendar` carries the calendar emoji; `Outro` deliberately does not - it
    # is the "anything else" escape, and decorating it would imply a category.
    #
    # Decorating is safe ONLY because the label keeps its underlying word:
    # every matcher compares through `flow_router._norm`, which runs
    # `strip_decoration` on BOTH sides, so "🗓️ Agendar", "Agendar" and a typed
    # "agendar" share one key (FEAT 44). Renaming it to "Agendar Consulta"
    # would NOT be safe for free - it changes what `strip_decoration` yields,
    # so `LABEL_BOOK` itself would have to be renamed and every matcher, the
    # LLM prompt and `_GREETING_ACTION_IDS` would have to follow. Length was
    # never the constraint: "🗓️ Agendar Consulta" is 19 of the 20 allowed.
    return [decorate(EMOJI_SCHEDULE, LABEL_BOOK), LABEL_OTHER]


def _flow_professionals(
    professional_rows: list[Professional], services: list | None
) -> list[SimpleNamespace]:
    """The router-shaped ACTIVE roster: plain snapshots, catalog already resolved.

    What `route()` receives as `professionals`. `appointment_types` resolves through the
    clinic catalog (one spelling per service); `None` is preserved as `None`, never
    flattened to `[]`, because that is the ONLY thing that makes
    `professional_appointment_types` fall back to the tenant's legacy list - a
    professional whose own list is `[]` offers nothing. `business_hours` is verbatim, NULL
    and all: `professional_business_hours` reads the NULL-versus-EMPTY distinction to tell
    "inherits the clinic's hours" from "has none at all".
    """
    return [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            appointment_types=(
                resolve_entries(p.appointment_types, services)
                if p.appointment_types
                else p.appointment_types
            ),
            business_hours=p.business_hours,
        )
        for p in professional_rows
    ]


def _flow_tenant_snapshot(
    tenant: Tenant,
    professionals: list[Professional],
    services: list | None = None,
    insurance: TenantInsurance | None = None,
) -> SimpleNamespace:
    """Build the tenant-shaped config consumed by the deterministic router.

    A single active professional is the effective clinic config, matching
    ``load_tenant_config``. Multi-professional flows select a professional
    before reading that professional's catalog, so they keep tenant defaults
    on the initial snapshot.

    Resolution goes through ``professional_appointment_types`` /
    ``professional_business_hours``, so the sole professional's EMPTY own
    config stays empty here: a clinic that closed every day or removed every
    service has the deterministic router offer nothing, rather than quietly
    falling back to the tenant's legacy columns (see the NULL-versus-EMPTY note
    in services/tenant_config.py).

    The "exactly one" rule comes from `services/booking_scope.py`, the same
    one `resolve_booking_owner_id` applies to the ROSTER this snapshot travels
    with (``route(professionals=...)``) — so whoever's catalog is rendered
    here is provably whoever owns the booking that follows. `collect_insurance`
    / `insurances` stay tenant-level on purpose: they are clinic-wide settings
    and the convênio step asks them regardless of topology (see
    `flow_router._insurance_step_skip_reason`).

    `services` is the clinic's canonical catalog (services/service_catalog.py),
    loaded by the caller because the router itself does no DB I/O. Resolving
    HERE is what keeps `flow_router` a pure function while still showing the
    patient one spelling per service: the router receives entries that are
    already canonical and filters them exactly as before. An empty catalog (a
    tenant not backfilled yet) resolves to the raw stored entries.

    `insurance` is the clinic's convênio catalog selection
    (services/insurance_catalog.py::load_tenant_insurance), loaded by the
    caller for the same reason. It becomes `insurance_plans` (the plans, with
    catalog ids) and `insurance_accepted_by` (which doctor takes which), which
    is what lets the router MARK the doctors who take the patient's plan. Left
    out, `insurance_plans` stays None and the router falls back to the legacy
    `insurances` strings - same names, no marks.
    """
    appointment_types = resolve_entries(tenant.appointment_types, services)
    business_hours = tenant.business_hours
    professional = sole_active_professional(professionals)
    if professional is not None:
        appointment_types = professional_appointment_types(professional, tenant, services)
        business_hours = professional_business_hours(professional, tenant)

    return SimpleNamespace(
        initial_flows=tenant.initial_flows,
        reminders_v2_enabled=getattr(tenant, "reminders_v2_enabled", False),
        address=getattr(tenant, "address", None),
        timezone=getattr(tenant, "timezone", None),
        appointment_types=appointment_types,
        appointment_duration_min=tenant.appointment_duration_min,
        business_hours=business_hours,
        collect_insurance=tenant.collect_insurance,
        insurance_mode=getattr(tenant, "insurance_mode", None),
        insurances=tenant.insurances,
        insurance_plans=insurance.plans if insurance is not None else None,
        insurance_accepted_by=insurance.accepted_by if insurance is not None else {},
    )

def _format_appointment_when(start_at: datetime, tz_name: str | None) -> str:
    """Render an appointment start for greeting copy, in the tenant's timezone."""
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")
    return _as_utc(start_at).astimezone(tz).strftime("%d/%m às %H:%M")

@dataclass(frozen=True)
class _UpcomingGreetingData:
    """Plain data `_adapt_greeting_to_state` needs for HAS_UPCOMING(_SOON).

    Loaded by `_load_upcoming_greeting_data` inside the open ingest session
    (`_persist_inbound_message`) and passed in so the composition functions
    below stay DB-free and directly unit-testable. Every other opening state
    ignores this argument entirely.
    """

    # professional_id (str) -> stored display name (Professional.name,
    # verbatim - no honorific logic), for every distinct professional
    # referenced across `future_appointments`. Includes INACTIVE
    # professionals: an appointment with a deactivated doctor still shows
    # their name.
    professional_names: dict[str, str] = field(default_factory=dict)
    # Resolved catalog dict (name/price/description/long_description/
    # requirements - see services/tenant_config.py) for the NEAREST
    # appointment's service, matched by name (casefold, stripped) against the
    # owning professional's own catalog, or the tenant's when the appointment
    # has no owner. None when no match was found (stale/renamed service) -
    # the detail block then degrades to date/doctor/service-name only.
    nearest_service: dict | None = None

def _appointment_doctor_name(appointment: dict, professional_names: dict[str, str]) -> str | None:
    """The stored professional name for one `future_appointments` dict, or None.

    Verbatim from `Professional.name` - no "Dr(a)." honorific games, the
    clinic's own stored name is used as-is. None when the appointment has no
    `professional_id`, or (should not happen - see `_load_upcoming_greeting_data`)
    the id wasn't resolved.
    """
    professional_id = appointment.get("professional_id")
    return professional_names.get(professional_id) if professional_id else None

def _compose_upcoming_greeting_body(
    greeting: str,
    intro: str,
    description: str | None,
    requirements: list[str],
    brief_lines: list[str],
    *,
    max_chars: int = GREETING_DETAIL_MAX_CHARS,
) -> str:
    """Pure compose + size-trim for the HAS_UPCOMING(_SOON) greeting body.

    Assembles, in order: `greeting`, `intro` (the nearest appointment's
    always-shown when/doctor/service/price lines, already rendered by the
    caller), the optional `description` paragraph, the "Orientações de
    pré-consulta" bullets (one per `requirements` item), the "Suas próximas
    consultas" bullets (one per `brief_lines` entry - each already rendered
    as "when — service[ — doctor]": date+service+doctor ONLY, no
    price/description), and the closing action hint.

    Pure string/list manipulation - no DB, no ORM types - so it is unit-tested
    directly with plain args. Under-budget input is returned unmodified. Over
    `max_chars`, drops content in order until it fits: (1) `description`,
    (2) excess `brief_lines` (kept to the first `GREETING_BRIEF_KEEP`, with an
    "… e mais N consultas" tail), (3) excess `requirements` bullets (kept to
    the first `GREETING_REQUIREMENTS_KEEP`, with a "…" tail). If still over
    budget after all three, the maximally-trimmed body is returned as-is
    (best effort - the tenant's own greeting text is never touched).
    """

    def _assemble(
        *, include_description: bool, brief_cap: int | None, requirements_cap: int | None
    ) -> str:
        parts = [greeting, intro]
        if include_description and description:
            parts.append(description)
        if requirements:
            shown = requirements if requirements_cap is None else requirements[:requirements_cap]
            bullets = "\n".join(f"• {item}" for item in shown)
            if requirements_cap is not None and len(requirements) > requirements_cap:
                bullets += "\n…"
            parts.append(f"{GREETING_REQUIREMENTS_HEADER}\n{bullets}")
        if brief_lines:
            shown = brief_lines if brief_cap is None else brief_lines[:brief_cap]
            bullets = "\n".join(f"• {line}" for line in shown)
            if brief_cap is not None and len(brief_lines) > brief_cap:
                extra = len(brief_lines) - brief_cap
                tail = "consulta" if extra == 1 else "consultas"
                bullets += f"\n… e mais {extra} {tail}"
            parts.append(f"{GREETING_BRIEF_HEADER}\n{bullets}")
        parts.append(GREETING_ACTION_HINT)
        return "\n\n".join(parts)

    body = _assemble(include_description=True, brief_cap=None, requirements_cap=None)
    if len(body) <= max_chars:
        return body

    body = _assemble(include_description=False, brief_cap=None, requirements_cap=None)
    if len(body) <= max_chars:
        return body

    body = _assemble(
        include_description=False, brief_cap=GREETING_BRIEF_KEEP, requirements_cap=None
    )
    if len(body) <= max_chars:
        return body

    return _assemble(
        include_description=False,
        brief_cap=GREETING_BRIEF_KEEP,
        requirements_cap=GREETING_REQUIREMENTS_KEEP,
    )

def _adapt_greeting_has_upcoming(
    greeting: str,
    future_appointments: list[dict],
    tenant: Tenant,
    upcoming_data: _UpcomingGreetingData,
) -> str:
    """Render the HAS_UPCOMING(_SOON) greeting body (still a pure function).

    `future_appointments` is `PatientOpeningContext.future_appointments`
    (nearest first, guaranteed non-empty by the resolver for this state).
    `upcoming_data` carries the plain data the call site resolved inside the
    open DB session - see `_load_upcoming_greeting_data`.
    """
    nearest = future_appointments[0]
    when = _format_appointment_when(nearest["start_at"], tenant.timezone)
    doctor = _appointment_doctor_name(nearest, upcoming_data.professional_names)
    service_name = nearest.get("appointment_type") or "Consulta"

    header_line = f"Vi aqui que você já tem uma consulta marcada para {when}"
    if doctor:
        header_line += f", com {doctor}"
    header_line += "."

    service = upcoming_data.nearest_service
    price = service.get("price") if service else None
    service_line = f"{service_name} — {price}" if price else service_name
    description = (
        (service.get("long_description") or service.get("description")) if service else None
    )
    requirements = list(service.get("requirements") or []) if service else []

    brief_lines = []
    for appt in future_appointments[1:]:
        appt_when = _format_appointment_when(appt["start_at"], tenant.timezone)
        line = f"{appt_when} — {appt.get('appointment_type') or 'Consulta'}"
        appt_doctor = _appointment_doctor_name(appt, upcoming_data.professional_names)
        if appt_doctor:
            line += f" — {appt_doctor}"
        brief_lines.append(line)

    intro = f"{header_line}\n{service_line}"
    return _compose_upcoming_greeting_body(greeting, intro, description, requirements, brief_lines)

def _adapt_greeting_to_state(
    greeting: str,
    context: PatientOpeningContext | None,
    tenant: Tenant,
    upcoming_data: _UpcomingGreetingData | None = None,
) -> str:
    """Append the state-aware opening content to the verbatim greeting.

    A None context (patient not resolved) and the RETURNING_NO_APPOINTMENT/NEW
    states return the greeting unchanged — the safe degrade (the returning
    greeting message already covers the "good to see you again" tone).
    JUST_HAD_CONSULT only presupposes attendance when the doctor explicitly
    marked ATTENDED; a past row still in SCHEDULED/CONFIRMED has an unknown
    outcome and gets the neutral line. HAS_UPCOMING(_SOON) gets the full
    detail/brief blocks (`_adapt_greeting_has_upcoming`); `upcoming_data`
    defaults to empty so callers that don't resolve it (or don't need to,
    for other states) still degrade gracefully rather than crash.
    """
    if context is None:
        return greeting
    if context.state in (
        PatientOpeningState.HAS_UPCOMING_SOON,
        PatientOpeningState.HAS_UPCOMING,
    ):
        return _adapt_greeting_has_upcoming(
            greeting,
            context.future_appointments,
            tenant,
            upcoming_data or _UpcomingGreetingData(),
        )
    if context.state is PatientOpeningState.JUST_HAD_CONSULT:
        line = (
            JUST_HAD_CONSULT_ATTENDED_LINE
            if context.recent_past_appointments[0]["status"] == AppointmentStatus.ATTENDED
            else JUST_HAD_CONSULT_NEUTRAL_LINE
        )
        return f"{greeting}\n\n{line}"
    return greeting

async def _load_upcoming_greeting_data(
    session: AsyncSession, tenant: Tenant, future_appointments: list[dict]
) -> _UpcomingGreetingData:
    """DB-side loads for the HAS_UPCOMING(_SOON) greeting detail (impure).

    Called from `_persist_inbound_message`'s open ingest session, right after
    `resolve_patient_opening_state`. Resolves (a) the display name for every
    professional referenced across `future_appointments` (active or not - a
    deactivated doctor's name must still show on an existing booking), and
    (b) the catalog service dict for the NEAREST appointment (for
    price/description/requirements enrichment). Returns plain data only (see
    `_UpcomingGreetingData`) so `_adapt_greeting_to_state` and its helpers
    stay DB-free. No appointment content is logged here - state/count
    logging is already done by `resolve_patient_opening_state`.
    """
    professional_ids = {pid for appt in future_appointments if (pid := appt.get("professional_id"))}
    professionals_by_id: dict[str, Professional] = {}
    if professional_ids:
        rows = await session.scalars(
            select(Professional).where(Professional.id.in_([UUID(pid) for pid in professional_ids]))
        )
        professionals_by_id = {str(row.id): row for row in rows}

    nearest = future_appointments[0]
    owner_id = nearest.get("professional_id")
    owner = professionals_by_id.get(owner_id) if owner_id else None
    # Resolved through the clinic's canonical catalog so a historical
    # `appointment_type` whose spelling drifted still finds its service (and
    # therefore its price/description) — historical rows are never rewritten.
    services = await load_service_catalog(session, tenant.id)
    catalog = (
        professional_appointment_types(owner, tenant, services)
        if owner is not None
        else active_appointment_types(tenant, services)
    )
    nearest_service = None
    raw_type = nearest.get("appointment_type")
    if raw_type:
        target_name = normalize_service_name(raw_type)
        nearest_service = next(
            (svc for svc in catalog if normalize_service_name(svc.get("name")) == target_name),
            None,
        )

    return _UpcomingGreetingData(
        professional_names={pid: row.name for pid, row in professionals_by_id.items()},
        nearest_service=nearest_service,
    )
