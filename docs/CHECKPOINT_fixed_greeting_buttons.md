# CHECKPOINT — Fixed greeting buttons (product principle: LLM is the last resort)

> **Superado em parte (2026-08-13):** `flows_enabled` virou incondicional — não existe
> mais cohort flows-desligado. Ver `CHECKPOINT_flows_unconditional.md`.

> **Superseded in part (2026-08-02)**: the initial-greeting trio described here
> ([Agendar][Remarcar][Cancelar]) was consolidated to
> **[Agendar][Gerenciar consulta][Outro]** — see
> `CHECKPOINT_trio_gerenciar_scoped_help.md`. The HAS_UPCOMING(_SOON) trio, the
> flows-disabled short-circuit mechanism, and the hub-contract removal below remain
> accurate as described.

Built 2026-08-01. Product principle from the owner (2026-08-01): **the LLM is the last
resort, never the default** — conversation entry points must route deterministically
whenever possible. This round implements that at the main entry point: the conversation-
opening greeting.

Suite state: **1072 passed** (`uv run python -m pytest -q`, ran directly — no Windows App
Control block this session), up from a **1049-passed** baseline (23 new tests, 0
failures). `ruff check .` clean on every changed file (the only 8 project-wide findings
are pre-existing, in files this round never touched: `UP042` enum style on
`AppointmentStatus`/`PixDepositStatus`/`PatientOpeningState`, and two unrelated `E501`s in
`test_ehr_plugin.py`/`test_post_booking_plugin.py`).

## FASE 1 — Investigation findings

### 1. The `greeting|N` payload: full path, and how it actually diverged from the brief's working assumption

Traced handler → arq → task → router:

- `api/webhook.py` does nothing special for interactive replies — it verifies the Meta
  signature, dedupes, and enqueues the raw payload to `process_webhook_event`. No
  branching on `field`/button ids happens here.
- `workers/tasks.py::process_webhook_event` → `_handle_patient_messages` →
  `schemas/webhook.py::extract_inbound_body(msg)`. **This is the crux the brief's working
  assumption didn't anticipate**: for a plain (non-`slot|`/`prof|`) button tap,
  `extract_inbound_body` returns the button's **title** (its visible label text), not its
  `id`. The `greeting|{index}` id string was — before this round — **never read by any
  code**, anywhere (confirmed by the send-side comment it replaced: *"the label drives the
  LLM, so a positional id is enough"*). The tap becomes functionally indistinguishable
  from the patient having typed that exact label as free text.
- That label text then becomes a **new** inbound message, going through the **normal**
  `_persist_inbound_message` → `_send_bot_reply` path — i.e. "cai na rota geral de texto",
  exactly as the brief suspected. Whether it lands on the LLM depends entirely on
  `flow_router.route()`, whose very first line is:
  `if not flows_enabled(tenant): return _preserve(conversation, "delegate_llm")`.

This makes `flows_enabled(tenant)` (`Tenant.initial_flows.get("enabled")`, model comment:
*"Empty dict / `enabled` false = the legacy full-LLM path"*) the real fork in the road —
**not** a single greeting-buttons code path:

- **`flows_enabled(tenant) == True`**: `_greeting_buttons_for()` (`workers/tasks.py`) had
  **two** live branches, not one: the HAS_UPCOMING(_SOON) trio
  `[Remarcar, Cancelar, Outro]` (already deterministic, the brief's "padrão a reaproveitar"),
  and — for every other state — `menu_buttons_for(tenant, multi_professional)`. For a
  multi-doctor tenant this is a **fixed**, code-constant trio
  (`BTN_CHOOSE_PROFESSIONAL`/`BTN_FIND_PROFESSIONAL`/`Outro`); for a single-doctor tenant
  it reads `tenant.initial_flows.buttons` — tenant-configurable text, but matched
  **positionally** by `route()`'s `_menu_index` (slot 0 is always "services", slot 1 always
  "hours", slot 2/unmatched always LLM handoff — the label is decorative, not decisive).
  **This branch never read `tenant.greeting_buttons`** and was already effectively safe
  from "falls to the LLM because of free text" — see the out-of-scope note below on its
  real (milder) risk.
- **`flows_enabled(tenant) == False`** ("the legacy full-LLM path"): this is the **only**
  branch that read `tenant.greeting_buttons` (`workers/tasks.py:727`, per the brief). For
  this cohort `route()` delegates **unconditionally** to the LLM for *any* inbound text —
  greeting buttons were always decorative quick-reply suggestions for an already-all-LLM
  tenant, not a routing signal.

So the brief's premise was correct in spirit (`tenant.greeting_buttons` → free text →
LLM) but the actual trigger is narrower and sharper than "tapping a greeting button": it's
specifically **`flows_enabled(tenant) == False`**, a pre-existing, deliberate per-tenant
regime, not something intrinsic to buttons. The fix therefore had two parts: (a) stop
`tenant.greeting_buttons` from ever being read (done unconditionally), and (b) make sure
that whatever we now offer this cohort on the greeting still never reaches the LLM on tap
— which required a small, new, `flows_enabled`-independent short-circuit (see FASE 2 §2).

