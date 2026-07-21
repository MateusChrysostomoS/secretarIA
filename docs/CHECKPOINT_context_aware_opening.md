# CHECKPOINT — Context-aware opening + read-only patient-appointments tool

> Builds on `docs/CHECKPOINT_multi_doctor_flow.md` (the deterministic flow /
> LLM-handoff surface). This round (PROMPT 1 of the context-aware-opening
> series) makes the conversation-opening greeting reflect the patient's REAL
> appointment state, and gives the LLM agent a read-only, patient-scoped way
> to answer "tenho consulta marcada?" from DB truth. PROMPT 2 (final per-state
> greeting copy) is expected next; the copy shipped here is deliberately MVP.

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

## Pendências

- PROMPT 2: final per-state greeting copy (likely tenant-configurable) —
  the three MVP lines live as constants in `workers/tasks.py`
  (`UPCOMING_APPOINTMENT_GREETING_LINE`, `JUST_HAD_CONSULT_NEUTRAL_LINE`,
  `JUST_HAD_CONSULT_ATTENDED_LINE`), one obvious place to swap.
- Commit/push + deploy (EasyPanel) are manual; no migration in this round.
- Real-device WhatsApp e2e of the adapted greeting still pending.
