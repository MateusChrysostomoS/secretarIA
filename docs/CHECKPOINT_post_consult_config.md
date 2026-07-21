# CHECKPOINT — Post-consult tenant config fields

Validated 2026-07-21 (`uv run python -m pytest -q` → 773 passed, up from a 756-passed
baseline taken before this round; same 1 pre-existing `HTTP_422_UNPROCESSABLE_ENTITY`
deprecation warning as baseline, nothing new). This is PROMPT 3 of the post-consult
follow-up feature: two new tenant-level config fields, persisted + surfaced via the
doctor hub, with one of the two wired into the agent's prompt on qualifying turns.
Sending `post_consult_message` itself is **not** built here — see Pendências.

## What was built

Two new nullable `Text` columns on `tenants` (`models/tenant.py`, "Tenant config"
block, right after `persona_notes` — same non-secret, freely-hub-exposed treatment as
`greeting_message`/`persona_notes`, no `_encrypted` suffix, no entitlement gate on
save), each with a distinct job:

- **`post_consult_message`** — copy the secretary will send after a consult.
  Persisted + surfaced only; **no runtime use yet** (a later prompt wires the actual
  send).
- **`post_consult_knowledge`** — free-text reference material the LLM MAY consult to
  answer post-consult questions (recovery care, return-visit norms, how exam results
  are delivered). Injected into the system prompt, but only on turns that qualify
  (see below) — never unconditionally like `persona_notes`.

Migration `32e87fcf926b` (revises `b3d9f2a1c8e7`, the prior head) adds both columns,
nullable, no backfill. Alembic head is now `32e87fcf926b`.

Both fields flow through the existing doctor-hub config plumbing exactly like
`persona_notes`, with zero new gating:

- `schemas/config.py::TenantConfigUpdate` / `TenantConfigRead` — both fields added
  (`TenantConfigUpdate`'s cap: `max_length=4000`, same as `persona_notes`).
- `api/hub/config.py` — both names added to `_SCALAR_FIELDS` (so `PUT`'s
  `exclude_unset` partial-update loop picks them up) and to `_read_model`'s
  `TenantConfigRead(...)` construction. No entitlement check anywhere on this path
  today, and this round adds none.

### The injection seam (`post_consult_knowledge` only)

`TenantRuntimeConfig` (`services/tenant_config.py`) gained a trailing
`post_consult_knowledge` field (default `None`, so every existing construction site —
production and tests — stays valid unchanged); `load_tenant_config` passes
`tenant.post_consult_knowledge` straight through (no professional-level override, no
extra query).

`ai/graph.py::run_agent` gained `include_post_consult_knowledge: bool = False`. Right
after the existing `_config_with_selected_professional` overlay, when the caller
didn't ask for it (`include_post_consult_knowledge is False`) and the config actually
carries a value, `run_agent` blanks it for this call via
`dataclasses.replace(tenant_config, post_consult_knowledge=None)` — the same
frozen-dataclass-overlay pattern `_config_with_selected_professional` already uses.
This means the field only ever reaches the prompt on a turn that explicitly opted in.

`ai/prompts.py::_format_post_consult_knowledge(config)` renders the
"CONHECIMENTO PÓS-CONSULTA" block (or `""` when `config.post_consult_knowledge` is
falsy — which, thanks to the graph.py blanking above, is every non-qualifying turn).
`secretary_system_prompt` renders it immediately after the professional section:
persona → professional → post-consult → CONTEXTO OPERACIONAL. Same "interpreted, not
read verbatim" framing as `persona_notes`/`_format_professional_context`: the block
explicitly tells the model to use the material only when it helps answer what the
patient asked, never to recite or dump it unprompted.

### Qualifying turns (`workers/tasks.py`)

`_should_inject_post_consult_knowledge(knowledge, opening_state, flow_state,
delegated_to_llm)` is a pure decision function (no I/O) — `knowledge` blank/None
always wins (`False` regardless of everything else); otherwise the turn qualifies
when ANY of:

1. `opening_state is PatientOpeningState.JUST_HAD_CONSULT` — the patient's derived
   appointment state resolves to "just had a consult" **at this turn** (reuses
   `services/patient_context.py::resolve_patient_opening_state`, a fresh call inside
   `_send_bot_reply`, independent of the conversation-opening-greeting call site in
   `_persist_inbound_message`).
2. `flow_state is FlowState.LLM` — the conversation is already in full-LLM
   ("Outro"/deviated) mode.
3. `delegated_to_llm` — the deterministic flow router ran THIS turn and returned
   `False` (its documented contract: delegate to the LLM), e.g. the "Outro" tap
   itself.

Orchestration lives inside `_send_bot_reply`'s existing per-turn session block: right
after `tenant`/`tenant_config` resolve, `opening_state` is only queried when
`tenant.post_consult_knowledge` is non-blank, `conversation.patient_id` is known, AND
`flow_state != FlowState.LLM` (skips the extra appointment query whenever condition 2
already qualifies the turn on its own). `flow_state` itself is captured unconditionally
as soon as `conversation` resolves. `delegated_to_llm` is computed right after the
existing deterministic-flow block (`flow_snapshot is not None` at that point already
means `_run_flow` ran and returned `False`, since a `True` return exits early) and fed
into `_should_inject_post_consult_knowledge` alongside `opening_state`/`flow_state`,
whose result becomes `run_agent`'s `include_post_consult_knowledge=` argument. All DB
reads happen inside the session, before it closes, matching the file's existing
snapshot-into-locals discipline.

## Testing

- `tests/test_hub_config.py` — defaults-null assertions added to the existing GET
  test; a new disconnected-tenant PUT/GET round-trip test (proving no entitlement/
  activation gate, mirroring `test_put_greeting_only_succeeds_while_disconnected`);
  a new partial-update test (saving only `post_consult_message` leaves a
  previously-saved `post_consult_knowledge` untouched).
- `tests/test_prompts.py` — section absent when unset, present (header + body +
  the "NÃO as recite" framing) when set, and ordering with persona +
  professional sections (mirrors `test_persona_notes_and_professional_section_
  coexist_in_order`).
- `tests/test_professional_config.py` —
  `test_load_tenant_config_zero_professionals_stays_tenant_level` extended with a
  `post_consult_knowledge` passthrough assertion (cheap existing `session` fixture,
  no new harness).
- `tests/test_post_consult_injection.py` (new) — full truth-table coverage of
  `_should_inject_post_consult_knowledge`: blank/`None` knowledge always `False`;
  each of the three qualifying conditions alone is sufficient; the non-qualifying
  combination (`NEW`/`None` state, `IDLE`, not delegated) is `False`; the other
  opening states (`RETURNING_NO_APPOINTMENT`, `HAS_UPCOMING`, `HAS_UPCOMING_SOON`)
  do not qualify on their own either.

## Pendências

- **`post_consult_message` has no runtime wiring** — it is persisted and surfaced by
  the hub only. Actually sending it (when/how the secretary follows up after a
  consult) is explicitly a later feature.
- **Deploy**: apply migration `32e87fcf926b` in the deploy environment.
- **Hub UI**: the doctor-hub frontend form for both fields ships separately, in
  `brain-frontend`.
