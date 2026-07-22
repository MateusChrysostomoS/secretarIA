# CHECKPOINT — Context-aware opening + read-only patient-appointments tool

> Builds on `docs/CHECKPOINT_multi_doctor_flow.md` (the deterministic flow /
> LLM-handoff surface). This round (PROMPT 1 of the context-aware-opening
> series) makes the conversation-opening greeting reflect the patient's REAL
> appointment state, and gives the LLM agent a read-only, patient-scoped way
> to answer "tenho consulta marcada?" from DB truth. PROMPT 2 (final per-state
> greeting copy) shipped the same day — see its own section further down; the
> MVP copy described in the PROMPT 1 sections below has been superseded for
> HAS_UPCOMING(_SOON) only.

Built 2026-07-21. Suite state: **756 passed** (`uv run python -m pytest`, 16
new tests), `ruff check` introduces zero new findings, `graphify update .`
run. NOT committed — review + commit/push is manual, as usual.

## Core rule

"Has an appointment?" is **derived by query at message time, never stored** —
no boolean flag on `Patient` (a stored flag desyncs on book/cancel/complete).
All derivation lives in ONE place:

## `services/patient_context.py` (new — the single source of truth)

- `load_upcoming_appointments(session, tenant_id, patient_id, *, now=None)` —
  EXTRACTED from `workers/tasks.py` (was the private
  `_load_upcoming_appointments`). Future appointments, nearest first,
  status in **SCHEDULED + CONFIRMED** (deliberately broadened from
  SCHEDULED-only: a CONFIRMED appointment is still upcoming and still
  manageable — this also means the "Gerenciar consulta" flow now lists
  CONFIRMED rows, a small intentional behavior change). Same detached-dict
  shape as before (`id`, `google_event_id`, `appointment_type`, `start_at`,
  `end_at`).
- `resolve_patient_opening_state(...) -> PatientOpeningContext | None` — the
  5-state resolver, first-match order, buckets evaluated lazily (1 indexed
  SELECT for the common has-upcoming case, at most 3 small ones):
  1. `HAS_UPCOMING_SOON` — nearest future appointment starts within
     `UPCOMING_SOON_HOURS`;
  2. `HAS_UPCOMING` — future appointment(s), none inside the soon window;
  3. `JUST_HAD_CONSULT` — no future, but a non-cancelled/non-rescheduled
     appointment STARTED within `POST_CONSULT_WINDOW_HOURS`;
  4. `RETURNING_NO_APPOINTMENT` — no future/recent, but ≥1 past appointment
     of any status;
  5. `NEW` — patient with no appointment history.
  `patient_id=None` returns **None**: callers MUST degrade to the plain
  first-contact greeting — never to "você não tem consulta" (erring toward
  first-contact is safe; erring toward "no appointment" is not).
- Stuck-row guard: there is NO auto-transition to attended/no_show (only the
  doctor sets status via the hub PATCH), so the recent-past lookback is hard
  bounded by `POST_CONSULT_WINDOW_HOURS` — weeks-old rows still sitting in
  SCHEDULED never influence the greeting.
- Logging: ids/state/counts only (`patient_opening_state_resolved`), never
  appointment contents.

## `workers/tasks.py` (the opening router)

- Gate wired inside `_persist_inbound_message`, immediately after
  `_select_greeting` — i.e. the EXACT gate `_greeting_buttons_for` already
  uses (verbatim greeting on the conversation-opening message only). No
  second gate exists; non-opening turns never resolve state. Runs entirely in
  the arq worker; the webhook ACK path is untouched.
- `_adapt_greeting_to_state` appends an MVP line to the tenant's verbatim
  greeting: states 1+2 share the upcoming line (with the nearest start
  rendered in the tenant timezone via `_format_appointment_when`); state 3
  uses NEUTRAL copy unless the row is explicitly `ATTENDED` (only then the
  "como foi sua consulta?" variant — a past row still SCHEDULED/CONFIRMED has
  an unknown outcome); states 4+5 leave the greeting untouched. Buttons are
  unchanged (the greeting still doubles as the effective menu).
- The manage trigger in `_send_bot_reply` now calls the shared
  `load_upcoming_appointments` — one source of truth with the resolver and
  the agent tool.

## Agent surface