### 2. `reactivation_choice_buttons(tenant)` — confirmed already deterministic

`reactivation_choice_buttons(tenant)` (`services/flow_router.py`) reads
`tenant.initial_flows.reactivation.buttons` — **also** tenant-configurable free text
(default `["Sim", "Não"]`), structurally identical in shape to `initial_flows.buttons`.
But its routing (`classify_yes_no(body, tenant)`, called from
`_persist_inbound_message`'s `reactivation_origin` gate, entirely independent of
`flows_enabled`) is **purely positional**: it re-derives
`reactivation_choice_buttons(tenant)` at answer time and compares the tapped label against
`buttons[0]`/`buttons[1]` — button 0 is *always* "yes", button 1 is *always* "no", no
matter what text the tenant configured for them. A tap that matches neither degrades to
`"other"`, which the caller treats as "route normally against the preserved state" — never
a crash, never a forced LLM call. **No fix needed; this is exactly the reusable pattern
the brief pointed at.**

One **adjacent, non-behavioral** change was still necessary: the reactivation prompt's
Sim/Não buttons are sent through the *same* `_send_greeting` function as the main greeting
buttons, and used to get the *same* `f"greeting|{index}"` id scheme. Since the id is never
read for this routing (title/text only), this was harmless on its own — but it would have
collided with the new `extract_greeting_button` legacy-detection logic below (a fresh
`greeting|0` from a reactivation prompt is indistinguishable from a legacy numeric
greeting-button id). Fixed by giving reactivation's buttons a **distinct** id prefix,
`reactivation|<index>`, in `_send_greeting` — pure hygiene, zero behavior change (see
`_GREETING_ACTION_IDS` in `workers/tasks.py`).

## FASE 2 — What was built

### 1. The fixed set: payload ids, labels, routes

| Payload id (`interactive.button_reply.id`) | Label (pt-BR, exact) | Route |
|---|---|---|
| `greeting\|agendar` | **Agendar** | `flow_router.enter_booking()` — multi-doctor: doctor list (`_enter_professional_list`); single-doctor: service catalog (mirrors `_enter_menu_choice`'s index-0 branch). No services configured → deterministic "No momento não há serviços disponíveis para agendamento." (never LLM). |
| `greeting\|remarcar` | **Remarcar** | `flow_router.enter_manage_action("reschedule", …)` (pre-existing, reused as-is). No active appointment → deterministic "Você não tem nenhuma consulta agendada no momento." + the menu. |
| `greeting\|cancelar` | **Cancelar** | `flow_router.enter_manage_action("cancel", …)` (pre-existing, reused as-is). Same empty-state reply as above. |

This is `_greeting_buttons_for`'s new **unconditional default** (any tenant, any
`flows_enabled` state) whenever a greeting is configured at all and the HAS_UPCOMING(_SOON)
special case doesn't win. **No "Outro"/escape button** in this trio — WhatsApp's 3-button
cap is fully spent on the three obvious, "óbvios" candidates the brief named; per its own
instruction ("só se fizer sentido no desenho — não por padrão") an explicit escape button
wasn't added, since the pre-existing 2-step "type something → menu reshown → type again →
LLM" degrade (`route()`'s unmatched-at-IDLE/MENU fallback, unchanged) already gives a
flows-enabled patient an escape hatch without spending a button slot on it.

