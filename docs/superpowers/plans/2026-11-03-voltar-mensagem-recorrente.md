# "voltar" sends the recurring-patient message — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every time a patient types `voltar`, the secretary sends the clinic's configured "Mensagem para paciente recorrente" (`Tenant.returning_greeting_message`, `{{name}}` filled) as the body of the main-menu card, with the menu buttons under it.

**Architecture:** `voltar` already means "main menu" (`workers/shared/text.py::_MENU_COMMANDS` → `turn_router` sets `_ReplyContext.menu_requested` → `orchestrator` calls `sentinels._handle_show_main_menu`, which builds one `MenuBubble(body=menu_label(tenant), labels=main_menu_buttons())`). We keep that path and only swap the card body for the rendered recurring message when the command is `voltar` and the clinic configured one. The card keeps the main-menu labels, so `flow_runner._offer_manage_on_main_menu` still adds "Gerenciar consulta" for patients with a booking.

**Tech Stack:** Python 3.12, FastAPI + arq worker, pytest (aiosqlite harness in `tests/test_menu_command.py`), ruff.

**Spec:** the owner's request of 2026-11-03: "every time the patient types `voltar` it's mandatory to send this clinic's configured *mensagem para paciente recorrente* and then the buttons". The first-contact frame promises "⚠️ Errou? Digite *voltar* a qualquer momento para recomeçar." (`services/greeting_template.py`).

## Global Constraints

- Both channels (WhatsApp, Portal) through the shared path; never compare the channel (`tests/test_channel_branches.py`).
- Only the bare word `voltar` (whole body, trimmed, case-insensitive). `/menu`, `menu`, `/reset`, `recomeçar`, `início` keep the plain menu card.
- No clinic message configured (NULL/blank) → today's plain menu card, unchanged.
- Card body cut with `truncate_plain(..., MAX_INTERACTIVE_BODY_CHARS)` (`send_buttons` does not truncate).
- Everything `/menu` does today stays: nothing deleted, deposit void, payer-data discard, state reset to MENU.
- Touches `workers/` → report that the `secretaria-worker` needs deploy. No migration. No push/deploy in this task.

## Review Focus

- No message configured → plain menu card as before.
- `{{name}}` with no patient name → placeholder collapses (`_render_greeting_template`).
- Message longer than the card cap (legacy row) → cut, never a Meta 400.
- `voltar` mid-booking → message + menu, state reset to MENU, appointment/deposit rows untouched.
- Patient with an upcoming appointment → card still gains "Gerenciar consulta".
- `quero voltar na segunda` / `posso voltar depois?` → not a command.

---

### Task 1: `voltar` renders the recurring message as the menu card body

**Files:**
- Modify: `src/secretaria/workers/shared/text.py` (predicate next to `is_menu_command`)
- Modify: `src/secretaria/workers/shared/context.py` (field next to `menu_requested`)
- Modify: `src/secretaria/workers/turn_router.py` (the `is_menu_command(body)` block)
- Modify: `src/secretaria/workers/shared/sentinels.py` (`_handle_show_main_menu`)
- Modify: `src/secretaria/workers/orchestrator.py` (the `if reply.menu_requested:` block)
- Modify: `src/secretaria/workers/tasks.py` (re-export `is_back_command` beside `is_menu_command`)
- Test: `tests/test_menu_command.py` (reuse `_seed`, `_value`, `_FakeWhatsAppClient`)