- `ai/tools.py::list_patient_appointments` — in `_BASE_TOOLS` (ai/graph.py).
  Read-only, futures-only; resolves tenant/patient from the ContextVars, and
  returns ONLY `quando` (dd/mm/yyyy in the tenant tz) + `tipo` per row —
  deliberately **no ids, not even `google_event_id`**, so the model cannot
  feed `cancel_event` from what it saw here. Unresolved patient → an explicit
  error payload, NEVER an empty list (same never-say-"no appointment" rule as
  the gate). Reschedule/cancel route back to the deterministic
  MANAGE_BOOKING rail via the existing `show_main_menu`
  exception→sentinel mechanism.
- `ai/prompts.py` — new bullet in the MENU section: use the tool for
  "já marcadas" questions, never answer from memory, and hand management
  actions to `show_main_menu` (Gerenciar consulta) instead of doing them in
  chat.

## Settings

`UPCOMING_SOON_HOURS=48`, `POST_CONSULT_WINDOW_HOURS=48` (config.py +
.env.example). Defaults are safe — no deployment env change required unless
overriding.

## Tests (tests/test_patient_context.py + tests/test_list_patient_appointments_tool.py)

Each of the 5 states; CONFIRMED counts as upcoming; cancelled rows never
count; stuck-past-SCHEDULED beyond the window excluded (but feeds
has_history); unresolved patient → None → greeting unchanged; neutral vs
attended-only copy; gate fires exactly once and only on the opening message
(`_persist_inbound_message` end-to-end on sqlite); shared-query identity
(`tasks.load_upcoming_appointments is patient_context.load_upcoming_appointments`);
webhook ACK path never calls the resolver; tool lists futures only, nearest
first, tz-rendered, zero ids in the payload, is read-only (rows unchanged),
errors on unresolved patient, and is registered in `_BASE_TOOLS` with the
prompt + description teaching the show_main_menu routing.

---

# PROMPT 2 — state-driven greetings, buttons, manage sub-flow, Outro handoff

Built 2026-07-21, same day, on top of PROMPT 1 (and PROMPT 3's post-consult
fields). Suite state after this round: **844 passed** (`uv run python -m
pytest`), `ruff check` clean, `graphify update .` run. Alembic head:
`e51cd84e1959` (`conversation_flow_managing_appointment_id`). NOT committed —
review + commit/push is manual, as usual. brain-frontend carries a matching
uncommitted change (see below).

## Pré-consulta orientations persistence (the round's resolved blocker)

The greeting spec required per-service pre-consult orientations, which had no
backend. Decision (explicit sign-off): build persistence now.

- `schemas/config.py::AppointmentType.requirements: list[str]` — items
  stripped, blanks dropped, ≤300 chars each, ≤20 items. Flows through BOTH
  hub PUTs (tenant config and professional config share the same
  `AppointmentType` class) into the `appointment_types` JSON columns — **no
  DB migration needed** for this part.
- `services/tenant_config.py::RuntimeAppointmentType.requirements` +
  `load_tenant_config` mapping (missing key on old data → `[]`).
- brain-frontend (`lib/secretaria-hub.ts`, `configuracao/lib/hub-mapping.ts`):
  `AppointmentTypeWire.requirements: string[]` now round-trips — the
  ServiceCard "Orientações de pré-consulta" editor saves for real instead of
  dropping on save.

## Greeting — HAS_UPCOMING(_SOON) final copy

- `_adapt_greeting_has_upcoming` + `_compose_upcoming_greeting_body`
  (`workers/tasks.py`, both pure): detail block for the NEAREST appointment
  (start in tenant tz, owning professional's stored name — INACTIVE included,
  service — price, description, "Orientações de pré-consulta:" bullets), then
  a brief block when more future appointments exist (one
  "when — service[ — doctor]" line each, nothing else), then a closing action
  hint. Size-budgeted under WhatsApp's 1024-char interactive body
  (`GREETING_DETAIL_MAX_CHARS=1000`; trim order: description → brief lines
  kept to 3 with "… e mais N consultas" → orientações bullets kept to 3 with
  "…").
- Data loaded by `_load_upcoming_greeting_data` inside the open ingest
  session; the nearest service is matched casefold against the OWNING
  professional's catalog, else the tenant's. JUST_HAD_CONSULT /
  RETURNING_NO_APPOINTMENT / NEW copy untouched (PROMPT 1 behavior stands).

## Buttons by state

