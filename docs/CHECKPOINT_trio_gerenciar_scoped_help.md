# CHECKPOINT — Trio [Agendar][Gerenciar consulta][Outro] + LLM escopada em médico/serviço

> **Superado em parte (2026-08-13):** `flows_enabled` virou incondicional — não existe
> mais cohort flows-desligado. Ver `CHECKPOINT_flows_unconditional.md`.

Built 2026-08-01/02, direct continuation of `CHECKPOINT_fixed_greeting_buttons.md` (same
product principle: **the LLM is the last resort, never the default** — but where an LLM
step IS the right tool, it runs scoped, grounded, and bounded).

Suite state: **1106 passed** (`uv run python -m pytest -q`, no Windows App Control block
this session), up from the prior round's **1072** (34 new tests, 0 failures). `ruff
check` clean on every changed file (the only project-wide findings are the same
pre-existing ones the prior checkpoint lists: `UP042` enums + unrelated `E501`s, plus two
`E501`s in `api/hub/__init__.py`/`config.py` — none in files this round touched).
brain-frontend side: see that repo's `docs/CHECKPOINT_fixed_greeting_buttons_ui.md`
(2026-08-02 section).

## Investigation findings (the brief's "investigar antes")

1. **Free text at the initial greeting, `flows_enabled=True`**: IDLE + unmatched text →
   `route()` re-presents the menu (state flips to MENU); a second unmatched text at MENU →
   `delegate_llm` + sticky `FlowState.LLM`. So the LLM was already implicitly reachable in
   2 steps — the new "Outro" button **formalizes an existing path** into one tap rather
   than opening a new one. Better: `route()` already matched `LABEL_OTHER` at IDLE
   (added by the prior round for the HAS_UPCOMING trio), so the flows-enabled routing for
   the new button needed **zero** new code. For `flows_enabled=False` the normal path is
   already all-LLM, so "Outro" just needed an **exemption** from the greeting-button
   short-circuit (see below).
2. **WhatsApp reply-button title cap**: 20 chars (`services/whatsapp.py::send_buttons`
   truncates `title[:20]`; `_label_match` is truncation-aware). "Gerenciar consulta" = 18
   chars → fits untruncated.
3. **Existing reschedule-vs-cancel copy**: `_manage_action_card` —
   `"{consulta}\n\nO que você gostaria de fazer?"` with `[Remarcar][Cancelar][Voltar]`,
   shown after the STEP_MANAGE_PICK tap. Reused as-is: "Gerenciar consulta" enters
   `_enter_manage`, so the pick → action-card sequence IS that copy.

## What was built

### 1. Greeting trio consolidated (workers/tasks.py + services/flow_router.py)

- `_greeting_buttons_for`'s default trio is now **[Agendar, Gerenciar consulta, Outro]**
  (`LABEL_BOOK`, new `LABEL_MANAGE_APPOINTMENT`, `LABEL_OTHER`). The
  HAS_UPCOMING(_SOON) trio `[Remarcar, Cancelar, Outro]` is **untouched** (it needs no
  "Agendar", so it never had the slot-space problem).
- `route()` IDLE dispatch: `LABEL_MANAGE_APPOINTMENT` → `_enter_manage(...)` (checked
  with the other greeting labels, before the multi-doctor dispatch, so it wins on both
  single- and multi-doctor tenants). `LABEL_RESCHEDULE`/`LABEL_CANCEL_APPT` matches
  stay (the HAS_UPCOMING trio and old threads still send them).
- **`_enter_manage` gained a single-appointment shortcut** (precedent:
  `enter_manage_action`'s single branch): exactly one upcoming appointment skips the
  one-row "qual consulta?" pick list and lands straight on that appointment's
  Remarcar/Cancelar/Voltar action card — never asks what the system already knows, and
  never re-asks doctor/service. Applies to both entries into `_enter_manage` (the new
  button AND the classic configurable manage label).
- Payload id `greeting|gerenciar` added to `_GREETING_ACTION_IDS` (remarcar/cancelar
  kept for the HAS_UPCOMING trio + stale-thread taps);
  `_GREETING_ACTION_UNAVAILABLE_TEXT["gerenciar"]` = "Para remarcar ou cancelar sua
  consulta, entre em contato com a nossa equipe."
- **flows-disabled short-circuit now exempts `"outro"`**
  (`_GREETING_LLM_ESCAPE_SUFFIX` in workers/tasks.py): for that cohort the normal path
  below IS the LLM — exactly what the button promises — so only Agendar/Gerenciar (and
  legacy/unknown suffixes) degrade to the fixed "contact us" replies.
- `wants_upcoming_appointments` also matches `LABEL_MANAGE_APPOINTMENT`, so the manage
  entry has the patient's appointments loaded on the tap turn.

### 2. Scoped-help LLM nodes ("Não sei" on both catalog lists)

New module **`ai/scoped_help.py`** + flow steps in `services/flow_router.py`
(`STEP_PROFESSIONAL_HELP`/`_FINAL`, `STEP_SERVICE_HELP`/`_FINAL`, inside
SERVICE_CATALOG). Two DISTINCT nodes (professional-fit vs service-fit — different fixed
openers, different prompts), deliberately NOT the general agent and NOT the "Outro"
hand-off:

- **Entry (deterministic)**: both catalog lists now append a fixed last row "Não sei"
  (`profhelp|0` with description "Te ajudo a escolher" / `svchelp|0` — non-`prof|`/`svc|`
  prefixes on purpose so the tap arrives as the plain title). Real options cap at
  `MAX_CATALOG_OPTION_ROWS = 9` — the last of WhatsApp's 10 rows is reserved, otherwise
  `send_list`'s silent `[:10]` would drop the help row exactly when the catalog is
  fullest. The tap itself sends the fixed scope opener (`PROFESSIONAL_HELP_OPENER` /
  `SERVICE_HELP_OPENER`) and flips the step — **no LLM call on the tap**.
- **The node (one structured LLM decision per patient answer)**:
  `run_professional_help`/`run_service_help` build a scope-specific system prompt
  **grounded on the exact options snapshot the router holds** (professionals'
  name/specialty/about — never `context_doctor_message`; services'
  name/price/description), re-read the last 10 conversation messages (stateless, same
  pattern as `run_agent`; `conversation_id` rides on the worker's conv snapshot for
  this), and force a `_ScopedHelpDecision` via `with_structured_output` — **pick**
  (exact option name) / **clarify** (one short question) / **escalate**. Never free
  text the router would re-interpret.
- **Hand-back (validated, structural)**: a pick is re-validated against the router's own
  snapshot via the same matchers a direct tap uses (`_match_professional` /
  `_match_service`); success re-enters the deterministic flow through
  `_enter_professional_services` (doctor greeting + THEIR services — the exact
  hand-back `select_professional_and_continue` already used) or the new
  `_enter_service_detail` (factored out of the STEP_AWAITING_SERVICE tap branch; the
  service-detail confirm card). An unresolvable pick is never offered to the patient →
  escalate.
- **Bound (enforced in code, not prompt)**: one clarify allowed. The clarify reply arms
  the `*_FINAL` step; any clarify outcome there — or a malformed decision, or an empty
  catalog (short-circuits without an LLM call) — collapses to **escalate**:
  `SCOPED_HELP_ESCALATE_MESSAGE` + the new `FlowRouterResult.action="handover"`.
- **`action="handover"`** (`_apply_flow_result`): persists the flow reset (IDLE), flips
  the conversation to `HandoverManager.set_human_active` (new
  `_set_conversation_human_active` helper — handover FIRST, then the message, mirroring
  `_handle_calendar_unavailable`'s order; NO owner email — nothing is broken, the
  secretary sees the chat in their own WhatsApp app), then sends the escalation
  message. The handover-timeout cron later hands back to the bot as usual.
- **Failure degrade**: an exception from the scoped node (provider/network) →
  `_preserve(conversation, "delegate_llm")` — the general agent picks the turn up with
  history intact. `resume_bubbles` doesn't know the help steps → existing
  menu-fallback default (deterministic).

### 3. Frontend (brain-frontend, separate repo)

`configuracao/components/MessagesSection.tsx`'s read-only `FIXED_GREETING_BUTTONS`
chips → `["Agendar", "Gerenciar consulta", "Outro"]`, caption updated (Outro = conversa
livre); comment-only updates in `lib/secretaria-hub.ts` + `docs/VERIFICATION_onboarding.md`.
tsc clean, build green, vitest 69/69. No wire change (the greeting-buttons field was
already gone from the contract).

## Testes (34 new / updated across the two repos' concerns)

- `test_flow_router.py` — "Gerenciar consulta" at IDLE: single → direct action card,
  2+ → pick list, empty → deterministic empty-state; "Não sei" service row (present,
  last, cap-reserved at 10); service-help node monkeypatched: pick→detail-card
  hand-back (incl. grounding assertions), invalid pick→handover, clarify→`_FINAL`,
  clarify-on-final→handover, escalate→handover, node crash→delegate_llm preserving
  state. `_enter_manage`'s single-appointment shortcut also covered via the classic
  manage label (2 updated tests + 1 new).
- `test_flow_router_multiprofessional.py` — "Gerenciar consulta" precedence before the
  multi-doctor dispatch; "Não sei" professional row (+ description, cap); professional
  help: pick→doctor greeting+services hand-back, invalid pick→handover,
  clarify→final→escalate (asserting the `final_round` flags the node saw), crash→
  delegate_llm; service help grounded on the SELECTED professional's own catalog, with
  the selection surviving the hand-back.
- `test_scoped_help.py` (new) — prompts list only the real options (and are
  scope-distinct); `_normalize` collapses malformed/final-round decisions to escalate;
  empty catalog escalates **without building the model**; message assembly
  (system+history fallback) with a fake structured-output model.
- `test_action_buttons.py` — flows-disabled `gerenciar` tap short-circuits end-to-end
  (tailored copy); flows-disabled `outro` tap is NOT short-circuited (falls through as
  plain "Outro").
- `test_bot_reply_gating.py` — "gerenciar" added to the tailored-reply params; new
  `_apply_flow_result` handover test (message sent + HUMAN_ACTIVE + flow reset).
- `test_patient_context.py` / `test_reactivation.py` / `test_webhook_parsing.py` —
  trio expectations updated; "gerenciar" in the known-suffix list.

## Decisões e pendências

- The 1-appointment shortcut intentionally also changes the classic manage label's
  behavior (one appointment no longer shows a one-row pick list) — consistent with the
  product principle; the neutral pick→action-card sequence is preserved for 2+.
- Escalation = real human handover (bot silenced via the existing Coexistence
  mechanism), not a dead-end message. The handover cron's timeout is the hand-back.
- `ai/scoped_help.py` reads DB (history) from a route()-called path — same "network
  call, own short session, no caller transaction" contract the calendar calls already
  have; `flow_router` still opens no transaction of its own.
- The multi-doctor menu's "Procurar médico" (sticky-LLM full agent + FIND_PROFESSIONAL_OPENER)
  is a different, pre-existing entry point — deliberately untouched; the "Não sei" row on
  the doctor LIST is the new scoped node.
- Not committed/deployed (per instructions). No DB migration in this round (the help
  steps live in the existing `flow_step` string column).
- Live e2e against a deployed mesh (real WhatsApp taps + a real OpenAI structured-output
  call) not run this round — same caveat as the prior checkpoint.
