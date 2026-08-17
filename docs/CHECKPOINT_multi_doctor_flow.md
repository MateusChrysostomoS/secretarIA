# CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection)

> **Superado em parte (2026-08-17):** o cartão de saudação de quem NÃO tem consulta
> futura passou a `[Agendar][Outro]`, o menu multi-médico passou a
> `[Escolher médico][Escolher serviço][Outro]`, e a pergunta de dia em texto livre virou
> um seletor tappável reaproveitado por todos os fluxos de agendamento — ver
> `CHECKPOINT_day_picker_entry_buttons.md`.

> **Superado em parte (2026-08-13):** `flows_enabled` virou incondicional — não existe
> mais cohort flows-desligado. Ver `CHECKPOINT_flows_unconditional.md`.

> Builds directly on `docs/CHECKPOINT_onboarding_multiprofessional.md` (the
> per-professional config layer: `specialty`/`about`/`context_doctor_message`,
> own hours/services/credential) and on `docs/CHECKPOINT_plugins.md` (the
> `multi_professional` addon's agent tools). This round gives tenants with 2+
> active professionals a **zero-LLM first-contact path** — pick a doctor from
> a tappable list (or describe a need and let the agent recommend one) — while
> keeping 0/1-professional tenants byte-identical to before.

> Follow-up (2026-07-21): the conversation-opening greeting became
> appointment-state-aware and the agent gained a read-only
> `list_patient_appointments` tool — see
> `docs/CHECKPOINT_context_aware_opening.md`.

> Follow-up (2026-08-02): the doctor list (and the service list) gained a fixed
> "Não sei" last row that opens a scoped, grounded, bounded LLM helper node
> (structured pick/clarify/escalate with a deterministic hand-back) — real
> option rows now cap at 9 to reserve that slot. See
> `docs/CHECKPOINT_trio_gerenciar_scoped_help.md`.

Built 2026-07-19, in one session, as three commits on `main` on top of the
baseline checkpoint commit (`dc09182`, which captured the previously
uncommitted onboarding/multi-professional working tree and fast-forwarded
`main` to `develop`'s tip — both branches now point at the same history;
`develop` was deliberately left in place, its cleanup is a separate decision):

| Commit | Scope |
| --- | --- |
| `543dad5` | PROMPT 1 — schema + shared professional-resolution service |
| `a8699fd` | PROMPT 2 — deterministic doctor-selection branch in `flow_router` |
| `81a65e1` | PROMPT 3 — LLM doctor-recommendation handoff + non-destructive menu return |

brain-frontend got exactly one companion commit (`90006c3`, on its `main`):
the `about` field hint in
`app/(site)/secretaria/configuracao/components/ProfessionalsSection.tsx` now
discloses that the text is shown verbatim to patients of multi-professional
clinics. No brain-api or PreCheck changes — confirmed out of scope.

Suite state at the end of the session: **737 passed** (`uv run python -m
pytest`, from the repo root), up from 690 at baseline; `ruff check src tests`
carries the same 7 pre-existing findings as the baseline (none introduced).
`graphify update .` run and committed. Nothing was pushed — review + push is
manual.

## Schema (migration `b3d9f2a1c8e7`, revises `f06e85b476f2`)

- `conversations.flow_selected_professional_id` — nullable UUID, FK →
  `professionals.id` **ON DELETE SET NULL**; which doctor the patient picked
  inside the SERVICE_CATALOG flow, parallel to `flow_selected_type/day/slot`.
- `conversations.flow_selected_insurance` — nullable String(120); the
  convênio label picked/typed at the insurance step, held between that step
  and booking. **Session decision, not in the original prompt doc** (which
  only specified the appointment column): the selection happens several turns
  before the booking exists, so it needs a home on `conversations` like every
  other mid-flow selection — same pattern, same lifecycle.
- `appointments.insurance` — nullable String(120); the label copied onto the
  booked row. Free text by design, never a FK (`tenants.insurances` is a JSON
  list of plan names; there is no Insurance table).

All additive/nullable, no backfill. Alembic head is now `b3d9f2a1c8e7`.

## Shared professional resolution (`services/tenant_config.py`)

- `list_active_professionals(session, tenant_id)` — THE roster query (active,
  ordered by name), now shared by the plugin's `_active_professionals` and
  the worker's flow snapshot.
- `resolve_professional_calendar(session, tenant, professional, *,
  tenant_config=None, calendar_factory=None)` — THE single implementation of
  the professional → tenant → env resolution chain (own encrypted credential →
  tenant token; own hours/services JSON → tenant's; first active resolved
  service's `duration_min` as slot length; own `google_calendar_id` →
  tenant's). Ends in `CalendarService.for_professional(...)` by default.
  - **Session decision — `calendar_factory` seam**: the prompt doc asked for
    a plain function the plugin would call, with plugin tests passing
    *unchanged*. Those tests monkeypatch `ai.tools.CalendarService`, so the
    construction step had to stay injectable: the plugin passes
    `ai.tools._calendar_for_professional` (ContextVar-scoped construction,
    exactly as before), the flow-router path passes nothing and builds
    directly. Resolution logic is single-sourced either way.
- `plugins/multi_professional.py::_professional_calendar` and
  `_active_professionals` are now thin wrappers over the shared functions —
  behavior identical, all pre-existing plugin tests unchanged and green.

## Deterministic flow (`services/flow_router.py`)

"Multi-professional" everywhere below = **2+ entries in the
active-professionals snapshot** the worker passes into `route()` (new
`professionals` param, same pre-loaded-snapshot pattern as
`upcoming_appointments`; `None`/0/1 → today's behavior, bit for bit).

- **Effective menu** (`menu_buttons_for(tenant, multi)`): for multi tenants
  the menu becomes exactly `Escolher médico` / `Procurar médico` / `Outro`
  (all ≤20 chars), replacing the configured buttons on every surface —
  greeting card (`workers/tasks.py::_greeting_buttons_for`), menu re-renders,
  reactivation reset. `Remarcar/Cancelar` is intentionally NOT a visible
  button (WhatsApp caps reply buttons at 3/message); it stays reachable by
  typing the configured manage label, matched before the menu mapping as
  always.
- **`Escolher médico`** → `STEP_AWAITING_PROFESSIONAL`: tappable list
  (`prof|<uuid>` row ids, name as title, `specialty` as the row description —
  `SlotsBubble` rows now optionally carry a third description element,
  backward compatible). Capped at 10 rows (WhatsApp limit) with a
  `flow_professional_list_truncated` warning — no pagination this round.
  `schemas/webhook.py::extract_inbound_body` mirrors the `slot|` contract for
  `prof|` ids, so a tap arrives as `"Dra. Ana (uuid)"`; resolution is by the
  embedded UUID first, typed-name fallback second (24-char truncation aware).
- **Tap** → `_enter_professional_services(professional, tenant)` (factored
  out because the PROMPT 3 hand-back calls the exact same sequence): greeting
  bubble = `specialty` (line) + `about` (verbatim), skipped entirely when both
  empty; NEVER `context_doctor_message` (LLM-internal persona text). Then THAT
  professional's services (`professional_appointment_types`, tenant fallback)
  through the existing service → confirm → day → slot → confirm mechanic. The
  worker resolves the selected professional's own `CalendarService`
  (`resolve_professional_calendar`) and passes it as `route()`'s `calendar`,
  so slot listing/booking hit the right agenda with the right duration.
- **Convênio step** (`STEP_AWAITING_INSURANCE`), between service-confirm and
  the day question, only when a professional is selected AND
  `tenant.collect_insurance` AND `tenant.insurances` non-empty: plan list
  (max 8) + fixed `Particular` + `Outro convênio` rows. "Outro convênio"
  prompts for the name and stays on the step; any other tap/text is stored
  as-is (canonicalized to the full plan name when it matches). **Informational
  only, permanently** — convênio is clinic-wide, never filters doctors or
  slots. Recorded onto `appointment.insurance` at booking.
- **`Procurar médico`** → one deterministic opener (exact text pinned in the
  prompt doc, `FIND_PROFESSIONAL_OPENER`) then sticky `FlowState.LLM` — the
  matching itself is the agent's job (below).
- The booked `appointment` dict gains `professional_id`/`insurance` keys ONLY
  in the professional branch (omitted, not None, otherwise — single-prof
  dicts stay byte-identical). Both selections are carried explicitly through
  every mid-flow `FlowRouterResult` (the caller's writes are unconditional)
  and survive LLM turns (`route()`'s LLM branch + `_preserve`).
- **Stale selection guard**: a `flow_selected_professional_id` that no longer
  resolves against the active snapshot (deactivated mid-flow) delegates to
  the LLM (catalog steps) or falls back to the menu (`resume_bubbles`) —
  never books against the wrong scope. Same spirit in the worker
  (`_flow_turn_calendar`): with a professional selected, ONLY their resolved
  calendar is used; if its resolution failed, the flow degrades to the LLM
  instead of silently using the tenant-level agenda.
- `resume_bubbles` re-renders the two new steps (professional list, insurance
  list) and is professionals/calendar-aware for the downstream ones.

## LLM handoff (`ai/`, `plugins/multi_professional.py`, `workers/tasks.py`)

- **Selected-doctor context**: `run_agent(...,
  selected_professional=<snapshot>)` overlays
  `specialty`/`about`/`context_doctor_message` onto the `TenantRuntimeConfig`
  for the turn (`graph._config_with_selected_professional`,
  `dataclasses.replace` on the frozen config) so
  `ai/prompts.py::_format_professional_context` renders exactly as it does
  for single-professional tenants. `load_tenant_config`'s
  exactly-one-active behavior is untouched; calendar resolution is untouched
  (base tools stay tenant-level).
- **`show_main_menu`** (`ai/tools.py`, in `_BASE_TOOLS`, always available):
  raises `ShowMainMenuRequested`; `run_agent` maps it to
  `SHOW_MAIN_MENU_SENTINEL` (same exception→sentinel mechanism as
  `CalendarUnavailableError`); `workers/tasks.py::_handle_show_main_menu`
  resets the flow fields to MENU via `_apply_flow_result` (which also clears
  both new selections) and sends the effective menu. **Nothing is deleted** —
  history and the patient row stay. Since PROMPT_FIX_18 the `/menu` command
  goes through this exact same handler (`source="command"`); the destructive
  wipe it used to trigger now lives behind `/dangerously-remove-context` —
  see `docs/CHECKPOINT_menu_rename_waba_lgpd.md`.
- **`select_professional_and_continue`** (plugin, addon-gated like its
  siblings): resolves the confirmed name via `_match_by_name` (unknown name →
  the same recoverable "valid options are…" error dict), then raises
  `SelectProfessionalRequested(professional_id, name)` →
  `SELECT_PROFESSIONAL_SENTINEL_PREFIX + uuid` →
  `workers/tasks.py::_handle_select_professional` re-enters the deterministic
  flow through `_enter_professional_services` (unresolvable id → menu
  fallback, never a dropped turn). This is where `Procurar médico` resolves:
  LLM recommends (1-3 doctors with reasons, from `list_professionals` which
  now also returns `about`), patient confirms, flow takes over.
- `secretary_system_prompt` gained a "MENU E ESCOLHA DE PROFISSIONAL" section
  teaching both tools (and noting the professional tools only exist for
  entitled clinics). `context_doctor_message` is never used for matching.
- **Session decision**: the worker now loads the active-professionals
  snapshot for **every resolved tenant** (not only flows-enabled ones) — the
  agent's professional context and the sentinel handlers work for
  flows-disabled tenants with the addon too. One indexed SELECT per bot turn.

## Testing

New: `tests/test_professional_resolution.py` (columns NULL-default, resolver
own-vs-fallback, factory seam), `tests/test_flow_router_multiprofessional.py`
(menu, list, greeting, insurance, e2e booking with
`professional_id`/`insurance`, stale-selection, resume),
`tests/test_agent_menu_tools.py` (tools raise, run_agent sentinel mapping,
prompt overlay, worker handlers non-destructiveness, addon gating), plus a
`prof|` case in `tests/test_webhook_parsing.py` and `about` coverage for
`list_professionals`. Adjusted (signature/pin only, no behavior change):
`test_bot_reply_gating.py` fakes accept `run_agent`'s new kwarg;
`test_agent_capability_cache.py`'s base-tool pin includes `show_main_menu`.

## Decisions made in-session (not pinned by the prompt doc)

1. `conversations.flow_selected_insurance` column added (see Schema — the
   selection needs to survive the day/slot/confirm turns).
2. `calendar_factory` seam on `resolve_professional_calendar` (see Shared
   professional resolution — keeps plugin tests unchanged, resolution
   single-sourced).
3. Professionals snapshot loaded for every resolved tenant, not only
   flows-enabled ones (see LLM handoff).
4. Professional-calendar failure degrades to the LLM instead of falling back
   to the tenant agenda (`_flow_turn_calendar`) — a wrong-agenda booking is
   worse than an LLM turn.
5. In multi mode, the tenant's *configured* button labels are not matched at
   the menu (the effective trio replaces them); typed configured labels fall
   through to the LLM like any free text. The typed manage label still works.
6. `run_agent` threads the professional context by overlaying the frozen
   config (`_config_with_selected_professional`) rather than adding a
   parameter to `secretary_system_prompt` — one rendering path, zero prompt
   API churn.

## Open follow-ups

- **4th manage surface**: a visible Remarcar/Cancelar entry for multi tenants
  would need a second message/step (WhatsApp caps reply buttons at 3) —
  deliberately not built; typing the manage label keeps working.
- **Per-tenant `Procurar médico` opener text**: the opener is fixed,
  tenant-neutral copy this round; making it configurable under
  `initial_flows` is a natural later knob.
- **>10 active professionals**: list truncates at 10 with a warning;
  pagination out of scope.
- **PROMPT 4 (dedicated `greeting_message` field)**: documented fallback
  only, explicitly not built — revisit only if real doctors' `about` text
  reads badly as a greeting in practice.
- **Branch cleanup**: `develop` intentionally left pointing at the merged
  history; deleting/keeping it is Lucas's call, not part of this run.