`_greeting_buttons_for(…, opening_context)`: HAS_UPCOMING(_SOON) with flows
enabled → exactly **[Remarcar] [Cancelar] [Outro]** (wins over the
multi-doctor trio too — the existing appointment's actions take the two
deterministic slots; 3-button WhatsApp cap). Every other state, flow-less
tenants, and unresolved context keep today's buttons exactly.

## Manage sub-flow — deterministic actions on the existing appointment

- New column `conversations.flow_managing_appointment_id` (UUID, FK →
  appointments, ON DELETE SET NULL; migration `e51cd84e1959`) — replaces the
  old `flow_selected_type` overload everywhere inside MANAGE_BOOKING
  (`flow_selected_type` stays NULL there now; carried by `_preserve`/
  `_preserve_reply`/snapshot/`_apply_flow_result` like the other flow fields).
- `route()` at IDLE/MENU/BUSINESS_HOURS: `LABEL_RESCHEDULE`/
  `LABEL_CANCEL_APPT` → `enter_manage_action(intent, …)` — single future
  appointment goes STRAIGHT to the day prompt / cancel-confirm card
  (`_begin_reschedule`/`_begin_cancel`, shared with the classic action card);
  multiple → intent-specific disambiguation SlotsBubble
  (`STEP_MANAGE_PICK_RESCHEDULE`/`_CANCEL`, explicit 10-row cap). The classic
  `manage_label` path (pick → action card) is unchanged.
- `LABEL_OTHER` ("Outro") is now an explicit `route()` match → sticky LLM,
  independent of the tenant's configured menu labels (previously only the
  index-based mapping or the multi-doctor trio reached it).
- Owning-professional calendar: `_manage_owner_calendar_target` (pure) +
  `manage_calendar` resolution in `_send_bot_reply` — manage turns act on the
  MANAGED appointment owner's agenda (tenant agenda when it has no owner),
  deliberately ignoring a stale `flow_selected_professional_id` from an
  earlier booking flow.
- PROMPT 4 hooks are comments only: deposit carry on reschedule
  (`_begin_reschedule`/`_manage_reschedule`), refund/retention on cancel
  (`_manage_cancel`).

## Outro → LLM handoff (appointment-aware agent mode)

- `ai/tools.py::manage_existing_appointment(action)` raises
  `ManageAppointmentRequested` → `run_agent` maps it to the
  `__MANAGE_APPOINTMENT__:<action>` sentinel →
  `workers/tasks.py::_handle_manage_appointment` reloads
  `load_upcoming_appointments` fresh and applies `enter_manage_action` via
  `_apply_flow_result`. LLM never executes remarcar/cancelar itself. Tool
  exposed only for flow-enabled tenants.
- Appointment context: `_appointment_context_text` (nearest with
  price/orientações when catalog-matched + brief lines) travels
  `run_agent(appointment_context=…)` →
  `TenantRuntimeConfig.appointment_context` →
  `prompts._format_appointment_context` — the "CONSULTAS MARCADAS DESTE
  PACIENTE" section with the rules: answer from this data; actions ONLY via
  `manage_existing_appointment`; "marcar outra consulta" via
  `show_main_menu`/`select_professional_and_continue` (standard booking
  flow). Gate: `_should_inject_appointment_context` — upcoming non-empty AND
  (sticky `FlowState.LLM` or this-turn delegation). Composes with the
  post-consult-knowledge gate, which is unchanged.

## Tests

756 (PROMPT 1) → **844**: hub `requirements` round-trips (tenant +
professional endpoints), runtime-config carry, greeting full/brief/degrade +
trim order + the three button sets, `_load_upcoming_greeting_data` (owner
catalog wins, inactive owner name, tenant fallback, no-match), manage
entries/disambiguation/10-row cap/full walkthroughs on the new column,
owner-calendar target, Outro-off-menu seam, tool/sentinel/handler/injection
wiring, prompt section formatting.

## Pendências

- Deploy: run `alembic upgrade head` (migration `e51cd84e1959`) on the
  secretaria service; redeploy brain-frontend so the orientations editor
  round-trips against the updated API.
- Commit/push are manual, as usual — secretarIA AND brain-frontend both carry
  this round uncommitted.
- Real-device WhatsApp e2e: adapted greeting, greeting-button manage entries,
  disambiguation list, Outro handoff + sentinel round-trip.
- PROMPT 4 integration points are marked with `PROMPT 4 hook` comments.
- Per-tenant configurable per-state greeting copy remains a possible
  follow-up (all copy lives as constants in `workers/tasks.py`).