**Unchanged**: the HAS_UPCOMING(_SOON) special case still wins first, exactly as before —
`[Remarcar, Cancelar, Outro]` (ids `greeting|remarcar`, `greeting|cancelar`,
`greeting|outro`) when flows are enabled AND the patient has a live upcoming appointment.
"Outro" *is* the deliberate LLM escape here — pre-existing, untouched.

`route()`'s IDLE dispatch gained one new match, `LABEL_BOOK` ("Agendar"), placed
**before** the multi-doctor dispatch (same precedent as the existing
`LABEL_RESCHEDULE`/`LABEL_CANCEL_APPT` matches), so it resolves identically on single- and
multi-doctor tenants. New helper `flow_router.enter_booking(tenant, professionals)`.

### 2. Making it work for `flows_enabled(tenant) == False` too (the actual hard part)

Per FASE 1 §1, `route()` cannot help this cohort — it bails to the LLM unconditionally at
its own top line, by design (that line is this tenant's own prior opt-out of deterministic
flows; not touched by this round). Rather than either (a) leaving this cohort with no
buttons at all, or (b) doing invasive surgery on `_send_bot_reply`'s `flows_enabled`-gated
data loading to make the full booking/manage machinery reachable there too, a **third,
narrower option** was chosen, directly precedented by the existing
`_handle_action_button`/`apptresched`-while-`flows_enabled=False` branch (*"there is no
non-flow equivalent to hand off to"* → a fixed pt-BR reply, never the LLM):

- `schemas/webhook.py::extract_greeting_button(msg)` — decodes any `"greeting|<suffix>"`
  id (returns the raw suffix, or `None` if the id doesn't have that prefix).
- `workers/tasks.py::_persist_inbound_message` — a **new** short-circuit, placed **after**
  the human-handover check (unlike `action_button`, a greeting-button tap is not
  time/money-critical, so it respects an active human takeover like a normal message
  would) and **before** the reactivation-origin gate: `if greeting_button is not None and
  not flows_enabled(tenant): return _ReplyContext(greeting_button_unavailable=greeting_button)`.
  A flows-**enabled** tenant's tap is deliberately **not** touched here — it falls through
  to the normal text-routed path, which `route()` already handles deterministically.
- `workers/tasks.py::_handle_greeting_button_unavailable` — a fully self-contained handler
  (own tenant/session lookup, own tenant-scoped `WhatsAppClient`), dispatched early in
  `_send_bot_reply` exactly like `_handle_action_button`. Sends one fixed pt-BR message,
  tailored per known action when recognized, a generic fallback otherwise:
  - `agendar` → "Para agendar uma consulta, entre em contato com a nossa equipe."
  - `remarcar` → "Para remarcar sua consulta, entre em contato com a nossa equipe."
  - `cancelar` → "Para cancelar sua consulta, entre em contato com a nossa equipe."
  - anything else (a legacy numeric suffix, or any future unrecognized one) → "Não
    consigo processar esse pedido automaticamente. Entre em contato com a nossa equipe,
    por favor."

Net effect: **every** button offered on the greeting — for **every** tenant, regardless of
`flows_enabled` — routes deterministically or degrades deterministically. None can ever
reach the LLM from a single tap.

### 3. Legacy `greeting|<número>` handling

No single new "legacy detector" was needed — the two cohorts degrade through two different
(both pre-existing-or-just-built) mechanisms:

- **`flows_enabled(tenant) == True`**: nothing new required. The old free-text label (the
  clinic's own previously-configured wording, e.g. "Marcar consulta") arrives as plain
  body text, doesn't match any of the new fixed labels (nor `manage_label`/
  `initial_flows.buttons`, in general), and falls through to `route()`'s existing
  "IDLE/BUSINESS_HOURS → (re)present the effective menu" default — deterministic, no LLM,
  unchanged by this round. Regression-tested (`test_route_idle_unmatched_shows_menu`,
  pre-existing, still green).
- **`flows_enabled(tenant) == False`**: caught by the exact same short-circuit as §2 above
  — `extract_greeting_button` doesn't care whether the suffix is `"0"` or `"agendar"`, and
  an unrecognized suffix simply falls to `_GREETING_ACTION_UNAVAILABLE_DEFAULT`. Sem
  crash, sem LLM.

### 4. Hub config contract (backend is now the source of truth for the frontend agent)

- **`GET /tenants/me/config`**: the `greeting_buttons` key is **gone from the response
  entirely** (not present with an empty list, not present-but-inert — genuinely absent).
  Chose full removal over "keep it inert" specifically to give the frontend agent an
  unambiguous signal: this concept no longer exists, don't render any UI for it.
- **`PUT /tenants/me/config`**: `greeting_buttons` is no longer declared on
  `TenantConfigUpdate` at all. An incoming `greeting_buttons` key is silently dropped
  (pydantic's default "extra fields ignored" behavior — `TenantConfigUpdate` uses no
  `extra="forbid"` config anywhere in this repo) — **200 OK, not a 422**, sibling fields in
  the same request still save normally, and the DB column is left untouched.
- `MAX_GREETING_BUTTONS`/`MAX_BUTTON_LABEL_CHARS` constants are **kept** (still validate
  `initial_flows.buttons`, an unrelated, still-live field — see out-of-scope note below).
- **New (adjacent) behavior**: `greeting_message`/`returning_greeting_message` now
  **unconditionally** enforce the 1024-char WhatsApp interactive-body cap
  (`MAX_GREETING_WITH_BUTTONS_CHARS`), not just when a sibling `greeting_buttons` was also
  present in the same request (the old `_check_greeting_with_buttons` validator's
  condition — impossible to keep verbatim once the field it read no longer exists). This
  is strictly *more* correct now: since the greeting is **always** sent with fixed buttons
  attached (§1), the smaller cap is now **always** the real constraint, not just
  conditionally.
- `api/hub/config.py`'s `_SCALAR_FIELDS` tuple and `_read_model` both drop
  `greeting_buttons` accordingly.

### 5. `tenant.greeting_buttons` (the DB column)

Left in place, per instruction (no migration this round). Its model comment
(`models/tenant.py`) now says it plainly: **orphaned**, no code reads or writes it
anymore, kept only for a future cleanup migration. `scripts/apply_config.py`
(`_ALLOWED_FIELDS`) and `scripts/configs/clinica-psi-infantil.json` (the dev seed example)
were updated the same way, for the same reason — that script writes straight onto the ORM
row (bypasses the HTTP schema entirely), so leaving it in `_ALLOWED_FIELDS` would have kept
silently writing into a column nothing reads, which is exactly the kind of stale
config-writer this round is about eliminating.

## Achados fora de escopo (registrados, não corrigidos)

Per instruction — noticed while tracing `route()`'s dispatch, not touched:

1. **`initial_flows.buttons`** (`flow_router.menu_buttons(tenant)`, feeding
   `menu_buttons_for`) is tenant-configurable free text that decides the **label shown**
   for a **position-fixed** action (`_enter_menu_choice`: slot 0 is hardcoded "services",
   slot 1 is hardcoded "hours", slot 2/anything else is hardcoded LLM handoff — the
   *routing* never depends on the label text, only the label's *position*). This is
   structurally safe from the "falls to the LLM because of free text" failure mode this
   round fixes — but it IS a labeling-honesty risk: a clinic could rename slot 1 to
   something like "Fale com humano" and tapping it would still trigger the hardcoded
   "Nosso horário de atendimento…" reply, not what the new label promises. Still drives
   mid-conversation menu re-presentation (`_menu_bubbles`) and the reactivation "Não"/reset
   branch — genuinely still live, just no longer what the *opening* greeting shows.
2. **`manage_label(tenant)`** (default `"Remarcar/Cancelar"`, also
   `initial_flows.manage_label`) is matched by **exact text content** (not position) at the
   very top of `route()`'s IDLE dispatch. Low risk in practice (always dispatches to the
   same neutral `_enter_manage` action card regardless of the configured wording — no
   variable behavior hinges on the text itself), but if a clinic ever set this to a phrase
   a patient might plausibly type in ordinary conversation, that message would
   unexpectedly enter the manage flow instead of reaching the LLM. Not touched.

Neither of these reads `tenant.greeting_buttons` and neither is reachable from the opening
greeting anymore after this round, so both are lower-priority than what this round fixed —
flagged for a future pass, not fixed here.

## Testes

23 new tests, all green, 3 pre-existing tests updated to match the new (intentional)
behavior change, 0 tests skipped/deleted without replacement:

- `tests/test_webhook_parsing.py` — `extract_greeting_button`: known actions
  (`agendar`/`remarcar`/`cancelar`/`outro`), legacy numeric id, non-greeting ids return
  `None`, the reactivation `"reactivation|"` prefix returns `None` (collision guard),
  no-button message.
- `tests/test_flow_router.py` — `enter_booking` direct (single-doctor list, no-services
  empty state); `route()` + `LABEL_BOOK` (enters service catalog); `route()` +
  `LABEL_RESCHEDULE`/`LABEL_BOOK` with **zero** upcoming appointments / zero services
  (deterministic empty-state replies, never LLM).
- `tests/test_flow_router_multiprofessional.py` — "Agendar" wins before the multi-doctor
  menu dispatch (mirrors the existing Remarcar/Cancelar precedent), lands on the doctor
  list.
- `tests/test_patient_context.py` — `_greeting_buttons_for`: HAS_UPCOMING(_SOON) trio
  unchanged; every other state now gets the fixed trio (renamed from
  "...keep_menu"); a flows-**disabled** tenant now **also** gets the fixed trio (renamed
  from "...unchanged", since the behavior is now the opposite of what that name said); a
  new poisoned-column test proves `tenant.greeting_buttons` is never read on any
  state/flows combination.
- `tests/test_reactivation.py` — the IDLE re-greet offer's expected buttons updated to the
  new fixed trio (was the old tenant-menu list).
- `tests/test_bot_reply_gating.py` — `greeting_button_unavailable` dispatch: tailored text
  per known action (parametrized `agendar`/`remarcar`/`cancelar`), generic text for an
  unrecognized/legacy suffix, `run_agent` asserted **never called** (raises if invoked),
  tenant-scoped `WhatsAppClient` used, defensive no-op when the conversation/tenant can't
  resolve.
- `tests/test_action_buttons.py` — full `_persist_inbound_message` → `_send_bot_reply`
  wiring: a flows-disabled tenant's tap short-circuits end-to-end; a flows-**enabled**
  tenant's identical tap is **not** short-circuited (falls through to normal
  text-routing, `reply.inbound_body` carries the label); a greeting-button tap **respects**
  human handover (contrast with `action_button`, documented as a deliberate difference).
- `tests/test_hub_config.py` — GET omits `greeting_buttons` entirely; PUT silently drops
  it (200, not persisted, sibling field still saves); the unconditional 1024-char greeting
  cap (both fields, boundary-exact still succeeds).

## Decisões e pendências

- The fixed trio deliberately has no "Outro" slot (see FASE 2 §1) — a considered choice,
  not an oversight; revisit if product wants a one-tap escape badly enough to drop one of
  Agendar/Remarcar/Cancelar for it.
- `reactivation|<index>` id-prefix change is purely internal wire format — nothing
  patient-visible changed, no migration/deploy coordination needed.
- Nothing in this round touches `initial_flows` (mid-conversation menu, `manage_label`,
  `menu_label` display copy) or the reactivation feature's own logic — both explicitly out
  of scope (see findings above / FASE 1 §2).
- Not deployed/committed (per instructions). No DB migration in this round (the orphaned
  `greeting_buttons` column stays until a future cleanup migration).
- Frontend follow-up (separate repo, not touched here): the hub's greeting-buttons editor
  UI should be removed — `GET`/`PUT /tenants/me/config` no longer has any surface for it.