**Interfaces:**
- Produces: `is_back_command(body: str | None) -> bool` — True only for whole-body, trimmed, case-insensitive `voltar`.
- Produces: `_ReplyContext.menu_back_greeting: bool = False`.
- Produces: `_handle_show_main_menu(..., greeting_body: str | None = None)` — when not None the menu `MenuBubble.body` is `truncate_plain(greeting_body, MAX_INTERACTIVE_BODY_CHARS)` instead of `menu_label(tenant)`; labels unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_menu_command.py`:
  - `test_is_back_command`: `"voltar"`, `"VOLTAR"`, `"  voltar  "` → True; `"menu"`, `"/menu"`, `"recomeçar"`, `"quero voltar na segunda"`, `""`, `None` → False.
  - `test_voltar_sends_returning_message_with_menu_buttons`: tenant `returning_greeting_message="Olá, {{name}}! Bem-vinda de volta à Clinic."`, patient "Maria"; inbound `voltar` → one `("buttons", WA_ID, body, buttons)` with `body == "Olá, Maria! Bem-vinda de volta à Clinic."`, labels include `"Agendar"` and `"Outro"` (and `"Gerenciar consulta"`, since `_seed` books an appointment).
  - `test_voltar_without_returning_message_sends_plain_menu`: message None → body equals `menu_label(tenant)`.
  - `test_slash_menu_never_sends_returning_message`: message configured, inbound `/menu` → body equals `menu_label(tenant)`.
  - `test_voltar_truncates_oversized_message`: 1500-char message → sent body length ≤ `MAX_INTERACTIVE_BODY_CHARS`.
  - `test_voltar_midflow_resets_state_and_keeps_rows`: `_seed(flow_state=SERVICE_CATALOG)` + message → conversation ends `FlowState.MENU`; `_snapshot` counts unchanged.

- [ ] **Step 2: Run to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_menu_command.py -q`
Expected: new tests FAIL (`is_back_command` missing / body is the default label).

- [ ] **Step 3: Implement** the interfaces. `turn_router`: pass `menu_back_greeting=is_back_command(body)` in the existing menu-command `_ReplyContext`. `orchestrator`: in the `menu_requested` branch, when `reply.menu_back_greeting` and `(tenant.returning_greeting_message or "").strip()`, set `greeting_body = _render_greeting_template(message, patient_name)` (read the name the way the nearby code does if not in scope) and pass it; else pass None. `sentinels`: body as described.

- [ ] **Step 4: Run to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_menu_command.py -q`
Expected: all PASS, pre-existing tests included.

- [ ] **Step 5: Neighbours and lint**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_greeting_template.py tests/test_channel_branches.py tests/test_workers_layering.py tests/test_lgpd_consent_gate.py tests/test_agent_menu_tools.py -q`, then `uv run ruff check` on the touched files (never `ruff format .`; lint is already red elsewhere at HEAD).
Expected: PASS, no new findings in touched files.

- [ ] **Step 6: Commit**

```bash
git add src/secretaria/workers/shared/text.py src/secretaria/workers/shared/context.py src/secretaria/workers/turn_router.py src/secretaria/workers/shared/sentinels.py src/secretaria/workers/orchestrator.py src/secretaria/workers/tasks.py tests/test_menu_command.py
git commit -m "feat(menu): 'voltar' sends the clinic's recurring-patient message with the menu buttons"
```

---

### Task 2: Whole-suite validation and checkpoint

**Files:**
- Create: `docs/CHECKPOINT_voltar_mensagem_recorrente.md`
- Modify: `CLAUDE.md` (one pointer line in "Documentação")

- [ ] **Step 1:** Run the full suite in the background with output to a file: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q`. The `test_human_backup_plugin` UTC-window flake (00–03h UTC) is pre-existing; separate it from regressions with evidence.
- [ ] **Step 2:** Write the checkpoint (function-name anchors, deploy note: worker deploy, no migration, test result) and the `CLAUDE.md` pointer.
- [ ] **Step 3: Commit** only those two files (`git add <paths>`, never `-A`).

## Self-review

- Coverage: "every `voltar`" → Task 1 on both channels; "message then buttons" → card body + labels; unconfigured fallback keeps today's menu (not requested, prevents an empty card).
- Names `is_back_command`, `menu_back_greeting`, `greeting_body` are identical in every step.
- No frontend file changes (the hub field already exists), so no browser test applies; visual proof needs a deploy, which is not authorized here.
