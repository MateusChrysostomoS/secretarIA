# R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The staff console never lists, counts, searches or opens a PreCheck conversation, and the clinic's extra reminder is configured as "N days before, at HH:MM" in the clinic's time zone instead of "X minutes before".

**Architecture:** Part A (secretarIA) adds two tenant columns (`reminder_extra_days_before`, `reminder_extra_send_time`) with an additive migration that converts the legacy minutes, puts the due-time rule in one pure module (`core/extra_reminder.py`, zoneinfo, DST-safe) used by both the planner (`schedule_reminders`) and the replan (`replan_custom_reminders`), and exposes the pair on the hub configuration wire while still accepting the legacy minutes from an older screen. Part B (Brain-Message-Frontend) drops the console's own call to PreCheck (the only way PreCheck threads entered the console), refuses `precheck:` ids as "not found", removes the now-dead PreCheck branches of the console UI, and replaces the minutes select with a days select + a time select.

**Tech Stack:** secretarIA — Python 3.12, FastAPI, SQLAlchemy async, Alembic, Pydantic v2, pytest (asyncio auto), zoneinfo. Front — Next.js 15 static export, React 19, TypeScript, vitest (node).

**Spec:** `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §6 (decisions of 2026-10-10; §6.1 PreCheck out of the console, §6.2 extra reminder by day + time). §5.B (replan rules) and §5.D (Meus pacientes) still apply where §6 does not override them.

**Task id:** TASK-048 (TASK-047 is already the R5/R7/R8 release task, `C:\TECH\BRAIN\tasks\TASK-047\`). Worktrees `C:\TECH\BRAIN-worktrees\TASK-048\secretarIA` and `C:\TECH\BRAIN-worktrees\TASK-048\Brain-Message-Frontend`.

**Status and base (2026-10-10):** PLAN ONLY — the owner asked not to execute it until a new explicit request. R5/R7/R8 were published by TASK-047: secretarIA `origin/main` **f6bba47** (contains the whole TASK-046 API branch incl. afb865a) and Brain-Message-Frontend `origin/main` **89995bf** (contains the TASK-046 front branch incl. fecfd29). When executed, cut both TASK-048 worktrees from `origin/main` of each repo (re-check that it still contains those SHAs), not from the TASK-046 branches.

**Owner confirmations (2026-10-10, spec §6.3):** extra reminder 2..14 days, 06:00..22:00 in 15-minute steps (D1/D2 confirmed); PreCheck **results** stay for the clinic (Anamneses + "Status do PreCheck" badge — D18 confirmed), only PreCheck **conversations** leave the staff console; PreCheck keeps appearing to the **patient** in the patient portal (unchanged). The legacy `reminder_extra_lead_minutes` column is dropped in a later migration once everything runs R9 (follow-up, not in R9 — see D20).

## Global Constraints

- Never push, never merge into `main`, never deploy, never run a migration against a real database. Commits are local, on the task branches only.
- Do NOT touch PreCheck (its repo, its n8n, its WhatsApp production flow) nor brain-api. The console change is front-only (decision D12).
- Every commit message ends with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Stage files by name (`git add <paths>`), never `git add -A` / `git add .` (parallel sessions share worktrees).
- secretarIA test command (App Control blocks the venv python; the interpreter is reused from TASK-046):
  `PYTHONPATH="src;.;C:/TECH/BRAIN-worktrees/TASK-046/secretarIA/.venv/Lib/site-packages" BOT_ALLOWLIST_WA_IDS="" /c/Users/mateu/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest <file> -q`
  (abbreviated `PYTEST <file>` below — always expand it). Run from `C:/TECH/BRAIN-worktrees/TASK-048/secretarIA`.
- secretarIA lint on touched files only: `uvx ruff check <files>` and `uvx ruff format --check <files>`. Never `ruff format .` (the repo is not fully formatted at HEAD).
- Front gates, run from `C:/TECH/BRAIN-worktrees/TASK-048/Brain-Message-Frontend`: `npm test` (or `npm test -- <file>`), `npm run typecheck`, `npm run build`. No ESLint (family decision; `npm run lint` must not be run).
- Front conventions: `C:/TECH/.claude/skills/front-brain/SKILL.md`; every new form control goes through `components/primitives/Select.tsx` (it renders a real `<label htmlFor>` — `custom-control-accessible-name` skill). UI copy in Portuguese; code, identifiers and comments in English.
- Extra reminder rules (spec §6.2), copied verbatim as constants: days **2..14**; time **06:00..22:00 in 15-minute steps**; off = both null; never closer than **25 h** to the appointment; computed on the clinic's local calendar in `Tenant.timezone` (IANA).
- PreCheck rule (spec §6.1): no PreCheck conversation in the console for any role; a `precheck:` id answers 404 "Conversa não encontrada."
- Deploy order (not executed here): `alembic upgrade head` from the new secretarIA image → `secretaria_api` AND `secretaria-worker` together (`GET /build` parity) → Brain-Message front.
- Talking to the owner: plain lay Portuguese, no code names (AI_WORKFLOW.md "Como falar com o dono").

## Review Focus

1. **A clinic in a time zone with daylight saving, appointment right after the change** — the reminder must leave at the chosen local hour, not one hour off. Pinned: Task A1 `test_dst_change_between_reminder_and_appointment_keeps_the_local_hour`.
2. **An appointment late at night (local date differs from the UTC date)** — "2 dias antes" counts on the clinic's calendar. Pinned: Task A1 `test_days_count_on_the_clinic_calendar_not_utc` and Task A3 `test_a_late_night_appointment_counts_days_on_the_local_calendar`.
3. **The R7 screen still in production after the new API is deployed** (it sends `reminder_extra_lead_minutes` on every save) — must neither 422 nor reset the chosen hour. Pinned: Task A4 `test_a_legacy_lead_from_an_older_screen_is_translated_keeping_the_hour` and `test_saves_that_change_nothing_do_not_replan` (legacy echo case).
4. **The clinic changes its time zone after reminders were planned** — pending extra reminders move to the new local hour. Pinned: Task A3 `test_a_new_time_zone_moves_the_pending_extra_reminder` and Task A4 `test_changing_the_time_zone_replans_in_the_same_save`.
5. **A bookmarked or pasted `/chat?conversa=precheck:…` link** — must show "conversa não encontrada" and never call PreCheck. Pinned: Task B1 `a pasted PreCheck id is 'not found' everywhere, before any request`.

---

## Decisions taken where the spec is silent

| # | Decision | Why / cost if wrong |
|---|---|---|
| D1 | Days range **2..14**. | "1 dia antes" duplicates the fixed 1-day reminder; 14 = R7's old ceiling (20160 min). Cost: a clinic wanting "1 dia antes às 18h" cannot — owner can widen later (constant + validator). |
| D2 | Time **06:00..22:00, 15-minute steps**. | The clinic picks the hour; night messages wake patients. 15-minute grid keeps the select short (65 options). |
| D3 | Due = (local date of the appointment start in `Tenant.timezone`) − N days, at HH:MM local, `datetime.combine(..., tzinfo=ZoneInfo)` then `.astimezone(UTC)`; skipped when `start − due < 25 h`. | 25 h = R7's 1500-minute floor (1 h before the fixed day reminder), so the R4 custom warning (`due + 2 h`) stays where R7 put it. With D1+D2 the guard is unreachable in practice (minimum gap is exactly 25 h on a spring-forward night) — kept as defence for hand-edited rows; a test proves "unreachable" over all allowed settings. |
| D4 | Past at planning/replan time → not created / cancelled (unchanged R1/R7 rule). | Never sent late. |
| D5 | Invalid or missing `Tenant.timezone` → UTC (same fallback as `plugins/reminders.py`). | The schema already refuses unknown zones on write; only a hand-edited row hits this. |
| D6 | DST edge times: a non-existent local time (spring-forward gap) uses `fold=0` (lands one hour later in wall-clock terms); an ambiguous one (fall-back) takes the first occurrence. | Unreachable in Brazil (no DST since 2019) and with D2's 06:00..22:00 window in the usual 00:00–03:00 transitions. |
| D7 | Storage: `reminder_extra_days_before INTEGER NULL`, `reminder_extra_send_time VARCHAR(5) NULL` ("HH:MM"). Both null = off; a half-set row reads as off. No DB CHECK. | Same "HH:MM" string the front and `business_hours` already use; no Time type round-trip differences between SQLite and Postgres. |
| D8 | Legacy `reminder_extra_lead_minutes` column **kept** (widen before, narrow after): no longer read by R9 code, **mirrored as days × 1440 on every save**. Drop it in a later migration once API and worker both run R9. | A code rollback then still plans on the same day. Cost: one redundant column for a while. |
| D9 | Migration converts legacy values: `days = ceil(lead/1440)` clamped to 2..14, time `09:00`, only where `lead > 0`. Already-planned pending rows are **not** recomputed by the migration; they move at the next save of the reminder setting. | Keeps the reminder on the same day or a little earlier. R7 is being released now (TASK-047), so some clinics may have values. Re-saving replans; the owner turns reminders on per clinic. |
| D10 | Wire: `reminder_extra_days_before` + `reminder_extra_send_time` travel **together** on PUT (both null = off, both set = on); one without the other, or a null paired with a value → 422. | No "half-saved" state; the new screen always sends both. |
| D11 | Backward tolerance: PUT still accepts `reminder_extra_lead_minutes` (same 1500..20160 / null validation as R7) and translates it with the D9 rule, **keeping the stored hour** (09:00 when none). When the new pair is also present, the pair wins and the legacy key is ignored. GET keeps returning `reminder_extra_lead_minutes` (the mirror). | The R7 screen sends the lead on every save of any section; with the mirror it round-trips to "no change" (no replan, hour kept). |
| D12 | Replan triggers when days, time **or `timezone`** changed in the save. | A time-zone change moves local-time reminders. |
| D13 | PreCheck threads entered the console only through the front's own call `GET /api/precheck/precheck-sessions` (and `/precheck-sessions/{ref}/messages`) with a brain-api-minted PreCheck token (`lib/real/console-api.real.ts::precheckList/precheckThread`, `lib/real/precheck-hub.ts`). secretarIA's `/tenants/me/conversations` never served PreCheck. So the removal is **front-only**; no backend change is needed. | Owner rule: PreCheck production untouchable. |
| D14 | Deleted with it: `lib/real/precheck-hub.ts` and `brain-session.ts::mintPrecheckToken` (no other caller in this repo). brain-api's `/sso/precheck/token` is untouched (other consumers). `precheck-base.ts` stays (the patient portal's Bristol picture). | Dead client code removed. |
| D15 | A `precheck:` id is refused with `ConsoleApiError(404, "conversation_not_found", "Conversa não encontrada.")` **before any request**, in `getConversation`, `listMessages`, `getPatientContext`, `sendMessage`, `setConversationMode`; `getTyping`/`sendTyping` keep their silent no-op. ChatScreen already turns a 404 into "conversa não encontrada". | Same answer as an id that does not exist (spec §6.1). |
| D16 | `ConversationProduct` narrowed to `"secretaria"`; the console's PreCheck row badge, read-only thread branch, `PRODUCT_*` labels, ModeSwitch `automationLabel` prop and the R8 filter's PreCheck line are removed. The `product` field stays (the typing indicator speaks per product). | "Simplify, don't leave dead branches" — tsc then flags any comparison with `"precheck"`. |
| D17 | `mappers.ts::toPrecheckMessages` (+ its wire types and tests) is **kept**: no production caller any more, but its tests are the only coverage of the card helpers the patient portal shares (`precheckInteractive`, `precheckReplyTo`, `precheckAskedBy`, `precheckImage`). Only the console listing/addressing helpers are deleted. | Cost: one function without a production caller; moving those tests onto the portal path is a follow-up. |
| D18 | Out of scope (unchanged): the **Anamneses** module (questionnaire results, not conversations), the "Status do PreCheck" badge in a secretarIA conversation's context panel, the patient portal's PreCheck tab, and the mock `c-ana` bubble whose sender is `precheck` (a message inside a secretarIA conversation). The mock **PreCheck conversation** `c-camila` is removed. | Spec §6.1 last bullet. |
| D19 | Front: a stored value outside the grid (e.g. days 1, time 07:10 set by hand) is shown in place in its select and is **not re-sent** on save (absent = unchanged), like R5's legacy-lead rule. Turning the reminder on from "Desligado" pre-selects 09:00. | A save never 422s because of a value the clinic did not touch. |
| D20 | The legacy column `reminder_extra_lead_minutes` stays in R9 (kept in sync as days × 1440 for rollback safety) and is **dropped in a later migration** once API, worker and front all run R9 — owner confirmed 2026-10-10. | Not part of R9; recorded as a pending item in both checkpoints (A5, B4). |

## Produces for Part B — exact wire shapes

`GET /tenants/me/config`, `GET /tenants/me/configuration` (`tenant` object) and the PUT echoes (`TenantConfigRead`), additive:

```json
{
  "reminders_v2_enabled": true,
  "reminder_extra_days_before": 5,
  "reminder_extra_send_time": "09:00",
  "reminder_extra_lead_minutes": 7200
}
```

Off: `"reminder_extra_days_before": null, "reminder_extra_send_time": null, "reminder_extra_lead_minutes": null`. A backend older than R9 omits both new keys (front: section hidden, never written). `reminder_extra_lead_minutes` is legacy (= days × 1440) — the R9 front ignores it.

`PUT /tenants/me/configuration` body `{"tenant": {...}}` and `PUT /tenants/me/config` body:

```json
{ "reminder_extra_days_before": 3, "reminder_extra_send_time": "08:15" }
{ "reminder_extra_days_before": null, "reminder_extra_send_time": null }
```

Both keys together or neither. Days int 2..14; time `"HH:MM"`, 06:00..22:00, minutes ∈ {00,15,30,45}. Anything else → 422 (FastAPI default body), nothing saved. A change of days, time or `timezone` replans pending extra reminders in the same transaction.

---

## File Structure

**Part A — secretarIA (`C:/TECH/BRAIN-worktrees/TASK-048/secretarIA`)**

| File | Responsibility |
|---|---|
| Create `src/secretaria/core/extra_reminder.py` | The pure rule: constants, `parse_send_time`, `is_valid_send_time`, `clinic_zone`, `custom_due_at`, `from_legacy_lead`, `legacy_lead_minutes`. No DB, no I/O. |
| Create `tests/test_extra_reminder.py` | Pure tests of the rule (DST, local calendar, guard, ranges). |
| Create `migrations/versions/a4c7e2f9b1d3_extra_reminder_day_time.py` | Two additive columns + legacy conversion. |
| Create `tests/test_migration_extra_reminder_day_time.py` | Head, upgrade/downgrade, conversion agrees with `from_legacy_lead`. |
| Modify `src/secretaria/models/tenant.py` | Map the two columns. |
| Modify `tests/_reminder_fixtures.py` | Fixture clinics use the pair (5 days at 09:00) + the mirror. |
| Modify `tests/test_migration_clinic_action_notices.py` | R7 is no longer the head. |
| Modify `src/secretaria/services/reminder_schedule.py` | `schedule_reminders` and `replan_custom_reminders` use `custom_due_at`. |
| Create `tests/test_extra_reminder_schedule.py` | Planner integration. |
| Modify `tests/test_reminder_lead_replan.py` | Replan by days/time/time zone. |
| Modify `tests/test_reminder_schedule.py:75` | Turn the extra reminder off via the new column. |
| Modify `src/secretaria/schemas/config.py` | Update/Read schemas for the pair. |
| Modify `src/secretaria/services/hub_configuration.py` | Apply the pair or the legacy lead; replan on change; read model. |
| Rename+rewrite `tests/test_hub_config_reminder_lead.py` → `tests/test_hub_config_extra_reminder.py` | Wire tests. |
| Create `docs/CHECKPOINT_lembretes_r9.md`; modify `docs/CHECKPOINT_lembretes_r7.md` | Docs. |

**Part B — Brain-Message-Frontend (`C:/TECH/BRAIN-worktrees/TASK-048/Brain-Message-Frontend`)**

| File | Responsibility |
|---|---|
| Modify `lib/real/console-api.real.ts` | secretarIA-only list; `refusePrecheckId`. |
| Delete `lib/real/precheck-hub.ts`; modify `lib/real/brain-session.ts` | Dead PreCheck client. |
| Modify `lib/real/mappers.ts`, `lib/__tests__/real-mappers.test.ts` | Drop console listing/addressing helpers. |
| Modify `lib/conversations.ts`, `lib/__tests__/conversations.test.ts` | R8 filter without the PreCheck line. |
| Create `lib/real/__tests__/console-no-precheck.test.ts` | Real-client behaviour. |
| Modify `lib/types.ts`, `components/console/ConversationRow.tsx`, `components/console/Thread.tsx`, `components/console/ModeSwitch.tsx`, `components/console/console.css`, `lib/text.ts`, `lib/__tests__/text.test.ts`, `lib/mock/data.ts` | Console UI without PreCheck branches. |
| Modify `lib/contexto/reminders.ts`, `lib/contexto/types.ts`, `lib/contexto/hub-mapping.ts`, `lib/contexto/snapshot.ts`, `lib/contexto/screen.ts`, `lib/real/secretaria-hub.ts`, `components/contexto/ReminderSection.tsx`, `components/contexto/ContextoView.tsx`, tests `lib/contexto/__tests__/reminders.test.ts`, `components/contexto/__tests__/reminder-section.test.tsx` | Day + time UI. |
| Create `docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`; modify `docs/CHECKPOINT_console_precheck.md`, `docs/CHECKPOINT_meus_pacientes_seletor_medico.md`, `docs/CHECKPOINT_brain_message_lembretes_agenda.md` | Docs. |

---

## Setup (before Task A1)

- [ ] **Step 1: Create the two worktrees** (base = `origin/main` of each repo, which holds the TASK-047 release of R5/R7/R8 — secretarIA f6bba47 / front 89995bf or later; `git fetch origin` first and record the base SHA. The commands below were written against the TASK-046 branches: replace the last argument `task/TASK-046-…` with `origin/main`).

```bash
git -C C:/TECH/BRAIN-worktrees/TASK-046/secretarIA worktree add C:/TECH/BRAIN-worktrees/TASK-048/secretarIA -b task/TASK-048-lembrete-dia-hora-api task/TASK-046-meus-pacientes-api
git -C C:/TECH/BRAIN-worktrees/TASK-046/Brain-Message-Frontend worktree add C:/TECH/BRAIN-worktrees/TASK-048/Brain-Message-Frontend -b task/TASK-048-sem-precheck-lembrete-dia-hora-front task/TASK-046-meus-pacientes-front
```

- [ ] **Step 2: Bring the spec edit and this plan into the new secretarIA worktree and commit them** (they are uncommitted in the TASK-046 worktree).

```bash
cp C:/TECH/BRAIN-worktrees/TASK-046/secretarIA/docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md C:/TECH/BRAIN-worktrees/TASK-048/secretarIA/docs/superpowers/specs/
mkdir -p C:/TECH/BRAIN-worktrees/TASK-048/secretarIA/docs/superpowers/plans
cp C:/TECH/BRAIN-worktrees/TASK-046/secretarIA/docs/superpowers/plans/2026-10-10-lembretes-r9-sem-precheck-e-lembrete-dia-hora.md C:/TECH/BRAIN-worktrees/TASK-048/secretarIA/docs/superpowers/plans/
cd C:/TECH/BRAIN-worktrees/TASK-048/secretarIA
git add docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md docs/superpowers/plans/2026-10-10-lembretes-r9-sem-precheck-e-lembrete-dia-hora.md
git commit -m "docs: R9 owner decisions (spec §6) and plan (TASK-048)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Front dependencies.** In `C:/TECH/BRAIN-worktrees/TASK-048/Brain-Message-Frontend` run `npm ci`. Then `npm test` and `npm run typecheck` once to record the baseline (expected: green; record counts).

- [ ] **Step 4: secretarIA baseline.** `PYTEST tests/test_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py` → all pass (record count).

---

# Part A — secretarIA

### Task A1: The pure rule `core/extra_reminder.py`

**Files:**
- Create: `src/secretaria/core/extra_reminder.py`
- Test: `tests/test_extra_reminder.py`

**Interfaces:**
- Consumes: nothing (stdlib only; the tenant is duck-typed).
- Produces (later tasks rely on these exact names):
  - `EXTRA_DAYS_MIN: int = 2`, `EXTRA_DAYS_MAX: int = 14`, `DEFAULT_SEND_TIME: str = "09:00"`, `MIN_LEAD: timedelta = timedelta(hours=25)`
  - `parse_send_time(value: object) -> datetime.time | None`
  - `is_valid_send_time(value: object) -> bool`
  - `clinic_zone(name: str | None) -> ZoneInfo`
  - `custom_due_at(tenant, start: datetime) -> datetime | None` — `tenant` has `.timezone`, `.reminder_extra_days_before`, `.reminder_extra_send_time`; returns an aware UTC datetime.
  - `from_legacy_lead(lead: int | None, *, current_send_time: str | None) -> tuple[int | None, str | None]`
  - `legacy_lead_minutes(days: int | None) -> int | None`

- [ ] **Step 1: Write the failing tests**

```python
"""The clinic's extra reminder: "N dias antes, às HH:MM" in its time zone (TASK-048 R9, spec §6.2)."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from secretaria.core.extra_reminder import (
    DEFAULT_SEND_TIME,
    EXTRA_DAYS_MAX,
    EXTRA_DAYS_MIN,
    MIN_LEAD,
    custom_due_at,
    from_legacy_lead,
    is_valid_send_time,
    legacy_lead_minutes,
    parse_send_time,
)


def clinic(days, send_time, timezone="America/Sao_Paulo"):
    return SimpleNamespace(
        timezone=timezone,
        reminder_extra_days_before=days,
        reminder_extra_send_time=send_time,
    )


def test_n_days_before_at_the_chosen_local_hour():
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # 09:00 in São Paulo
    assert custom_due_at(clinic(5, "08:30"), start) == datetime(2026, 10, 6, 11, 30, tzinfo=UTC)


def test_days_count_on_the_clinic_calendar_not_utc():
    start = datetime(2026, 10, 11, 1, 30, tzinfo=UTC)  # 2026-10-10 22:30 in São Paulo
    assert custom_due_at(clinic(2, "09:00"), start) == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_dst_change_between_reminder_and_appointment_keeps_the_local_hour():
    # New York leaves DST on 2026-11-01. Appointment 2026-11-03 10:00 EST (UTC-5);
    # the reminder on 2026-10-31 09:00 is still EDT (UTC-4) -> 13:00 UTC, not 14:00.
    start = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    due = custom_due_at(clinic(3, "09:00", "America/New_York"), start)
    assert due == datetime(2026, 10, 31, 13, 0, tzinfo=UTC)
    assert due.astimezone(ZoneInfo("America/New_York")).time() == time(9, 0)


def test_a_naive_start_is_read_as_utc():
    start = datetime(2026, 10, 11, 12, 0)
    assert custom_due_at(clinic(5, "08:30"), start) == datetime(2026, 10, 6, 11, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("days", "send_time"),
    [(None, "09:00"), (3, None), (None, None), (0, "09:00"), (-2, "09:00"), (3, "25:00"), (3, "9:00")],
)
def test_off_or_unusable_settings_mean_no_extra_reminder(days, send_time):
    assert custom_due_at(clinic(days, send_time), datetime(2026, 10, 20, 12, 0, tzinfo=UTC)) is None


def test_closer_than_25_hours_never_fires():
    # Only reachable by a hand-edited row (days below the allowed range).
    start = datetime(2026, 10, 11, 3, 0, tzinfo=UTC)  # 00:00 in São Paulo
    assert custom_due_at(clinic(1, "22:00"), start) is None


def test_an_unknown_time_zone_falls_back_to_utc():
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert custom_due_at(clinic(2, "09:00", "Mars/Olympus"), start) == datetime(
        2026, 10, 9, 9, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("zone", ["America/Sao_Paulo", "America/New_York"])
@pytest.mark.parametrize("base", ["2026-10-11", "2026-03-09", "2026-11-02"])
def test_every_allowed_setting_is_at_least_25_hours_before_any_start(zone, base):
    tz = ZoneInfo(zone)
    day = datetime.fromisoformat(base)
    times = [f"{h:02d}:{m:02d}" for h in range(6, 23) for m in (0, 15, 30, 45) if (h, m) <= (22, 0)]
    for minute_of_day in range(0, 24 * 60, 15):
        start = (day + timedelta(minutes=minute_of_day)).replace(tzinfo=tz).astimezone(UTC)
        for send_time in times:
            due = custom_due_at(clinic(EXTRA_DAYS_MIN, send_time, zone), start)
            assert due is not None
            assert start - due >= MIN_LEAD


@pytest.mark.parametrize("value", ["06:00", "09:15", "12:30", "21:45", "22:00"])
def test_send_times_on_the_grid_are_valid(value):
    assert is_valid_send_time(value) is True


@pytest.mark.parametrize(
    "value", ["05:45", "22:15", "10:10", "24:00", "9:00", "09:00:00", "", None, 900, "٠٩:٠٠"]
)
def test_send_times_off_the_grid_are_invalid(value):
    assert is_valid_send_time(value) is False


def test_parse_send_time_reads_any_clock_time():
    assert parse_send_time("07:10") == time(7, 10)
    assert parse_send_time("23:59") == time(23, 59)
    assert parse_send_time("24:00") is None


@pytest.mark.parametrize(
    ("lead", "expected_days"),
    [(None, None), (0, None), (-5, None), (720, 2), (1500, 2), (2880, 2), (2881, 3), (7200, 5),
     (20160, 14), (30000, 14)],
)
def test_legacy_minutes_become_whole_days_rounded_up_within_range(lead, expected_days):
    days, send_time = from_legacy_lead(lead, current_send_time=None)
    assert days == expected_days
    assert send_time == (DEFAULT_SEND_TIME if expected_days else None)


def test_legacy_translation_keeps_a_valid_stored_hour():
    assert from_legacy_lead(4320, current_send_time="07:30") == (3, "07:30")
    assert from_legacy_lead(4320, current_send_time="03:00") == (3, DEFAULT_SEND_TIME)


def test_the_mirror_is_days_times_1440():
    assert legacy_lead_minutes(5) == 7200
    assert legacy_lead_minutes(None) is None
    assert (EXTRA_DAYS_MIN, EXTRA_DAYS_MAX) == (2, 14)
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST tests/test_extra_reminder.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'secretaria.core.extra_reminder'`.

- [ ] **Step 3: Write the module**

```python
"""The clinic's extra reminder: "N dias antes, às HH:MM" in its time zone (TASK-048 R9).

Spec docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §6.2.
The ONE place the rule is written: the planner (services/reminder_schedule.py), the
hub configuration (schemas/config.py, services/hub_configuration.py) and the R9
migration's SQL (same legacy conversion) all follow it. Pure: no DB, no I/O.

The due time is computed on the clinic's LOCAL calendar - the appointment's local
date minus N days, at HH:MM local - and only then converted to UTC, so a daylight
saving change between the two never shifts the hour the patient receives it.
"""

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXTRA_DAYS_MIN = 2
EXTRA_DAYS_MAX = 14
SEND_TIME_EARLIEST = time(6, 0)
SEND_TIME_LATEST = time(22, 0)
SEND_TIME_STEP_MINUTES = 15
DEFAULT_SEND_TIME = "09:00"
# Never closer to the appointment than one hour before the fixed 1-day reminder (the
# 1500-minute floor R7 had). With the ranges above this only bites a hand-edited row.
MIN_LEAD = timedelta(hours=25)
FALLBACK_ZONE = "UTC"
_MINUTES_PER_DAY = 1440
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def parse_send_time(value: object) -> time | None:
    """'HH:MM' (24 h, two digits each, ASCII) -> time; anything else -> None."""
    if not isinstance(value, str):
        return None
    match = _CLOCK.fullmatch(value)
    if match is None:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def is_valid_send_time(value: object) -> bool:
    """What a clinic may choose: 06:00..22:00 in 15-minute steps."""
    parsed = parse_send_time(value)
    return (
        parsed is not None
        and parsed.minute % SEND_TIME_STEP_MINUTES == 0
        and SEND_TIME_EARLIEST <= parsed <= SEND_TIME_LATEST
    )


def clinic_zone(name: str | None) -> ZoneInfo:
    """The clinic's IANA zone; an unknown or empty name falls back to UTC."""
    try:
        return ZoneInfo(name or FALLBACK_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_ZONE)


def custom_due_at(tenant, start: datetime) -> datetime | None:
    """When the extra reminder of an appointment starting at `start` leaves (UTC), or None.

    None = the clinic has no extra reminder (days or time missing/unusable), or the
    computed moment is less than MIN_LEAD before the start. Whether it is already in
    the past is the CALLER's check (it knows `now`).
    """
    days = tenant.reminder_extra_days_before
    send_time = parse_send_time(tenant.reminder_extra_send_time)
    if days is None or days <= 0 or send_time is None:
        return None
    start_utc = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    zone = clinic_zone(tenant.timezone)
    local_day = start_utc.astimezone(zone).date() - timedelta(days=days)
    due = datetime.combine(local_day, send_time, tzinfo=zone).astimezone(UTC)
    if start_utc - due < MIN_LEAD:
        return None
    return due


def from_legacy_lead(
    lead: int | None, *, current_send_time: str | None
) -> tuple[int | None, str | None]:
    """R7's "minutes before" -> (days, HH:MM): whole days rounded up, within 2..14.

    Keeps the clinic's stored hour when it is a valid one, else 09:00. None/<=0 = off.
    The R9 migration applies the same rule in SQL to the stored values.
    """
    if lead is None or lead <= 0:
        return None, None
    days = min(EXTRA_DAYS_MAX, max(EXTRA_DAYS_MIN, -(-lead // _MINUTES_PER_DAY)))
    send_time = current_send_time if is_valid_send_time(current_send_time) else DEFAULT_SEND_TIME
    return days, send_time


def legacy_lead_minutes(days: int | None) -> int | None:
    """The legacy column's mirror (days x 1440) - kept until the column is dropped."""
    return days * _MINUTES_PER_DAY if days else None
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTEST tests/test_extra_reminder.py`
Expected: PASS (all). Then `uvx ruff check src/secretaria/core/extra_reminder.py tests/test_extra_reminder.py` and `uvx ruff format --check` on the same two files → clean.

- [ ] **Step 5: Commit**

```bash
git add src/secretaria/core/extra_reminder.py tests/test_extra_reminder.py
git commit -m "feat(reminders): extra reminder rule as days before at a local hour (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task A2: Columns, migration and fixtures

**Files:**
- Create: `migrations/versions/a4c7e2f9b1d3_extra_reminder_day_time.py`
- Create: `tests/test_migration_extra_reminder_day_time.py`
- Modify: `src/secretaria/models/tenant.py` (after `reminder_extra_lead_minutes`, ~line 248)
- Modify: `tests/_reminder_fixtures.py:48-71`
- Modify: `tests/test_migration_clinic_action_notices.py:29-34`

**Interfaces:**
- Consumes: `secretaria.core.extra_reminder.from_legacy_lead` (Task A1).
- Produces: `Tenant.reminder_extra_days_before: int | None`, `Tenant.reminder_extra_send_time: str | None`; Alembic head `a4c7e2f9b1d3`; fixture clinics `tenant`/`other_tenant` = 5 days at "09:00", mirror 7200, timezone default `America/Sao_Paulo`.

- [ ] **Step 1: Write the failing migration test**

```python
"""tenants.reminder_extra_days_before + reminder_extra_send_time (TASK-048 R9, spec §6.2)."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.core.extra_reminder import from_legacy_lead
from secretaria.models import Tenant
from tests._reminder_fixtures import db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a4c7e2f9b1d3"
DOWN_REVISION = "d8e3a5c1f7b2"
# (row id, legacy minutes) - the same table test_extra_reminder.py checks in Python.
LEGACY = [
    ("off", None), ("zero", 0), ("short", 720), ("min", 1500), ("two", 2880),
    ("three", 2881), ("five", 7200), ("max", 20160), ("long", 30000),
]


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_extra_reminder_day_time.py"
    spec = importlib.util.spec_from_file_location("extra_reminder_day_time_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_r7():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_converts_legacy_minutes_like_the_python_rule_and_downgrade_removes(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'r9.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text("CREATE TABLE tenants (id VARCHAR(16) PRIMARY KEY, reminder_extra_lead_minutes INTEGER)")
        )
        for row_id, lead in LEGACY:
            conn.execute(
                sa.text("INSERT INTO tenants (id, reminder_extra_lead_minutes) VALUES (:i, :l)"),
                {"i": row_id, "l": lead},
            )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns("tenants")}
            assert cols["reminder_extra_days_before"]["nullable"] is True
            assert cols["reminder_extra_send_time"]["nullable"] is True
            rows = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(
                    sa.text(
                        "SELECT id, reminder_extra_days_before, reminder_extra_send_time, "
                        "reminder_extra_lead_minutes FROM tenants"
                    )
                )
            }
            for row_id, lead in LEGACY:
                assert rows[row_id][:2] == from_legacy_lead(lead, current_send_time=None), row_id
                assert rows[row_id][2] == lead, "the legacy column is left as it was"
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("tenants")}
            assert "reminder_extra_days_before" not in names
            assert "reminder_extra_send_time" not in names
            assert "reminder_extra_lead_minutes" in names
    engine.dispose()


async def test_the_model_maps_both_columns_as_nullable(db):  # noqa: F811
    assert Tenant.__table__.c.reminder_extra_days_before.nullable is True
    assert Tenant.__table__.c.reminder_extra_send_time.nullable is True
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        assert (tenant.reminder_extra_days_before, tenant.reminder_extra_send_time) == (None, None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST tests/test_migration_extra_reminder_day_time.py`
Expected: FAIL — migration file not found / `AttributeError` on the model columns.

- [ ] **Step 3: Write the migration**

`migrations/versions/a4c7e2f9b1d3_extra_reminder_day_time.py`:

```python
"""R9: the clinic's extra reminder as "N days before, at HH:MM" - two additive columns.

TASK-048 R9 (docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §6.2):

    ADD tenants.reminder_extra_days_before  INTEGER NULL     (2..14, validated by the API)
    ADD tenants.reminder_extra_send_time    VARCHAR(5) NULL  ("HH:MM", clinic time zone)

Data: every clinic with a positive legacy `reminder_extra_lead_minutes` gets
days = ceil(lead / 1440) clamped to 2..14, at 09:00 - the same rule as
core/extra_reminder.py::from_legacy_lead. The legacy column is NOT dropped (widen
before, narrow after): R9 code stops reading it and mirrors days x 1440 into it.
Pending reminder rows are not recomputed here; they move at the clinic's next save of
the reminder setting.

Metadata-only adds on Postgres 11+ plus one small UPDATE on `tenants`. DEPLOY ORDER -
the database moves FIRST (the ORM names every mapped column on every read):

    1. `alembic upgrade head` from the NEW image (one-off), both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front.

Rollback: OLD code on both services first, then `alembic downgrade`. The legacy column
still holds days x 1440, so old code plans the extra reminder on the same day; the
chosen hour is lost.

Revision ID: a4c7e2f9b1d3
Revises: d8e3a5c1f7b2
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e2f9b1d3"
down_revision: str | None = "d8e3a5c1f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("reminder_extra_days_before", sa.Integer(), nullable=True))
    op.add_column(
        "tenants", sa.Column("reminder_extra_send_time", sa.String(length=5), nullable=True)
    )
    # Integer division on both Postgres and SQLite: (lead + 1439) / 1440 = ceil(lead / 1440).
    op.execute(
        "UPDATE tenants SET "
        "reminder_extra_days_before = CASE "
        "WHEN reminder_extra_lead_minutes <= 2880 THEN 2 "
        "WHEN reminder_extra_lead_minutes >= 20160 THEN 14 "
        "ELSE (reminder_extra_lead_minutes + 1439) / 1440 END, "
        "reminder_extra_send_time = '09:00' "
        "WHERE reminder_extra_lead_minutes > 0"
    )


def downgrade() -> None:
    op.drop_column("tenants", "reminder_extra_send_time")
    op.drop_column("tenants", "reminder_extra_days_before")
```

- [ ] **Step 4: Map the columns** — in `src/secretaria/models/tenant.py`, directly after the line `reminder_extra_lead_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)` insert:

```python
    # TASK-048 R9 (spec 2026-10-09 §6.2): the extra reminder as "N days before, at HH:MM"
    # in `timezone` (core/extra_reminder.py). Both NULL = no extra reminder. The column
    # above (`reminder_extra_lead_minutes`) is the LEGACY form: R9 no longer reads it and
    # mirrors days x 1440 into it on every save, so a code rollback plans the same day.
    # Drop it in a later migration once API and worker both run R9 ("narrow after").
    reminder_extra_days_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reminder_extra_send_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
```

- [ ] **Step 5: R7 is no longer the head** — in `tests/test_migration_clinic_action_notices.py` replace

```python
def test_the_migration_is_the_single_head_on_top_of_the_edit_outbox():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION
```

with

```python
def test_the_migration_sits_on_top_of_the_edit_outbox():
    # The head itself is pinned by the newest migration's test (R9: a4c7e2f9b1d3).
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION) is not None
    assert _migration().down_revision == DOWN_REVISION
```

- [ ] **Step 6: Fixtures use the pair** — in `tests/_reminder_fixtures.py` replace the `_make_tenant`, `tenant` and `other_tenant` definitions with:

```python
async def _make_tenant(db, name: str, *, enabled: bool, extra_days: int | None) -> Tenant:
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name=name,
            phone_number_id=None,
            reminders_v2_enabled=enabled,
            reminder_extra_days_before=extra_days,
            reminder_extra_send_time="09:00" if extra_days else None,
            reminder_extra_lead_minutes=extra_days * 1440 if extra_days else None,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    """Clinic with the feature ON and an extra reminder 5 days before at 09:00 (São Paulo).

    NOW is 12:00 UTC = 09:00 in São Paulo, so for an appointment at NOW + k days the extra
    reminder is exactly start - 5 days (the R1 assertions written in minutes still hold).
    """
    return await _make_tenant(db, "Clinic", enabled=True, extra_days=5)


@pytest_asyncio.fixture
async def other_tenant(db) -> Tenant:
    return await _make_tenant(db, "Other clinic", enabled=True, extra_days=5)
```

- [ ] **Step 7: Run to verify**

Run: `PYTEST tests/test_migration_extra_reminder_day_time.py tests/test_migration_clinic_action_notices.py tests/test_migration_professional_edit_notices.py tests/test_migration_reminders_foundation.py tests/test_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py`
Expected: PASS (the planner still reads the legacy column, which the fixtures still fill). If a `get_heads()` test elsewhere pins `d8e3a5c1f7b2`, change it the same way as Step 5.

- [ ] **Step 8: Lint and commit**

```bash
uvx ruff check migrations/versions/a4c7e2f9b1d3_extra_reminder_day_time.py tests/test_migration_extra_reminder_day_time.py src/secretaria/models/tenant.py tests/_reminder_fixtures.py tests/test_migration_clinic_action_notices.py
git add migrations/versions/a4c7e2f9b1d3_extra_reminder_day_time.py tests/test_migration_extra_reminder_day_time.py src/secretaria/models/tenant.py tests/_reminder_fixtures.py tests/test_migration_clinic_action_notices.py
git commit -m "feat(reminders): tenant columns for the day+time extra reminder, legacy minutes converted (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task A3: The planner and the replan use the local day + hour

**Files:**
- Modify: `src/secretaria/services/reminder_schedule.py` (`_planned_kinds` ~118-125, the loop in `schedule_reminders` ~187-190, `replan_custom_reminders` ~563-650)
- Create: `tests/test_extra_reminder_schedule.py`
- Modify (rewrite): `tests/test_reminder_lead_replan.py`
- Modify: `tests/test_reminder_schedule.py:75`

**Interfaces:**
- Consumes: `custom_due_at(tenant, start) -> datetime | None` (Task A1); the Task A2 columns and fixtures.
- Produces: `replan_custom_reminders(session, tenant, *, now) -> CustomReplan` — same signature as R7; it now honours days, time and `timezone`. Task A4 calls it whenever any of the three changed.

- [ ] **Step 1: Write the failing planner tests** — `tests/test_extra_reminder_schedule.py`:

```python
"""schedule_reminders plans the extra reminder on the clinic's local day and hour (TASK-048 R9)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

from secretaria.models import Appointment, Tenant
from secretaria.services.reminder_schedule import _as_utc, schedule_reminders
from tests._reminder_fixtures import NOW, db, make_appointment, tenant  # noqa: F401


async def _configure(db, tenant_id, **fields) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _schedule(db, tenant_id, appointment_id):
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        appointment = await session.get(Appointment, appointment_id)
        created = await schedule_reminders(session, appointment, clinic, now=NOW)
        await session.commit()
        return created


async def test_the_extra_reminder_leaves_n_days_before_at_the_chosen_local_time(db, tenant):
    await _configure(db, tenant.id, reminder_extra_send_time="07:15")
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    custom = created[0]
    assert _as_utc(custom.due_at) == datetime(2026, 10, 2, 10, 15, tzinfo=UTC)  # 07:15 São Paulo
    assert _as_utc(custom.warn_due_at) == datetime(2026, 10, 2, 12, 15, tzinfo=UTC)


async def test_a_late_night_appointment_counts_days_on_the_local_calendar(db, tenant):
    await _configure(db, tenant.id, reminder_extra_days_before=2)
    start = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)  # 2026-10-08 22:30 in São Paulo
    appointment = await make_appointment(db, tenant, start_at=start)

    created = await _schedule(db, tenant.id, appointment.id)

    custom = next(r for r in created if r.kind == "custom")
    assert _as_utc(custom.due_at) == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # 06/10 09:00 local


async def test_a_day_already_past_gets_no_extra_reminder(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["day", "hour"]


async def test_a_clinic_without_an_extra_reminder_plans_only_day_and_hour(db, tenant):
    await _configure(
        db, tenant.id, reminder_extra_days_before=None, reminder_extra_send_time=None
    )
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["day", "hour"]
```

- [ ] **Step 2: Rewrite the replan tests** — replace the whole content of `tests/test_reminder_lead_replan.py` with:

```python
"""A new extra-reminder setting replans the still-pending extra reminders.

TASK-044 R7 (spec §5.B) rules, with the R9 setting (spec §6.2): days before, local hour
and the clinic's time zone. NOW is 2026-10-01 12:00 UTC = 09:00 in São Paulo, so an
appointment at NOW + k days starts at 09:00 local and "N days before at 09:00" is exactly
start - N days.
"""

# ruff: noqa: F811

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.services import reminder_schedule
from secretaria.services.reminder_schedule import CustomReplan, _as_utc
from tests._reminder_fixtures import NOW, db, make_appointment, other_tenant, tenant  # noqa: F401

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
MANAUS = ZoneInfo("America/Manaus")


async def _set_tenant(db, tenant_id, **fields) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _plan(db, tenant_id, appointment_id) -> None:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        appointment = await session.get(Appointment, appointment_id)
        await reminder_schedule.schedule_reminders(session, appointment, clinic, now=NOW)
        await session.commit()


async def _rows(db, appointment_id, kind: str) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(
                    AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.kind == kind,
                )
                .order_by(AppointmentReminder.created_at)
            )
        )


async def _row(db, appointment_id, kind: str) -> AppointmentReminder:
    rows = await _rows(db, appointment_id, kind)
    assert len(rows) == 1, rows
    return rows[0]


async def _replan(
    db, tenant_id, days: int | None, send_time: str | None = "09:00", **fields
) -> CustomReplan:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        clinic.reminder_extra_days_before = days
        clinic.reminder_extra_send_time = send_time if days is not None else None
        for name, value in fields.items():
            setattr(clinic, name, value)
        result = await reminder_schedule.replan_custom_reminders(session, clinic, now=NOW)
        await session.commit()
        return result


async def test_new_days_move_the_pending_extra_reminder_and_nothing_else(db, tenant):
    start = NOW + timedelta(days=10)
    appointment = await make_appointment(db, tenant, start_at=start)
    await _plan(db, tenant.id, appointment.id)
    day_before = await _row(db, appointment.id, "day")
    custom_before = await _row(db, appointment.id, "custom")

    result = await _replan(db, tenant.id, 2)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(moved=1)
    assert custom.id == custom_before.id and custom.status == "pending"
    assert _as_utc(custom.due_at) == start - timedelta(days=2)
    assert _as_utc(custom.warn_due_at) == start - timedelta(days=2) + timedelta(hours=2)
    day = await _row(db, appointment.id, "day")
    assert (day.id, _as_utc(day.due_at)) == (day_before.id, _as_utc(day_before.due_at))


async def test_a_new_hour_moves_the_pending_extra_reminder(db, tenant):
    start = NOW + timedelta(days=10)
    appointment = await make_appointment(db, tenant, start_at=start)
    await _plan(db, tenant.id, appointment.id)

    result = await _replan(db, tenant.id, 5, "07:30")

    local = _as_utc((await _row(db, appointment.id, "custom")).due_at).astimezone(SAO_PAULO)
    assert result == CustomReplan(moved=1)
    assert local.date() == start.astimezone(SAO_PAULO).date() - timedelta(days=5)
    assert (local.hour, local.minute) == (7, 30)


async def test_a_new_time_zone_moves_the_pending_extra_reminder(db, tenant):
    start = NOW + timedelta(days=10)
    appointment = await make_appointment(db, tenant, start_at=start)
    await _plan(db, tenant.id, appointment.id)

    result = await _replan(db, tenant.id, 5, "09:00", timezone="America/Manaus")

    local = _as_utc((await _row(db, appointment.id, "custom")).due_at).astimezone(MANAUS)
    assert result == CustomReplan(moved=1)
    assert (local.hour, local.minute) == (9, 0)


async def test_switching_the_extra_reminder_off_cancels_it(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)

    result = await _replan(db, tenant.id, None)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(cancelled=1)
    assert custom.status == "cancelled"
    assert custom.invalidated_at is not None and custom.warn_due_at is None
    assert (await _row(db, appointment.id, "day")).status == "pending"


async def test_a_new_time_already_past_cancels_instead_of_sending_late(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _plan(db, tenant.id, appointment.id)  # 5 days before: due tomorrow 09:00

    result = await _replan(db, tenant.id, 6)  # 6 days before 09:00 local = NOW

    assert result == CustomReplan(cancelled=1)
    assert (await _row(db, appointment.id, "custom")).status == "cancelled"


async def test_an_extra_reminder_already_sent_stays_as_history(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)
    sent_row = await _row(db, appointment.id, "custom")
    async with db() as session:
        row = await session.get(AppointmentReminder, sent_row.id)
        row.status = "sent"
        await session.commit()

    result = await _replan(db, tenant.id, 2)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan()
    assert custom.status == "sent" and _as_utc(custom.due_at) == _as_utc(sent_row.due_at)


async def test_turning_it_on_adds_it_only_where_a_plan_already_exists(db, tenant):
    await _set_tenant(
        db,
        tenant.id,
        reminder_extra_days_before=None,
        reminder_extra_send_time=None,
        reminder_extra_lead_minutes=None,
    )
    planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, planned.id)  # day + hour only
    never_planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=12))

    result = await _replan(db, tenant.id, 2)

    assert result == CustomReplan(created=1)
    custom = await _row(db, planned.id, "custom")
    assert custom.status == "pending" and custom.with_prompt is True
    assert _as_utc(custom.due_at) == NOW + timedelta(days=10) - timedelta(days=2)
    assert await _rows(db, never_planned.id, "custom") == []  # left to the reconcile cron


async def test_another_clinic_and_a_switched_off_clinic_are_untouched(db, tenant, other_tenant):
    start = NOW + timedelta(days=10)
    mine = await make_appointment(db, tenant, start_at=start)
    theirs = await make_appointment(db, other_tenant, start_at=start)
    await _plan(db, tenant.id, mine.id)
    await _plan(db, other_tenant.id, theirs.id)

    await _replan(db, tenant.id, 2)

    assert _as_utc((await _row(db, theirs.id, "custom")).due_at) == start - timedelta(days=5)

    await _set_tenant(db, tenant.id, reminders_v2_enabled=False)
    result = await _replan(db, tenant.id, 3)

    assert result == CustomReplan()
    assert _as_utc((await _row(db, mine.id, "custom")).due_at) == start - timedelta(days=2)
```

- [ ] **Step 3: Turn the extra reminder off via the new column** — in `tests/test_reminder_schedule.py`, inside `test_no_custom_row_when_the_clinic_did_not_configure_one`, replace `t.reminder_extra_lead_minutes = None` with:

```python
        t.reminder_extra_days_before = None
        t.reminder_extra_send_time = None
```

- [ ] **Step 4: Run to verify they fail**

Run: `PYTEST tests/test_extra_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_reminder_schedule.py`
Expected: FAIL — the planner still uses the legacy minutes (e.g. the 07:15 test gets `start - 7200 min`; `test_no_custom_row…` still creates a custom row; replan by days does nothing).

- [ ] **Step 5: Implement** — in `src/secretaria/services/reminder_schedule.py`:

(a) add the import next to the other `secretaria.*` imports:

```python
from secretaria.core.extra_reminder import custom_due_at
```

(b) replace

```python
def _planned_kinds(tenant: Tenant) -> list[tuple[str, timedelta]]:
    plan: list[tuple[str, timedelta]] = []
    extra = tenant.reminder_extra_lead_minutes
    if extra is not None and extra > 0:
        plan.append((REMINDER_KIND_CUSTOM, timedelta(minutes=extra)))
    plan.append((REMINDER_KIND_DAY, _DAY_LEAD))
    plan.append((REMINDER_KIND_HOUR, _HOUR_LEAD))
    return plan
```

with

```python
def _planned_dues(tenant: Tenant, start: datetime) -> list[tuple[str, datetime]]:
    """(kind, due) of the reminders an appointment starting at `start` gets, in send order.

    The extra ("custom") one is the clinic's "N dias antes, às HH:MM" on its local calendar
    (core/extra_reminder.py, spec 2026-10-09 §6.2); day/hour are fixed leads.
    """
    plan: list[tuple[str, datetime]] = []
    custom = custom_due_at(tenant, start)
    if custom is not None:
        plan.append((REMINDER_KIND_CUSTOM, custom))
    plan.append((REMINDER_KIND_DAY, start - _DAY_LEAD))
    plan.append((REMINDER_KIND_HOUR, start - _HOUR_LEAD))
    return plan
```

(c) in `schedule_reminders` replace

```python
    for kind, lead in _planned_kinds(tenant):
        due = start - lead
        if due <= now_utc:
```

with

```python
    for kind, due in _planned_dues(tenant, start):
        if due <= now_utc:
```

(d) in `replan_custom_reminders` replace the docstring's first two paragraphs

```python
    """Put the clinic's NEW `reminder_extra_lead_minutes` on the reminders already planned.

    Call AFTER `tenant.reminder_extra_lead_minutes` holds the new value, inside the
    configuration save's transaction (flushes, never commits). For every live future
    appointment of the clinic that has a patient:
```

with

```python
    """Put the clinic's CURRENT extra-reminder setting on the reminders already planned.

    Call AFTER the tenant holds the new `reminder_extra_days_before` /
    `reminder_extra_send_time` / `timezone` (TASK-048 R9, spec §6.2), inside the
    configuration save's transaction (flushes, never commits). The due time is
    core/extra_reminder.py::custom_due_at of each appointment's CURRENT start. For every
    live future appointment of the clinic that has a patient:
```

and in the same docstring replace `with the new lead (creating only `custom` there would make the cron skip it and lose` by `with the new setting (creating only `custom` there would make the cron skip it and lose` and `* with a lead and no `custom` row, one is created` by `* with the setting on and no `custom` row, one is created`; and `due time is not in the future` stays. Then replace

```python
    now_utc = _as_utc(now)
    lead = tenant.reminder_extra_lead_minutes
    lead_delta = timedelta(minutes=lead) if lead is not None and lead > 0 else None
```

with

```python
    now_utc = _as_utc(now)
```

and replace

```python
        due = start - lead_delta if lead_delta is not None else None
```

with

```python
        due = custom_due_at(tenant, start)
```

Also update the section comment `# --- TASK-044 R7 (spec 2026-10-09 §5.B): the clinic changed its extra-reminder lead ----` to `# --- TASK-044 R7 (§5.B) / TASK-048 R9 (§6.2): the clinic changed its extra reminder ----`.

- [ ] **Step 6: Run to verify they pass**

Run: `PYTEST tests/test_extra_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_reminder_schedule.py tests/test_reminder_confirmation.py tests/test_confirmation_warnings_cron.py tests/test_hub_calendar_confirmation.py tests/test_reminder_engine.py`
Expected: PASS. (If `tests/test_reminder_engine.py` does not exist, run `PYTEST tests -q -k "reminder or confirmation"` instead.) A failure in a test that asserts a custom due as `start - timedelta(minutes=7200)` for a start that is NOT at 12:00 UTC is a legitimate rule change: rewrite its expectation as "5 days before at 09:00 São Paulo" and say so in the commit body.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff check src/secretaria/services/reminder_schedule.py tests/test_extra_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_reminder_schedule.py
uvx ruff format --check src/secretaria/services/reminder_schedule.py tests/test_extra_reminder_schedule.py tests/test_reminder_lead_replan.py
git add src/secretaria/services/reminder_schedule.py tests/test_extra_reminder_schedule.py tests/test_reminder_lead_replan.py tests/test_reminder_schedule.py
git commit -m "feat(reminders): plan and replan the extra reminder on the clinic's local day and hour (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task A4: Hub configuration wire (pair + legacy tolerance + replan trigger)

**Files:**
- Modify: `src/secretaria/schemas/config.py` (`TenantConfigUpdate` ~213-220 and its validators; `TenantConfigRead` ~360-363)
- Modify: `src/secretaria/services/hub_configuration.py` (`TENANT_SCALAR_FIELDS` ~142, `apply_tenant_config` ~269-302, `tenant_read_model` ~396-399)
- Rename + rewrite: `tests/test_hub_config_reminder_lead.py` → `tests/test_hub_config_extra_reminder.py`

**Interfaces:**
- Consumes: `EXTRA_DAYS_MIN`, `EXTRA_DAYS_MAX`, `is_valid_send_time`, `from_legacy_lead`, `legacy_lead_minutes` (Task A1); `reminder_schedule.replan_custom_reminders` (Task A3).
- Produces: the wire in "Produces for Part B".

- [ ] **Step 1: Move the test file** — `git mv tests/test_hub_config_reminder_lead.py tests/test_hub_config_extra_reminder.py`, then replace its whole content with:

```python
"""The extra reminder on the hub configuration (TASK-048 R9, spec §6.2; R7 §5.B replan)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, time, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Appointment, AppointmentReminder, Tenant  # noqa: E402
from secretaria.services import reminder_schedule  # noqa: E402
from tests._reminder_fixtures import db, make_appointment, tenant  # noqa: E402, F401

CONFIG = "/tenants/me/config"
CONFIGURATION = "/tenants/me/configuration"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
MANAUS = ZoneInfo("America/Manaus")


@pytest.fixture(autouse=True)
def _override(db, tenant):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _stored(db, tenant_id):  # noqa: F811
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        return (
            row.reminders_v2_enabled,
            row.reminder_extra_days_before,
            row.reminder_extra_send_time,
            row.reminder_extra_lead_minutes,
        )


async def _set(db, tenant_id, **fields) -> None:  # noqa: F811
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _planned_custom_due(db, tenant, start: datetime) -> tuple[Appointment, datetime]:  # noqa: F811
    now = datetime.now(UTC)
    appointment = await make_appointment(db, tenant, start_at=start)
    async with db() as session:
        clinic = await session.get(Tenant, tenant.id)
        row = await session.get(Appointment, appointment.id)
        await reminder_schedule.schedule_reminders(session, row, clinic, now=now)
        await session.commit()
    return appointment, now


async def _custom_due(db, appointment_id) -> datetime:  # noqa: F811
    async with db() as session:
        custom = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appointment_id,
                AppointmentReminder.kind == "custom",
            )
        )
    return reminder_schedule._as_utc(custom.due_at)


async def test_get_config_exposes_the_switch_the_pair_and_the_legacy_mirror(client: AsyncClient):
    body = (await client.get(CONFIG)).json()

    assert body["reminders_v2_enabled"] is True
    assert body["reminder_extra_days_before"] == 5
    assert body["reminder_extra_send_time"] == "09:00"
    assert body["reminder_extra_lead_minutes"] == 7200


async def test_the_configuration_save_stores_the_pair_and_echoes_it(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminder_extra_days_before": 3, "reminder_extra_send_time": "08:15"}},
    )

    assert response.status_code == 200, response.text
    echoed = response.json()["tenant"]
    assert (echoed["reminder_extra_days_before"], echoed["reminder_extra_send_time"]) == (3, "08:15")
    assert echoed["reminder_extra_lead_minutes"] == 4320
    assert await _stored(db, tenant.id) == (True, 3, "08:15", 4320)


async def test_both_null_switch_it_off_on_the_legacy_save_too(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(
        CONFIG, json={"reminder_extra_days_before": None, "reminder_extra_send_time": None}
    )

    assert response.status_code == 200
    assert response.json()["reminder_extra_days_before"] is None
    assert await _stored(db, tenant.id) == (True, None, None, None)


@pytest.mark.parametrize(
    "patch",
    [
        {"reminder_extra_days_before": 1, "reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": 15, "reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": 0, "reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": "dois", "reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": "9:00"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": "05:45"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": "22:15"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": "10:10"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": "24:00"},
        {"reminder_extra_days_before": 3},
        {"reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": None, "reminder_extra_send_time": "09:00"},
        {"reminder_extra_days_before": 3, "reminder_extra_send_time": None},
        {"reminder_extra_lead_minutes": 1440},
    ],
)
async def test_an_invalid_or_half_pair_is_422_and_nothing_changes(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
    patch,
):
    response = await client.put(CONFIGURATION, json={"tenant": patch})

    assert response.status_code == 422
    assert await _stored(db, tenant.id) == (True, 5, "09:00", 7200)


@pytest.mark.parametrize(("days", "send_time"), [(2, "06:00"), (14, "22:00")])
async def test_the_bounds_themselves_are_accepted(client: AsyncClient, db, tenant, days, send_time):  # noqa: F811
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminder_extra_days_before": days, "reminder_extra_send_time": send_time}},
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, days, send_time, days * 1440)


async def test_a_legacy_lead_from_an_older_screen_is_translated_keeping_the_hour(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    await _set(db, tenant.id, reminder_extra_send_time="07:30")

    response = await client.put(CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2881}})

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, 3, "07:30", 4320)

    response = await client.put(CONFIG, json={"reminder_extra_lead_minutes": None})

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, None, None, None)


async def test_the_pair_wins_over_a_legacy_lead_in_the_same_body(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {
                "reminder_extra_days_before": 4,
                "reminder_extra_send_time": "10:00",
                "reminder_extra_lead_minutes": 20160,
            }
        },
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, 4, "10:00", 5760)


async def test_the_switch_is_never_written_by_the_hub(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminders_v2_enabled": False, "persona_notes": "Gentil"}},
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, 5, "09:00", 7200)


async def test_a_saved_pair_moves_the_planned_extra_reminder_in_the_same_save(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(hour=15, minute=0, second=0, microsecond=0)
    appointment, _ = await _planned_custom_due(db, tenant, start)

    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminder_extra_days_before": 3, "reminder_extra_send_time": "08:00"}},
    )

    assert response.status_code == 200
    local = (await _custom_due(db, appointment.id)).astimezone(SAO_PAULO)
    assert local.date() == start.astimezone(SAO_PAULO).date() - timedelta(days=3)
    assert local.time() == time(8, 0)


async def test_changing_the_time_zone_replans_in_the_same_save(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(hour=15, minute=0, second=0, microsecond=0)
    appointment, _ = await _planned_custom_due(db, tenant, start)

    response = await client.put(CONFIGURATION, json={"tenant": {"timezone": "America/Manaus"}})

    assert response.status_code == 200, response.text
    local = (await _custom_due(db, appointment.id)).astimezone(MANAUS)
    assert local.time() == time(9, 0)


async def test_saves_that_change_nothing_do_not_replan(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []

    async def _spy(*args, **kwargs):
        calls.append(1)
        return reminder_schedule.CustomReplan()

    monkeypatch.setattr(reminder_schedule, "replan_custom_reminders", _spy)

    same_pair = {"reminder_extra_days_before": 5, "reminder_extra_send_time": "09:00"}
    await client.put(CONFIGURATION, json={"tenant": same_pair})
    await client.put(CONFIGURATION, json={"tenant": {"persona_notes": "Gentil"}})
    await client.put(CONFIGURATION, json={"tenant": {"timezone": "America/Sao_Paulo"}})
    # The R7 screen echoes the mirror on every save: it must be a no-op.
    await client.put(CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 7200}})

    assert calls == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST tests/test_hub_config_extra_reminder.py`
Expected: FAIL — `KeyError: 'reminder_extra_days_before'` on GET; PUTs with the pair return 200 without storing (unknown keys ignored); legacy PUT stores minutes only.

- [ ] **Step 3: Schemas** — in `src/secretaria/schemas/config.py`:

(a) import, after the `secretaria.core.whatsapp_limits` import block:

```python
from secretaria.core.extra_reminder import EXTRA_DAYS_MAX, EXTRA_DAYS_MIN, is_valid_send_time
```

(b) in `TenantConfigUpdate` replace the R7 lead comment + field

```python
    # TASK-044 R7 (spec 2026-10-09 §5.B): the clinic's extra ("custom") reminder, in
    # minutes before the appointment. More than the 1-day reminder (> 1440; 1500 keeps
    # them at least an hour apart) and at most 14 days. Absent = untouched; explicit
    # null = no extra reminder. A change replans the still-pending extra reminders
    # (services/reminder_schedule.py::replan_custom_reminders). `reminders_v2_enabled`
    # is deliberately NOT accepted here: the owner turns reminders on per clinic
    # (spec 4.5) - a PUT that sends it is ignored like any unknown key.
    reminder_extra_lead_minutes: int | None = Field(default=None, ge=1500, le=20160)
```

with

```python
    # TASK-048 R9 (spec 2026-10-09 §6.2): the clinic's extra ("custom") reminder as
    # "N dias antes, às HH:MM" in its time zone. The two keys travel TOGETHER: both null =
    # no extra reminder, both set = on (validated below). Absent = untouched. A change
    # (or a `timezone` change) replans the still-pending extra reminders
    # (services/reminder_schedule.py::replan_custom_reminders). `reminders_v2_enabled`
    # is deliberately NOT accepted here: the owner turns reminders on per clinic
    # (spec 4.5) - a PUT that sends it is ignored like any unknown key.
    reminder_extra_days_before: int | None = Field(
        default=None, ge=EXTRA_DAYS_MIN, le=EXTRA_DAYS_MAX
    )
    reminder_extra_send_time: str | None = None
    # LEGACY (TASK-044 R7): minutes before, still accepted from a screen older than R9
    # and translated by services/hub_configuration.py (core/extra_reminder.py::
    # from_legacy_lead). Ignored when the pair above is in the same body.
    reminder_extra_lead_minutes: int | None = Field(default=None, ge=1500, le=20160)
```

(c) next to `_paid_flag_is_true_or_false`, add:

```python
    @field_validator("reminder_extra_send_time")
    @classmethod
    def _send_time_on_the_grid(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_send_time(value):
            raise ValueError(
                "reminder_extra_send_time must be HH:MM between 06:00 and 22:00, "
                "in 15-minute steps"
            )
        return value

    @model_validator(mode="after")
    def _extra_reminder_pair_travels_together(self) -> "TenantConfigUpdate":
        sent = {"reminder_extra_days_before", "reminder_extra_send_time"} & self.model_fields_set
        if not sent:
            return self
        if len(sent) == 1:
            raise ValueError(
                "reminder_extra_days_before and reminder_extra_send_time travel together"
            )
        if (self.reminder_extra_days_before is None) != (self.reminder_extra_send_time is None):
            raise ValueError(
                "reminder_extra_days_before and reminder_extra_send_time are both null (off) "
                "or both set"
            )
        return self
```

(d) in `TenantConfigRead`, after `reminder_extra_lead_minutes: int | None = None`, add:

```python
    # TASK-048 R9: the extra reminder as "N days before, at HH:MM" (both None = off).
    # `reminder_extra_lead_minutes` above is now the LEGACY mirror (days x 1440) kept for
    # a screen older than R9; remove it together with the legacy column.
    reminder_extra_days_before: int | None = None
    reminder_extra_send_time: str | None = None
```

- [ ] **Step 4: Service** — in `src/secretaria/services/hub_configuration.py`:

(a) add the import next to `from secretaria.models import Tenant`:

```python
from secretaria.core import extra_reminder
```

(b) in `TENANT_SCALAR_FIELDS` delete the line `"reminder_extra_lead_minutes",` (the pair is applied by the helper below, never as a plain scalar).

(c) directly above `async def apply_tenant_config`, add:

```python
def _extra_reminder_state(tenant: Tenant) -> tuple[int | None, str | None, str | None]:
    """What the extra reminders' due times depend on (a change replans them)."""
    return (
        tenant.reminder_extra_days_before,
        tenant.reminder_extra_send_time,
        tenant.timezone,
    )


def _apply_extra_reminder(tenant: Tenant, data: dict) -> None:
    """The clinic's extra reminder (TASK-048 R9, spec §6.2) from a validated patch.

    The pair wins; without it, a legacy `reminder_extra_lead_minutes` from a screen older
    than R9 is translated (whole days rounded up, the stored hour kept). The legacy
    column is mirrored as days x 1440 so a code rollback plans the same day.
    """
    if "reminder_extra_days_before" in data:
        days = data["reminder_extra_days_before"]
        send_time = data.get("reminder_extra_send_time") if days is not None else None
    elif "reminder_extra_lead_minutes" in data:
        days, send_time = extra_reminder.from_legacy_lead(
            data["reminder_extra_lead_minutes"],
            current_send_time=tenant.reminder_extra_send_time,
        )
    else:
        return
    tenant.reminder_extra_days_before = days
    tenant.reminder_extra_send_time = send_time
    tenant.reminder_extra_lead_minutes = extra_reminder.legacy_lead_minutes(days)
```

(d) in `apply_tenant_config` replace

```python
    previous_lead = tenant.reminder_extra_lead_minutes
    for field_name in TENANT_SCALAR_FIELDS:
        if field_name in data:
            setattr(tenant, field_name, data[field_name])
```

with

```python
    previous_extra = _extra_reminder_state(tenant)
    for field_name in TENANT_SCALAR_FIELDS:
        if field_name in data:
            setattr(tenant, field_name, data[field_name])
    _apply_extra_reminder(tenant, data)
```

and replace

```python
    # TASK-044 R7 (spec §5.B): a NEW extra-reminder lead moves the reminders already
    # planned, in this same transaction - a rolled-back save rolls the replan back too.
    if (
        "reminder_extra_lead_minutes" in data
        and tenant.reminder_extra_lead_minutes != previous_lead
    ):
        await reminder_schedule.replan_custom_reminders(session, tenant, now=datetime.now(UTC))
```

with

```python
    # TASK-044 R7 (§5.B) / TASK-048 R9 (§6.2): a NEW day, hour or time zone moves the extra
    # reminders already planned, in this same transaction - a rolled-back save rolls the
    # replan back too.
    if _extra_reminder_state(tenant) != previous_extra:
        await reminder_schedule.replan_custom_reminders(session, tenant, now=datetime.now(UTC))
```

(e) in `tenant_read_model`, after `reminder_extra_lead_minutes=tenant.reminder_extra_lead_minutes,` add:

```python
        reminder_extra_days_before=tenant.reminder_extra_days_before,
        reminder_extra_send_time=tenant.reminder_extra_send_time,
```

- [ ] **Step 5: Run to verify it passes**

Run: `PYTEST tests/test_hub_config_extra_reminder.py tests/test_reminder_lead_replan.py`
Expected: PASS.

- [ ] **Step 6: Run every config/hub test** (other config tests compare whole read models or PUT bodies)

Run: `PYTEST tests -q -k "config or configuration or hub"`
Expected: PASS. A test asserting the exact key set of `TenantConfigRead` gets the two new keys added to its expectation (additive contract change).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff check src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py tests/test_hub_config_extra_reminder.py
uvx ruff format --check src/secretaria/services/hub_configuration.py tests/test_hub_config_extra_reminder.py
git add src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py tests/test_hub_config_extra_reminder.py tests/test_hub_config_reminder_lead.py
git commit -m "feat(hub): extra reminder as days + local hour on the configuration, legacy minutes still accepted (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task A5: secretarIA checkpoint and full validation

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r9.md`
- Modify: `docs/CHECKPOINT_lembretes_r7.md` (one pointer line under the "Lembrete extra" bullet)

**Interfaces:**
- Consumes: Tasks A1–A4 (commit SHAs).
- Produces: the documentation the next session and the deploy operator read.

- [ ] **Step 1: Full suite and lint**

Run: `PYTEST tests -q` (expect the TASK-047 baseline of ~4927 passed + the new tests, 0 failed; separate any pre-existing failure from a regression by running the failing test at the base SHA).
Run: `uvx ruff check src tests` (expect clean) and `alembic heads` via `PYTHONPATH=... python.exe -m alembic heads` → `a4c7e2f9b1d3 (head)`.

- [ ] **Step 2: Write `docs/CHECKPOINT_lembretes_r9.md`**

```markdown
# CHECKPOINT — Lembretes R9: lembrete extra "N dias antes, às HH:MM" (TASK-048)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §6.2.
Plano: `docs/superpowers/plans/2026-10-10-lembretes-r9-sem-precheck-e-lembrete-dia-hora.md` (Parte A).

## Estado

- Branch `task/TASK-048-lembrete-dia-hora-api` (base: <SHA base>). Commits: <SHAs A1..A5>.
- **Local e commitado; não mesclado, não pushado, não deployado. Migração não aplicada em banco real.**
- Suíte: <N> passed / <M> skipped / 0 failed; ruff limpo; head Alembic `a4c7e2f9b1d3`.

## O que entrou onde

- Regra única: `core/extra_reminder.py` (`custom_due_at`, `from_legacy_lead`, `is_valid_send_time`).
- Colunas: `tenants.reminder_extra_days_before`, `tenants.reminder_extra_send_time` (migração
  `a4c7e2f9b1d3`, converte os minutos antigos: dias arredondados para cima, 2..14, às 09:00).
- Planejamento: `services/reminder_schedule.py::schedule_reminders` (`_planned_dues`) e
  `replan_custom_reminders` usam `custom_due_at`.
- Configuração: `schemas/config.py` (par dias+hora, juntos; minutos antigos ainda aceitos) e
  `services/hub_configuration.py::apply_tenant_config` (`_apply_extra_reminder`; reprograma quando muda
  dia, hora ou fuso).

## Decisões

D1–D11 do plano ("Decisions taken where the spec is silent"): 2..14 dias; 06:00..22:00 de 15 em 15;
piso de 25 h; fuso inválido → UTC; coluna antiga mantida e espelhada (dias × 1440); minutos antigos
traduzidos mantendo a hora gravada; par vence o legado; mudança de fuso reprograma.

## Deploy (quando o dono autorizar)

1. `alembic upgrade head` a partir da imagem nova (serviços ainda antigos).
2. `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` → paridade).
3. Front Brain-Message.

Rollback: código antigo nos dois serviços, depois `alembic downgrade` (o dia continua certo pela coluna
espelhada; a hora escolhida se perde).

## Pendências

- Remover a coluna `reminder_extra_lead_minutes` e o campo legado do contrato numa migração futura, depois
  que API, worker e front estiverem no R9 (code-first, depois o DROP).
- Lembretes extras já planejados antes do deploy mantêm o horário antigo até a clínica salvar a
  configuração de lembretes de novo.
- Prova real em clínica de teste após deploy autorizado.
```

Fill the `<…>` values from Step 1 and `git log`.

- [ ] **Step 3: Pointer in `docs/CHECKPOINT_lembretes_r7.md`** — directly after the bullet that starts with `- Lembrete extra (spec §5.B):` add:

```markdown
  - **Substituído no R9 (2026-10-10):** o lembrete extra passou a ser "N dias antes, às HH:MM" — ver `docs/CHECKPOINT_lembretes_r9.md`.
```

- [ ] **Step 4: Commit**

```bash
git add docs/CHECKPOINT_lembretes_r9.md docs/CHECKPOINT_lembretes_r7.md
git commit -m "docs: checkpoint for the day+time extra reminder (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

# Part B — Brain-Message-Frontend

### Task B1: The console's data layer never reaches PreCheck

**Files:**
- Modify: `lib/real/console-api.real.ts`
- Delete: `lib/real/precheck-hub.ts`
- Modify: `lib/real/brain-session.ts:295-308` (delete `mintPrecheckToken`)
- Modify: `lib/real/mappers.ts:367-530` (PreCheck section)
- Modify: `lib/__tests__/real-mappers.test.ts`
- Modify: `lib/conversations.ts:24-26`, `lib/__tests__/conversations.test.ts`
- Create: `lib/real/__tests__/console-no-precheck.test.ts`

**Interfaces:**
- Consumes: nothing from Part A (independent).
- Produces: every `ConsoleApi` call with a `precheck:` id rejects `ConsoleApiError(404, "conversation_not_found")` before any request; `listConversations` returns secretarIA rows only. `mappers.ts` keeps `PRECHECK_ID_PREFIX`, `isPrecheckConversationId`, `toPrecheckMessages` and the shared card helpers; it no longer exports `precheckConversationId`, `precheckSessionRef`, `PreCheckSessionWire`, `PRECHECK_THREAD_LABEL`, `precheckThreadName`, `toPrecheckConversationSummary`.

- [ ] **Step 1: Write the failing test** — `lib/real/__tests__/console-no-precheck.test.ts`:

```ts
// PreCheck conversations never appear in the staff console (secretarIA spec 2026-10-09 §6.1):
// the real client lists secretarIA only, never calls PreCheck, and answers a `precheck:` id
// (an old link, a pasted URL) exactly like an id that does not exist — before any request.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRealConsoleApi } from "@/lib/real/console-api.real";
import { fetchEntitlements, fetchMe, getSession, type Session } from "@/lib/real/brain-session";
import { hubFetch } from "@/lib/real/secretaria-hub";
import { fetchOwnProfessionalId } from "@/lib/real/console-my-patients";

vi.mock("@/lib/real/brain-session", async (original) => ({
  ...await original<typeof import("@/lib/real/brain-session")>(),
  getSession: vi.fn(),
  ensureSession: vi.fn(),
  fetchMe: vi.fn(),
  fetchEntitlements: vi.fn(),
}));
vi.mock("@/lib/real/secretaria-hub", async (original) => ({
  ...await original<typeof import("@/lib/real/secretaria-hub")>(),
  hubFetch: vi.fn(),
}));
vi.mock("@/lib/real/console-my-patients", () => ({
  fetchOwnProfessionalId: vi.fn(),
  fetchMyPatientConversationIds: vi.fn(),
}));

const session: Session = {
  token: "synthetic-test-token", tenantId: "test-clinic", email: "staff@example.test",
  role: "doctor", userId: "test-staff",
};
const HUB_ROW = {
  id: "a939ed79-f092-432e-b847-6094c2abc274",
  patient_wa_id: null,
  patient_name: "Paciente Teste",
  handover_state: "BOT_ACTIVE",
  last_message_at: "2026-10-10T12:00:00+00:00",
};
const fetchSpy = vi.fn();

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("fetch", fetchSpy);
  vi.mocked(getSession).mockReturnValue(session);
  vi.mocked(fetchOwnProfessionalId).mockResolvedValue(null);
  vi.mocked(fetchMe).mockResolvedValue({
    user: { id: "test-staff", name: "Dra. Teste", email: "staff@example.test", role: "doctor" },
    tenant: { id: "test-clinic", clinic_name: "Clínica Teste" },
  } as never);
  // A clinic that OWNS PreCheck: before R9 this is exactly when the console listed PreCheck threads.
  vi.mocked(fetchEntitlements).mockResolvedValue({
    tenant_id: "test-clinic",
    clinic_name: "Clínica Teste",
    products: { secretaria: true, precheck: true },
    channels: { brain_message: true },
  } as never);
  vi.mocked(hubFetch).mockImplementation((async (_s: unknown, path: string) => {
    if (path === "/tenants/me/config") return { timezone: "America/Sao_Paulo", is_active: true };
    if (path === "/tenants/me/conversations") return [HUB_ROW];
    throw new Error(`unexpected hub path ${path}`);
  }) as never);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("console without PreCheck conversations (spec §6.1)", () => {
  it("lists only secretarIA conversations and never calls PreCheck, even for a clinic that owns it", async () => {
    const api = createRealConsoleApi();
    const list = await api.listConversations({ scope: "todas", query: "" });
    expect(list.map((c) => c.id)).toEqual([HUB_ROW.id]);
    expect(list.every((c) => c.product === "secretaria")).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each(["precheck:ref-abc123", "precheck:"])("a pasted PreCheck id is 'not found' everywhere, before any request: %s", async (id) => {
    const api = createRealConsoleApi();
    await expect(api.getConversation(id)).rejects.toMatchObject({ status: 404, code: "conversation_not_found" });
    await expect(api.listMessages(id)).rejects.toMatchObject({ status: 404 });
    await expect(api.getPatientContext(id)).rejects.toMatchObject({ status: 404 });
    await expect(api.sendMessage(id, { text: "oi" } as never)).rejects.toMatchObject({ status: 404 });
    await expect(api.setConversationMode(id, "humano")).rejects.toMatchObject({ status: 404 });
    const hubPaths = vi.mocked(hubFetch).mock.calls.map((call) => String(call[1]));
    expect(hubPaths.some((p) => p.includes("precheck"))).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/real/__tests__/console-no-precheck.test.ts`
Expected: FAIL — the first test sees `fetchSpy` called (the PreCheck token mint / `/api/precheck/precheck-sessions`); the second sees `getConversation` 404 but `listMessages` attempting PreCheck (`fetchSpy` called) and `sendMessage` rejecting 501 `precheck_read_only`.

- [ ] **Step 3: Implement in `lib/real/console-api.real.ts`**

(a) Header comment: replace the lines

```ts
//             markRead (Brain-Message threads only; a no-op on WhatsApp and
//             PreCheck — docs/CHECKPOINT_brain_message_status_entrega_frontend.md),
```

with

```ts
//             markRead (Brain-Message threads only; a no-op on WhatsApp —
//             docs/CHECKPOINT_brain_message_status_entrega_frontend.md),
```

and replace

```ts
// PreCheck threads have no staff view yet (PROMPT_BRAIN_MESSAGE_PRECHECK_
// STAFF_VIEW.md): every conversation here is a secretarIA conversation.
```

with

```ts
// PreCheck conversations NEVER appear here, for any role (owner decision 2026-10-10,
// secretarIA spec 2026-10-09 §6.1): the list is secretarIA's alone, and a
// `precheck:<ref>` id (an old link) is answered 404 before any request
// (refusePrecheckId). docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md.
```

(b) Imports: replace the mappers import block and delete the precheck-hub import:

```ts
import {
  isPrecheckConversationId,
  knowledgeFromThread,
  readMarkApplies,
  toConversationSummary,
  toMessage,
  toThread,
  type ConversationWire,
  type HumanAuthor,
  type MessageWire,
  type MediaUrl,
  type ThreadKnowledge,
} from "./mappers";
import { fetchMyPatientConversationIds, fetchOwnProfessionalId } from "./console-my-patients";
import { HubApiError, hubFetch, hubFetchBlob, hubUpload, SECRETARIA_HUB_BASE } from "./secretaria-hub";
```

(c) Replace the whole `readOnlyPrecheck` function and its comment block with:

```ts
// A `precheck:` id is answered exactly like an id that does not exist, before any
// request (spec §6.1): the console never opens a PreCheck conversation.
function refusePrecheckId(conversationId: string): void {
  if (isPrecheckConversationId(conversationId)) {
    throw new ConsoleApiError(404, "conversation_not_found", "Conversa não encontrada.");
  }
}
```

(d) In `translate`, replace `if (error instanceof BrainApiError || error instanceof HubApiError || error instanceof PrecheckApiError) {` with `if (error instanceof BrainApiError || error instanceof HubApiError) {`.

(e) Delete the whole `precheckList` function (with its comment) and replace `fetchAll` with:

```ts
  async function fetchAll(): Promise<ConversationSummary[]> {
    const s = await session();
    const current = await me();
    if (!current.capabilities.chat) return [];
    // Freshest first, the ordering the screen has always shown.
    return (await secretariaList(s)).sort((a, b) => (a.lastMessageAt < b.lastMessageAt ? 1 : -1));
  }
```

(f) Delete the whole `precheckThread` function (with its comment block "Which backend holds a thread…") and replace `thread` with:

```ts
  async function thread(conversationId: string): Promise<Message[]> {
    refusePrecheckId(conversationId);
    const s = await session();
    const rows = await hubFetch<MessageWire[]>(s, `/tenants/me/conversations/${encodeURIComponent(conversationId)}/messages`);
    const messages = toThread(rows, conversationId, hubMedia(conversationId));
    const learned = knowledgeFromThread(messages);
    if (learned) knowledge.set(conversationId, learned);
    return messages;
  }
```

(g) `getConversation`: insert `refusePrecheckId(conversationId);` as the first line inside its `guarded(async () => {`.
(h) `sendMessage`: replace `if (isPrecheckConversationId(conversationId)) throw readOnlyPrecheck("Responder");` with `refusePrecheckId(conversationId);`.
(i) `setConversationMode`: replace `if (isPrecheckConversationId(conversationId)) throw readOnlyPrecheck("Assumir a conversa");` with `refusePrecheckId(conversationId);`.
(j) `getPatientContext`: insert `refusePrecheckId(conversationId);` as the first line inside its `guarded(async (): Promise<PatientContext> => {`.
(k) `signOut`: delete the line `clearPrecheckTokens();`.
(l) `getTyping` / `sendTyping`: unchanged (silent no-op for a `precheck:` id, covered by `console-typing.test.ts`).

- [ ] **Step 4: Delete the dead PreCheck client**

```bash
git rm lib/real/precheck-hub.ts
```

In `lib/real/brain-session.ts` delete the block from the comment line `// POST /sso/precheck/token — brain-api mints a PreCheck-COMPATIBLE JWT for the` through the closing `}` of `export async function mintPrecheckToken(...)`.

- [ ] **Step 5: Mappers** — in `lib/real/mappers.ts`:

Replace the section header comment (from `// PreCheck — the SECOND product in the same list (read-only)` through the line `// clinic refers to a questionnaire before opening it.`) with:

```ts
// PreCheck transcript → bubbles
// ---------------------------------------------------------------------------
//
// Since 2026-10-10 (secretarIA spec 2026-10-09 §6.1) the console never lists or
// opens a PreCheck conversation. What stays here: the `precheck:` id recogniser
// (an old link is refused as "not found" — console-api.real.ts::refusePrecheckId)
// and the transcript → bubble mapping with its card helpers, which the patient
// portal shares (lib/patient-portal.ts imports precheckInteractive/
// precheckReplyTo/precheckAskedBy/precheckImage). `toPrecheckMessages` itself has
// no production caller any more; it stays because its tests are the coverage of
// those shared helpers (moving them onto the portal path is a follow-up).
//
// Wire shapes mirror PreCheck app/schemas/brain_message.py
// (`BrainMessageTranscriptResponse`).
```

Replace the comment above `PRECHECK_ID_PREFIX` usage and the three functions:

```ts
// A PreCheck thread's conversation id is `precheck:<session_ref>`. The prefix
// is what makes listMessages routable from the id ALONE — no map to consult,
// nothing to lose on a reload, and a pasted /chat?conversa=precheck:… URL still
// reaches the right backend. secretarIA ids are hub UUIDs and never collide.
export function precheckConversationId(sessionRef: string): string {
  return PRECHECK_ID_PREFIX + sessionRef;
}

export function isPrecheckConversationId(conversationId: string): boolean {
  return conversationId.startsWith(PRECHECK_ID_PREFIX);
}

// The `session_ref` back out of a namespaced id. Returns null for a secretarIA
// id, so a caller cannot accidentally address PreCheck with a hub UUID.
export function precheckSessionRef(conversationId: string): string | null {
  if (!isPrecheckConversationId(conversationId)) return null;
  const ref = conversationId.slice(PRECHECK_ID_PREFIX.length);
  return ref.length > 0 ? ref : null;
}
```

with

```ts
// The id the console USED to give a PreCheck thread (`precheck:<session_ref>`). Only
// recognised now, so an old bookmark is refused instead of reaching a backend.
// secretarIA ids are hub UUIDs and never collide.
export function isPrecheckConversationId(conversationId: string): boolean {
  return conversationId.startsWith(PRECHECK_ID_PREFIX);
}
```

Delete `export type PreCheckSessionWire = {...};` (whole type), and delete the block from `// How a PreCheck thread names itself with no identity on the wire.` through the closing `}` of `export function toPrecheckConversationSummary(...)` (this removes `PRECHECK_THREAD_LABEL`, `precheckThreadName`, `toPrecheckConversationSummary`). If `initialsOf` is no longer used in `mappers.ts` after this, remove it from the imports (typecheck tells).

- [ ] **Step 6: Mapper tests** — in `lib/__tests__/real-mappers.test.ts`: remove `precheckConversationId`, `precheckSessionRef`, `toPrecheckConversationSummary` and `type PreCheckSessionWire` from the import list; delete the `pcSession` helper with its `// --- PreCheck: the second product in the same list ---` comment; delete the whole `describe("toPrecheckConversationSummary", …)`; replace the whole `describe("PreCheck conversation ids", …)` with:

```ts
describe("PreCheck conversation ids", () => {
  it("recognises the namespaced id an old link may still carry", () => {
    expect(isPrecheckConversationId("precheck:ref-abc123")).toBe(true);
  });

  it("does not claim a secretarIA hub UUID", () => {
    expect(isPrecheckConversationId("a939ed79-f092-432e-b847-6094c2abc274")).toBe(false);
  });
});
```

Keep every `toPrecheckMessages` describe block unchanged.

- [ ] **Step 7: The R8 filter loses its PreCheck line** — in `lib/conversations.ts` delete:

```ts
    // PreCheck threads have no appointment link: out while the chip is on, whatever the ids say.
    if (filter.myPatients && c.product !== "secretaria") return false;
```

In `lib/__tests__/conversations.test.ts`, in `describe("applyConversationFilter — Meus pacientes (TASK-046 R8)"`, delete the line `const withPrecheck = [...]` and replace the first test with:

```ts
  it("keeps only the ids the hub returned for mine=true", () => {
    const ids = new Set(["a", "c"]);
    expect(applyConversationFilter(list, { scope: "todas", query: "", myPatients: true }, "me", ids).map((c) => c.id)).toEqual(["a", "c"]);
  });
```

- [ ] **Step 8: Run to verify**

Run: `npm test -- lib/real/__tests__/console-no-precheck.test.ts lib/real/__tests__/console-typing.test.ts lib/__tests__/real-mappers.test.ts lib/__tests__/conversations.test.ts lib/__tests__/real-anamnesis.test.ts`
Expected: PASS. Then `npm run typecheck` → PASS (fix any leftover import the compiler names; nothing else may import the deleted symbols — `grep -rn "precheck-hub\|mintPrecheckToken\|toPrecheckConversationSummary\|precheckSessionRef" lib components app` must print nothing).

- [ ] **Step 9: Commit**

```bash
git add lib/real/console-api.real.ts lib/real/brain-session.ts lib/real/mappers.ts lib/__tests__/real-mappers.test.ts lib/conversations.ts lib/__tests__/conversations.test.ts lib/real/__tests__/console-no-precheck.test.ts
git commit -m "feat(console): PreCheck conversations never appear in the staff console (TASK-048 R9)

The list is secretarIA's alone; a precheck: id answers 404 before any request.
The console's own PreCheck client (precheck-hub, mintPrecheckToken) is removed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(`git rm` in Step 4 already staged the deletion.)

---

### Task B2: Console UI without PreCheck branches

**Files:**
- Modify: `lib/types.ts:122-134` (`ConversationProduct`, `ConversationSummary.id` comment)
- Modify: `components/console/ConversationRow.tsx`
- Modify: `components/console/Thread.tsx` (~32, ~69, ~88-108, ~146-152, ~207-217)
- Modify: `components/console/ModeSwitch.tsx:21-35`
- Modify: `components/console/console.css:72-74`
- Modify: `lib/text.ts:69-82`, `lib/__tests__/text.test.ts:3,59-68`
- Modify: `lib/mock/data.ts` (the `c-camila` PreCheck conversation)

**Interfaces:**
- Consumes: Task B1 (no code path produces `product: "precheck"` any more).
- Produces: `export type ConversationProduct = "secretaria";` — tsc rejects any remaining `=== "precheck"` comparison on a console conversation. `ModeSwitch` no longer takes `automationLabel`. `PRODUCT_LABELS` / `PRODUCT_CONDUCTOR_LABELS` no longer exist.

- [ ] **Step 1: Narrow the type (the failing check)** — in `lib/types.ts` replace the comment + type

```ts
// WHICH PRODUCT of the family carries this conversation. The console lists
// both in one place because a clinic asking "what is happening right now" is
// not asking about transports, but the two are not interchangeable: only a
// `secretaria` thread can be answered from here (PreCheck's staff view is
// read-only by design — see docs/CHECKPOINT_console_precheck.md), and the
// bubble/label colours differ per voice (tokens.css --precheck-*).
export type ConversationProduct = "secretaria" | "precheck";
```

with

```ts
// WHICH PRODUCT of the family carries this conversation. Only secretarIA since
// 2026-10-10: PreCheck conversations never appear in the console (secretarIA spec
// 2026-10-09 §6.1, docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md). The field
// stays because the typing indicator speaks per product (lib/typing.ts::typingView).
export type ConversationProduct = "secretaria";
```

and replace the `id` comment inside `ConversationSummary`

```ts
  // For a PreCheck thread this is NOT a bare id: it is namespaced
  // `precheck:<session_ref>` so that routing a call by conversation id alone is
  // a pure string decision (lib/real/mappers.ts::isPrecheckConversationId),
  // surviving a reload or a pasted /chat?conversa=... URL with no lookup.
  id: string;
```

with

```ts
  // A secretarIA hub id. A `precheck:<session_ref>` id (an old link) is refused as
  // "not found" before any request (lib/real/console-api.real.ts::refusePrecheckId).
  id: string;
```

- [ ] **Step 2: Run typecheck to see it fail**

Run: `npm run typecheck`
Expected: FAIL — TS2367 ("This comparison appears to be unintentional…") in `ConversationRow.tsx` and `Thread.tsx`, and TS errors on `PRODUCT_LABELS.precheck` / `PRODUCT_CONDUCTOR_LABELS[product]` indexing if any.

- [ ] **Step 3: ConversationRow** — in `components/console/ConversationRow.tsx`:

replace the imports

```ts
import { MODE_LABELS, PRODUCT_CONDUCTOR_LABELS, PRODUCT_LABELS } from "@/lib/text";
```
```ts
import { Badge, CountBadge, StateDot } from "../primitives/Badge";
```

with

```ts
import { MODE_LABELS } from "@/lib/text";
```
```ts
import { CountBadge, StateDot } from "../primitives/Badge";
```

replace

```ts
  const { patient, lastMessagePreview, lastMessageAt, unreadCount, state, mode, product } = conversation;
  // WHICH automation is conducting — a PreCheck questionnaire is not secretarIA.
  const conductorLabel = mode === "automacao" ? PRODUCT_CONDUCTOR_LABELS[product] : MODE_LABELS.humano;
```

with

```ts
  const { patient, lastMessagePreview, lastMessageAt, unreadCount, state, mode } = conversation;
  const conductorLabel = MODE_LABELS[mode];
```

replace

```tsx
              <span className={product === "precheck" ? "row-mode row-mode--precheck" : "row-mode"} title={conductorLabel}>
```

with

```tsx
              <span className="row-mode" title={conductorLabel}>
```

and delete

```tsx
            {/* Which product this thread belongs to. Shown only for PreCheck:
                secretarIA is the list's default and labelling every row would
                be noise. Colour comes from the measured --precheck-* tokens. */}
            {product === "precheck" && <Badge tone="precheck">{PRODUCT_LABELS.precheck}</Badge>}
```

- [ ] **Step 4: Thread** — in `components/console/Thread.tsx`:

delete the import line `import { PRODUCT_CONDUCTOR_LABELS } from "@/lib/text";` and the constant line `const PRECHECK_READ_ONLY = "Uma pré-consulta do PreCheck é somente leitura no console.";`

replace

```ts
  // One rule for typing AND for answering on the patient's behalf.
  const readOnly = conversation.product === "precheck";
  const mayAnswer = canSendMessages(me.user.role) && !readOnly;
  const answerDisabledReason = readOnly ? PRECHECK_READ_ONLY : canSendMessages(me.user.role) ? null : CANNOT_ANSWER;
```

with

```ts
  // One rule for typing AND for answering on the patient's behalf.
  const mayAnswer = canSendMessages(me.user.role);
  const answerDisabledReason = mayAnswer ? null : CANNOT_ANSWER;
```

replace

```ts
  // The handover switch too: there is no taking over a questionnaire.
  const modeDisabledReason = readOnly
    ? PRECHECK_READ_ONLY
    : !mayAnswer
      ? CANNOT_ANSWER
      : automationPaused && conversation.mode === "humano"
        ? AUTOMATION_PAUSED
        : null;
```

with

```ts
  const modeDisabledReason = !mayAnswer
    ? CANNOT_ANSWER
    : automationPaused && conversation.mode === "humano"
      ? AUTOMATION_PAUSED
      : null;
```

replace

```tsx
          <ModeSwitch
            mode={conversation.mode}
            automationLabel={PRODUCT_CONDUCTOR_LABELS[conversation.product]}
            onRequest={handover.request}
```

with

```tsx
          <ModeSwitch
            mode={conversation.mode}
            onRequest={handover.request}
```

replace

```tsx
      {readOnly ? (
        <p className="thread-hint">{PRECHECK_READ_ONLY}</p>
      ) : (
        conversation.mode === "automacao" &&
        mayAnswer && (
          // Coexistence rule as the mock models it: a human reply takes the
          // conversation over. Said BEFORE the person types, not after.
          <p className="thread-hint">A secretarIA está conduzindo. Ao enviar uma mensagem, você assume o atendimento.</p>
        )
      )}
```

with

```tsx
      {conversation.mode === "automacao" && mayAnswer && (
        // Coexistence rule as the mock models it: a human reply takes the
        // conversation over. Said BEFORE the person types, not after.
        <p className="thread-hint">A secretarIA está conduzindo. Ao enviar uma mensagem, você assume o atendimento.</p>
      )}
```

- [ ] **Step 5: ModeSwitch** — in `components/console/ModeSwitch.tsx` replace

```ts
  mode: ConversationMode;
  // Which automation conducts when `mode` is "automacao". Defaults to
  // secretarIA because that was the only automation in this list until PreCheck
  // threads joined it; a PreCheck thread must not read "secretarIA conduz".
  automationLabel?: string;
  onRequest: (next: ConversationMode) => void;
```

with

```ts
  mode: ConversationMode;
  onRequest: (next: ConversationMode) => void;
```

and

```ts
export function ModeSwitch({ mode, onRequest, disabledReason, busy = false, automationLabel = MODE_LABELS.automacao }: Props) {
  const id = useId();
  const on = mode === "automacao";
  const label = on ? automationLabel : MODE_LABELS.humano;
```

with

```ts
export function ModeSwitch({ mode, onRequest, disabledReason, busy = false }: Props) {
  const id = useId();
  const on = mode === "automacao";
  const label = MODE_LABELS[mode];
```

- [ ] **Step 6: Labels, CSS, mock**

`lib/text.ts`: delete the comment block `// WHICH product carries the conversation (ConversationProduct). …` and both `export const PRODUCT_LABELS = {…} as const;` and `export const PRODUCT_CONDUCTOR_LABELS = {…} as const;`.

`lib/__tests__/text.test.ts`: change the import to `import { MODE_LABELS, flattenPreview, formatBytes, initialsOf, initialsOfEmail } from "../text";` and delete the whole `describe("PRODUCT_CONDUCTOR_LABELS", …)` block. (If `MODE_LABELS` becomes unused in that file, drop it from the import too.)

`components/console/console.css`: delete

```css
/* A PreCheck thread is conducted by PreCheck, not by secretarIA: the marker
   speaks in that voice (tokens measured on precheck.com.br, tokens.css). */
.row-mode--precheck { color: var(--precheck-accent); }
```

(`.msg--precheck` / `.bubble-sender--precheck` stay — the patient portal and the kept bubble mapping use them.)

`lib/mock/data.ts` — remove the mock PreCheck conversation `c-camila`:
1. delete the comment block starting `// The clinic's PreCheck channel. PreCheck and secretarIA run on SEPARATE WhatsApp` through `export const PRECHECK_CONVERSATION_ID = "c-camila";`;
2. in the conversation list delete the object whose comment starts `// The PreCheck channel (see PRECHECK_CONVERSATION_ID)` (from its `{` to its `},`, the one with `id: PRECHECK_CONVERSATION_ID`);
3. in the messages map delete from the comment `// The PreCheck channel: two voices only, the questionnaire already finished.` through the `],` that closes `[PRECHECK_CONVERSATION_ID]: [` (just before `// Interactive flows (lib/mock/interactive-data.ts)`);
4. in the context map delete the entry `[PRECHECK_CONVERSATION_ID]: { … },` (the one with `summary: { source: "precheck", … }`).

- [ ] **Step 7: Run all gates**

Run: `npm run typecheck` → PASS; `grep -rn "PRECHECK_CONVERSATION_ID\|PRODUCT_LABELS\|PRODUCT_CONDUCTOR_LABELS\|row-mode--precheck\|PRECHECK_READ_ONLY" lib components app` → nothing; `npm test` → PASS (a mock-list count assertion that included `c-camila`, e.g. in `components/console/__tests__/conversation-list.test.tsx` or `lib/__tests__/portal-demo.test.ts`, drops by one — adjust it and say so in the commit body).

- [ ] **Step 8: Commit**

```bash
git add lib/types.ts components/console/ConversationRow.tsx components/console/Thread.tsx components/console/ModeSwitch.tsx components/console/console.css lib/text.ts lib/__tests__/text.test.ts lib/mock/data.ts
git commit -m "refactor(console): drop the PreCheck branches of the console UI (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Add any count-adjusted test file by name.)

---

### Task B3: Contexto — "Lembrete extra" as days + hour

**Files:**
- Modify (rewrite): `lib/contexto/reminders.ts`
- Modify: `lib/contexto/types.ts:173-203` (`Reminders`, `DEFAULT_REMINDERS`)
- Modify: `lib/contexto/hub-mapping.ts` (import ~40, `applyWireReminders` ~345-362, `buildConfigUpdatePayload` ~405-450)
- Modify: `lib/contexto/snapshot.ts:283-285` (`comparableReminders`)
- Modify: `lib/contexto/screen.ts:333` (action union) and ~580-584 (reducer)
- Modify: `lib/real/secretaria-hub.ts:331-335, 366-367` (wire types)
- Modify (rewrite): `components/contexto/ReminderSection.tsx`
- Modify: `components/contexto/ContextoView.tsx:340-345`
- Modify (rewrite): `lib/contexto/__tests__/reminders.test.ts`, `components/contexto/__tests__/reminder-section.test.tsx`

**Interfaces:**
- Consumes: the wire in "Produces for Part B" (keys `reminder_extra_days_before`, `reminder_extra_send_time`).
- Produces: `Reminders = { supported; v2Enabled; extraDaysBefore: number | null; extraSendTime: string | null; paidSupported; paidAutoApproved; paidInitial }`; screen actions `{ type: "set_reminder_days"; days: number | null }` and `{ type: "set_reminder_time"; time: string }`; `ReminderSection` props `{ v, setDays, setTime, setPaid, readOnly? }`.

- [ ] **Step 1: Rewrite the slice tests** — replace the whole content of `lib/contexto/__tests__/reminders.test.ts` with:

```ts
// The "Lembretes e avisos" slice: hydration is fail-open; each key is written only when the
// backend exposed it. Extra reminder = "N dias antes, às HH:MM" (TASK-048 R9, spec §6.2).
import { describe, expect, it } from "vitest";

import { applyWireReminders, buildConfigUpdatePayload } from "../hub-mapping";
import { DEFAULT_REMINDERS, type Reminders } from "../types";
import { tenantSlicesFromWire } from "../snapshot";
import { INITIAL_SCREEN_STATE, screenReducer } from "../screen";
import { dayOptions, extraInRange, parseDays, sendTimeOptions } from "../reminders";
import type { TenantConfigWire } from "@/lib/real/secretaria-hub";
import { TENANT_ID, tenantWire } from "./fixtures";

const payloadFor = (wire = tenantWire(), reminders?: Reminders) => {
  const t = tenantSlicesFromWire(wire);
  return buildConfigUpdatePayload(
    t.ctx, t.messages, t.postConsult, t.pixDeposit, t.clinicFacts, t.prefs.defaultDur, t.gcal.mode, t.clinicDays, reminders,
  );
};
const r = (over: Partial<Reminders> = {}): Reminders => ({ ...DEFAULT_REMINDERS, ...over });

describe("applyWireReminders", () => {
  it("an older backend (no key at all) supports nothing and reads as off", () => {
    expect(applyWireReminders(tenantWire())).toEqual(DEFAULT_REMINDERS);
    expect(DEFAULT_REMINDERS).toEqual({
      supported: false, v2Enabled: false, extraDaysBefore: null, extraSendTime: null,
      paidSupported: false, paidAutoApproved: false, paidInitial: false,
    });
  });

  it("an R7-only backend (minutes key, no day key) leaves the extra reminder hidden", () => {
    const r7 = { ...tenantWire(), reminders_v2_enabled: true, reminder_extra_lead_minutes: 7200 } as TenantConfigWire;
    expect(applyWireReminders(r7).supported).toBe(false);
  });

  it("R7's paid-notice flag alone makes the switch supported, with its stored value", () => {
    expect(applyWireReminders(tenantWire({ paid_notices_auto_approved: true }))).toEqual(r({ paidSupported: true, paidAutoApproved: true, paidInitial: true }));
    expect(applyWireReminders(tenantWire({ paid_notices_auto_approved: false }))).toEqual(r({ paidSupported: true }));
  });

  it("the day key present (even null) makes it supported; null is 'no extra reminder'", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: false, reminder_extra_days_before: null, reminder_extra_send_time: null }))).toEqual(r({ supported: true }));
  });

  it("reads the days, the hour and the read-only v2 flag", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: true, reminder_extra_days_before: 5, reminder_extra_send_time: "08:30" }))).toEqual(
      r({ supported: true, v2Enabled: true, extraDaysBefore: 5, extraSendTime: "08:30" }),
    );
  });

  it("garbage reads as off, never as a half setting", () => {
    const bad: Array<[unknown, unknown]> = [[0, "09:00"], [-1, "09:00"], [2.5, "09:00"], [Number.NaN, "09:00"], [3, "9:00"], [3, "25:00"], [3, null]];
    for (const [days, time] of bad) {
      const out = applyWireReminders(tenantWire({ reminder_extra_days_before: days as number, reminder_extra_send_time: time as string }));
      expect([out.supported, out.extraDaysBefore, out.extraSendTime]).toEqual([true, null, null]);
    }
  });

  it("tenantSlicesFromWire carries it", () => {
    const t = tenantSlicesFromWire(tenantWire({ reminder_extra_days_before: 3, reminder_extra_send_time: "07:00" }));
    expect([t.reminders.extraDaysBefore, t.reminders.extraSendTime]).toEqual([3, "07:00"]);
  });
});

describe("buildConfigUpdatePayload — the extra reminder and the paid flag", () => {
  it("NEVER sends any reminder key when the backend did not expose it", () => {
    for (const p of [payloadFor(), payloadFor(tenantWire(), DEFAULT_REMINDERS)]) {
      expect("reminder_extra_days_before" in p).toBe(false);
      expect("reminder_extra_send_time" in p).toBe(false);
      expect("paid_notices_auto_approved" in p).toBe(false);
    }
  });

  it("on: both keys together; off: both as explicit null", () => {
    const on = payloadFor(tenantWire(), r({ supported: true, extraDaysBefore: 5, extraSendTime: "08:30" }));
    expect([on.reminder_extra_days_before, on.reminder_extra_send_time]).toEqual([5, "08:30"]);
    const off = payloadFor(tenantWire(), r({ supported: true }));
    expect("reminder_extra_days_before" in off && "reminder_extra_send_time" in off).toBe(true);
    expect([off.reminder_extra_days_before, off.reminder_extra_send_time]).toEqual([null, null]);
  });

  it("a stored value off the grid is not re-sent (it would 422 the whole save); the bounds are", () => {
    for (const [days, time] of [[1, "09:00"], [15, "09:00"], [3, "05:45"], [3, "22:15"], [3, "07:10"]] as const) {
      const p = payloadFor(tenantWire(), r({ supported: true, extraDaysBefore: days, extraSendTime: time }));
      expect("reminder_extra_days_before" in p || "reminder_extra_send_time" in p).toBe(false);
    }
    expect(payloadFor(tenantWire(), r({ supported: true, extraDaysBefore: 2, extraSendTime: "06:00" })).reminder_extra_days_before).toBe(2);
    expect(payloadFor(tenantWire(), r({ supported: true, extraDaysBefore: 14, extraSendTime: "22:00" })).reminder_extra_send_time).toBe("22:00");
  });

  it("never sends the legacy minutes nor the read-only v2 flag", () => {
    const p = payloadFor(tenantWire(), r({ supported: true, v2Enabled: true, extraDaysBefore: 5, extraSendTime: "09:00" }));
    expect("reminder_extra_lead_minutes" in p).toBe(false);
    expect("reminders_v2_enabled" in p).toBe(false);
  });

  it("the paid flag travels only when it differs from the hydrated value — false included", () => {
    expect(payloadFor(tenantWire(), r({ paidSupported: true, paidInitial: false, paidAutoApproved: true })).paid_notices_auto_approved).toBe(true);
    expect(payloadFor(tenantWire(), r({ paidSupported: true, paidInitial: true, paidAutoApproved: false })).paid_notices_auto_approved).toBe(false);
    const hydrated = applyWireReminders(tenantWire({ paid_notices_auto_approved: true }));
    expect("paid_notices_auto_approved" in payloadFor(tenantWire(), hydrated)).toBe(false);
  });
});

describe("set_reminder_days / set_reminder_time / set_paid_notices", () => {
  const loaded = () => {
    let st = screenReducer(INITIAL_SCREEN_STATE, { type: "session_resolved", session: { token: "x.e30.y", tenantId: TENANT_ID, email: "d@exemplo.test", role: "manager", userId: "u" } });
    st = screenReducer(st, { type: "hydration", action: { type: "hydration_started", generation: 2, rosterGeneration: 2 } });
    return screenReducer(st, {
      type: "tenant_loaded", generation: 2, tenantId: TENANT_ID,
      wire: tenantWire({ reminders_v2_enabled: true, reminder_extra_days_before: null, reminder_extra_send_time: null, paid_notices_auto_approved: true }),
    });
  };

  it("turning it on pre-selects 09:00; the hour then changes alone; off clears both", () => {
    let st = screenReducer(loaded(), { type: "set_reminder_days", days: 5 });
    expect([st.tenant.reminders.extraDaysBefore, st.tenant.reminders.extraSendTime]).toEqual([5, "09:00"]);
    st = screenReducer(st, { type: "set_reminder_time", time: "07:30" });
    st = screenReducer(st, { type: "set_reminder_days", days: 3 });
    expect([st.tenant.reminders.extraDaysBefore, st.tenant.reminders.extraSendTime]).toEqual([3, "07:30"]);
    st = screenReducer(st, { type: "set_reminder_days", days: null });
    expect([st.tenant.reminders.extraDaysBefore, st.tenant.reminders.extraSendTime]).toEqual([null, null]);
  });

  it("the paid switch changes only its own field; the support flags stay as hydrated", () => {
    const st = screenReducer(screenReducer(loaded(), { type: "set_reminder_days", days: 5 }), { type: "set_paid_notices", value: false });
    expect(st.tenant.reminders).toEqual({
      supported: true, v2Enabled: true, extraDaysBefore: 5, extraSendTime: "09:00",
      paidSupported: true, paidAutoApproved: false, paidInitial: true,
    });
  });
});

describe("options", () => {
  it("days: Desligado first, then 2..14", () => {
    const opts = dayOptions(null);
    expect(opts[0]).toEqual({ value: "off", label: "Desligado" });
    expect(opts.map((o) => o.value)).toEqual(["off", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14"]);
    expect(opts.find((o) => o.value === "5")?.label).toBe("5 dias antes");
  });

  it("days: a stored value outside the range still shows, in place", () => {
    expect(dayOptions(1).slice(0, 3)).toEqual([{ value: "off", label: "Desligado" }, { value: "1", label: "1 dia antes" }, { value: "2", label: "2 dias antes" }]);
  });

  it("hours: 06:00..22:00 every 15 minutes; a stored off-grid hour shows in place", () => {
    const opts = sendTimeOptions(null);
    expect(opts).toHaveLength(65);
    expect([opts[0].value, opts[opts.length - 1].value]).toEqual(["06:00", "22:00"]);
    const values = sendTimeOptions("07:10").map((o) => o.value);
    expect(values.slice(values.indexOf("07:00"), values.indexOf("07:00") + 3)).toEqual(["07:00", "07:10", "07:15"]);
  });

  it("parseDays and extraInRange", () => {
    expect([parseDays("off"), parseDays(""), parseDays("abc"), parseDays("-3"), parseDays("7")]).toEqual([null, null, null, null, 7]);
    expect(extraInRange(2, "06:00")).toBe(true);
    expect(extraInRange(14, "22:00")).toBe(true);
    expect(extraInRange(1, "09:00") || extraInRange(15, "09:00") || extraInRange(3, "22:15") || extraInRange(3, "07:10")).toBe(false);
  });
});
```

- [ ] **Step 2: Rewrite the section test** — replace the `describe("ReminderSection — extra reminder", …)` block and the `html` helper / call sites in `components/contexto/__tests__/reminder-section.test.tsx` so the file reads:

```tsx
// ReminderSection is hook-free: markup via renderToStaticMarkup, and the element tree walked for the
// controls' real props (same idiom as pix-section.test.tsx).
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactElement } from "react";

import { ReminderSection } from "../ReminderSection";
import { Select } from "../../primitives/Select";
import { Toggle } from "../../primitives/Toggle";
import { DEFAULT_REMINDERS, type Reminders } from "@/lib/contexto/types";

function findAllByType(el: unknown, type: unknown, out: ReactElement[] = []): ReactElement[] {
  if (!el || typeof el !== "object") return out;
  const node = el as ReactElement<{ children?: unknown }>;
  if (node.type === type) out.push(node);
  const children = node.props?.children;
  for (const child of Array.isArray(children) ? children : [children]) findAllByType(child, type, out);
  return out;
}

const both: Reminders = { ...DEFAULT_REMINDERS, supported: true, paidSupported: true };
const on: Reminders = { ...both, extraDaysBefore: 5, extraSendTime: "08:30" };
const noop = () => {};
const html = (v: Reminders, readOnly = false) =>
  renderToStaticMarkup(<ReminderSection v={v} setDays={noop} setTime={noop} setPaid={noop} readOnly={readOnly} />);
const tree = (v: Reminders, over: Partial<Parameters<typeof ReminderSection>[0]> = {}) =>
  ReminderSection({ v, setDays: noop, setTime: noop, setPaid: noop, ...over });

describe("ReminderSection — paid notices (R7)", () => {
  it("a named switch says in words whether paid notices go without asking", () => {
    const off = html({ ...DEFAULT_REMINDERS, paidSupported: true });
    expect(off).toContain('aria-label="Lembretes e avisos"');
    expect(off).toContain('role="switch" aria-checked="false" aria-label="Enviar avisos pagos sem perguntar"');
    expect(off).toContain("A agenda pergunta antes de cada aviso pago.");
    const yes = html({ ...DEFAULT_REMINDERS, paidSupported: true, paidAutoApproved: true });
    expect(yes).toContain('aria-checked="true"');
    expect(yes).toContain("Os avisos pagos saem sem perguntar.");
  });

  it("wires the switch to setPaid and locks it while read-only", () => {
    const setPaid = vi.fn();
    const toggle = findAllByType(tree({ ...DEFAULT_REMINDERS, paidSupported: true, paidAutoApproved: true }, { setPaid }), Toggle)[0];
    (toggle.props as { onChange: (v: boolean) => void }).onChange(false);
    expect(setPaid).toHaveBeenCalledWith(false);
    const locked = findAllByType(tree({ ...DEFAULT_REMINDERS, paidSupported: true }, { setPaid, readOnly: true }), Toggle)[0];
    expect((locked.props as { disabled?: boolean }).disabled).toBe(true);
  });

  it("no paid switch when the backend does not expose the flag", () => {
    expect(html({ ...DEFAULT_REMINDERS, supported: true })).not.toContain('role="switch"');
  });
});

describe("ReminderSection — extra reminder (dias + horário)", () => {
  it("off: one labelled select with Desligado selected, and no hour field", () => {
    const out = html(both);
    expect(out).toMatch(/<label for="[^"]+" class="field-label">Lembrete extra<\/label>/);
    expect(out).toContain('<option value="off" selected="">Desligado</option>');
    expect(out).not.toContain("Horário do envio");
  });

  it("on: the chosen day and a labelled hour select with the chosen hour", () => {
    const out = html(on);
    expect(out).toContain('<option value="5" selected="">5 dias antes</option>');
    expect(out).toMatch(/<label for="[^"]+" class="field-label">Horário do envio<\/label>/);
    expect(out).toContain('<option value="08:30" selected="">08:30</option>');
    expect(out).toContain("fuso horário da clínica");
  });

  it("says in words whether the automatic reminders are on, never by colour alone", () => {
    expect(html(both)).toContain("Lembretes ainda não ativos");
    const active = html({ ...both, v2Enabled: true });
    expect(active).toContain("Lembretes ativos");
    expect(active).not.toContain("ainda não ativos");
  });

  it("wires the day select to setDays and the hour select to setTime; read-only locks both", () => {
    const setDays = vi.fn();
    const setTime = vi.fn();
    const [days, hour] = findAllByType(tree(on, { setDays, setTime }), Select);
    const dayProps = days.props as { value: string; onChange: (v: string) => void };
    expect(dayProps.value).toBe("5");
    dayProps.onChange("7");
    dayProps.onChange("off");
    expect(setDays.mock.calls).toEqual([[7], [null]]);
    (hour.props as { onChange: (v: string) => void }).onChange("07:30");
    expect(setTime).toHaveBeenCalledWith("07:30");
    for (const select of findAllByType(tree(on, { readOnly: true }), Select)) {
      expect((select.props as { disabled?: boolean }).disabled).toBe(true);
    }
  });

  it("no extra-reminder field while the backend does not expose it", () => {
    expect(html({ ...DEFAULT_REMINDERS, paidSupported: true })).not.toContain("Lembrete extra");
  });
});
```

- [ ] **Step 3: Run to verify they fail**

Run: `npm test -- lib/contexto/__tests__/reminders.test.ts components/contexto/__tests__/reminder-section.test.tsx`
Expected: FAIL — `dayOptions`/`sendTimeOptions`/`extraInRange`/`parseDays` not exported; `extraDaysBefore` undefined.

- [ ] **Step 4: Rewrite `lib/contexto/reminders.ts`**

```ts
// lib/contexto/reminders.ts — the choices behind the "Lembrete extra" fields (TASK-048 R9,
// secretarIA spec 2026-10-09 §6.2): "N dias antes, às HH:MM" in the clinic's time zone.
// Pure, so a stored value off the grid is testable without a DOM. The ranges mirror
// secretarIA core/extra_reminder.py (2..14 days; 06:00..22:00 in 15-minute steps).

export const EXTRA_DAYS_MIN = 2;
export const EXTRA_DAYS_MAX = 14;
export const DEFAULT_SEND_TIME = "09:00";
const FIRST_MINUTE = 6 * 60;
const LAST_MINUTE = 22 * 60;
const STEP_MINUTES = 15;
const CLOCK = /^([01][0-9]|2[0-3]):([0-5][0-9])$/;

export type ReminderOption = { value: string; label: string };

function minuteOfDay(hhmm: string): number | null {
  const m = CLOCK.exec(hhmm);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function clock(minute: number): string {
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

/** Any well-formed "HH:MM" — a stored hour is shown even when it is off the grid. */
export function isClockTime(value: unknown): value is string {
  return typeof value === "string" && CLOCK.test(value);
}

/** What secretarIA accepts on save: 2..14 days, 06:00..22:00 in 15-minute steps. */
export function extraInRange(days: number, time: string): boolean {
  const minute = minuteOfDay(time);
  return (
    Number.isInteger(days) && days >= EXTRA_DAYS_MIN && days <= EXTRA_DAYS_MAX &&
    minute !== null && minute >= FIRST_MINUTE && minute <= LAST_MINUTE && minute % STEP_MINUTES === 0
  );
}

export function formatDays(days: number): string {
  return `${days} ${days === 1 ? "dia" : "dias"} antes`;
}

/** "Desligado" first, then 2..14; a stored value outside the range is added IN PLACE. */
export function dayOptions(current: number | null): ReminderOption[] {
  const days: number[] = [];
  for (let d = EXTRA_DAYS_MIN; d <= EXTRA_DAYS_MAX; d++) days.push(d);
  if (current !== null && !days.includes(current)) days.push(current);
  days.sort((a, b) => a - b);
  return [{ value: "off", label: "Desligado" }, ...days.map((d) => ({ value: String(d), label: formatDays(d) }))];
}

/** 06:00..22:00 every 15 minutes; a stored off-grid hour is added IN PLACE. */
export function sendTimeOptions(current: string | null): ReminderOption[] {
  const times: string[] = [];
  for (let m = FIRST_MINUTE; m <= LAST_MINUTE; m += STEP_MINUTES) times.push(clock(m));
  if (current !== null && isClockTime(current) && !times.includes(current)) times.push(current);
  times.sort();
  return times.map((t) => ({ value: t, label: t }));
}

/** The day select's value -> days; "off" and anything unusable -> null (no extra reminder). */
export function parseDays(value: string): number | null {
  if (value === "off") return null;
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}
```

- [ ] **Step 5: Types, wire, mapping, snapshot, reducer**

`lib/contexto/types.ts` — replace the `Reminders` block comment line ``// `supported` <- `reminder_extra_lead_minutes` (R7 Task 16),`` with ``// `supported` <- `reminder_extra_days_before` (R9; absent on an older backend),``, replace

```ts
  /** Minutes before the appointment; null = no extra reminder. */
  extraLeadMinutes: number | null;
```

with

```ts
  /** Days before the appointment (2..14); null = no extra reminder. */
  extraDaysBefore: number | null;
  /** "HH:MM" in the clinic's time zone; null while off. */
  extraSendTime: string | null;
```

and in `DEFAULT_REMINDERS` replace `extraLeadMinutes: null,` with `extraDaysBefore: null,\n  extraSendTime: null,`.

`lib/real/secretaria-hub.ts` — in `TenantConfigWire` replace

```ts
  // TASK-032 reminders, exposed by R7 Task 16. Both ABSENT on an older backend:
  // the extra-reminder field is hidden then and never written.
  // `reminder_extra_lead_minutes`: 1500..20160 minutes; null = no extra reminder.
  reminders_v2_enabled?: boolean;
  reminder_extra_lead_minutes?: number | null;
```

with

```ts
  // Reminders: `reminders_v2_enabled` (READ-ONLY) and the extra reminder as
  // "N dias antes, às HH:MM" (TASK-048 R9): days 2..14, "HH:MM" 06:00..22:00 / 15 min,
  // both null = off. ABSENT on a backend older than R9: the field is hidden then and
  // never written. (secretarIA also sends the legacy `reminder_extra_lead_minutes`;
  // this screen ignores it.)
  reminders_v2_enabled?: boolean;
  reminder_extra_days_before?: number | null;
  reminder_extra_send_time?: string | null;
```

and in `TenantConfigUpdatePayload` replace `reminder_extra_lead_minutes: number | null;` with

```ts
  // Both keys together, or neither (secretarIA 422s a half pair).
  reminder_extra_days_before: number | null;
  reminder_extra_send_time: string | null;
```

`lib/contexto/hub-mapping.ts` — replace `import { leadInRange } from "./reminders";` with `import { extraInRange, isClockTime } from "./reminders";`; in `applyWireReminders` replace

```ts
  if (cfg.reminder_extra_lead_minutes === undefined) return { ...DEFAULT_REMINDERS, ...paidPart };
  const lead = cfg.reminder_extra_lead_minutes;
  return {
    supported: true,
    v2Enabled: cfg.reminders_v2_enabled === true,
    extraLeadMinutes: typeof lead === "number" && Number.isFinite(lead) && lead > 0 ? Math.trunc(lead) : null,
    ...paidPart,
  };
```

with

```ts
  if (cfg.reminder_extra_days_before === undefined) return { ...DEFAULT_REMINDERS, ...paidPart };
  const days = cfg.reminder_extra_days_before;
  const time = cfg.reminder_extra_send_time;
  // Both usable or the reminder reads as off: never a half setting on screen.
  const extra =
    typeof days === "number" && Number.isInteger(days) && days > 0 && isClockTime(time)
      ? { extraDaysBefore: days, extraSendTime: time }
      : { extraDaysBefore: null, extraSendTime: null };
  return { supported: true, v2Enabled: cfg.reminders_v2_enabled === true, ...extra, ...paidPart };
```

update its leading comment's second sentence to: `// A missing, non-positive or malformed day/hour reads as "no extra reminder"; only the literal \`true\` approves paid notices.`; in the `buildConfigUpdatePayload` comment replace `Lembretes e avisos (reminder_extra_lead_minutes / paid_notices_auto_approved) only` with `Lembretes e avisos (reminder_extra_days_before + reminder_extra_send_time / paid_notices_auto_approved) only`; and replace

```ts
  // Only what the backend exposed: an explicit null turns the extra reminder off.
  // A legacy lead outside R7's 1500..20160 is left out (absent = unchanged):
  // resending it would 422 the whole save, and the clinic did not touch it.
  const lead = reminders?.extraLeadMinutes ?? null;
  if (reminders?.supported && (lead === null || leadInRange(lead))) payload.reminder_extra_lead_minutes = lead;
```

with

```ts
  // Only what the backend exposed, and both keys together: explicit nulls turn the
  // extra reminder off. A stored value off the grid (set by hand) is left out (absent
  // = unchanged): resending it would 422 the whole save, and the clinic did not touch it.
  if (reminders?.supported) {
    const { extraDaysBefore: days, extraSendTime: time } = reminders;
    if (days === null) {
      payload.reminder_extra_days_before = null;
      payload.reminder_extra_send_time = null;
    } else if (time !== null && extraInRange(days, time)) {
      payload.reminder_extra_days_before = days;
      payload.reminder_extra_send_time = time;
    }
  }
```

`lib/contexto/snapshot.ts` — replace the body of `comparableReminders` with:

```ts
  return { extraDaysBefore: r.extraDaysBefore, extraSendTime: r.extraSendTime, paidAutoApproved: r.paidAutoApproved };
```

`lib/contexto/screen.ts` — add `import { DEFAULT_SEND_TIME } from "./reminders";` with the other relative imports; replace the action member `| { type: "set_reminder_lead"; extraLeadMinutes: number | null }` with

```ts
  | { type: "set_reminder_days"; days: number | null }
  | { type: "set_reminder_time"; time: string }
```

and replace the reducer case

```ts
    case "set_reminder_lead":
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...state.tenant.reminders, extraLeadMinutes: action.extraLeadMinutes } },
      };
```

with

```ts
    case "set_reminder_days": {
      // Off clears the hour too; turning it on keeps the hour already chosen, or 09:00.
      const current = state.tenant.reminders;
      const extraSendTime = action.days === null ? null : (current.extraSendTime ?? DEFAULT_SEND_TIME);
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...current, extraDaysBefore: action.days, extraSendTime } },
      };
    }
    case "set_reminder_time":
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...state.tenant.reminders, extraSendTime: action.time } },
      };
```

- [ ] **Step 6: Rewrite `components/contexto/ReminderSection.tsx`**

```tsx
// ReminderSection — "Lembretes e avisos" (TASK-032 R5; spec 2026-10-09 §2, §6.2). Two
// independent halves, each shown only when the backend exposes its key, so no control
// ever saves nowhere:
//  - "Avisos pagos do WhatsApp" (R7 `paid_notices_auto_approved`): the agenda's
//    "Não perguntar novamente" sets it; here the clinic turns it back off (or on).
//  - "Lembrete extra" (R9 `reminder_extra_days_before` + `reminder_extra_send_time`): the
//    clinic's ONE configurable reminder, "N dias antes, às HH:MM" in its time zone; the
//    1-day and 1-hour reminders are fixed product behaviour.
//
// Hook-free (like PixSection) so tests can walk its element tree. Honest about the
// reminders switch: `reminders_v2_enabled` is the owner's per-clinic interruptor and
// is READ-ONLY here — the badge says in words whether it is on.

import { Panel } from "../primitives/Panel";
import { Select } from "../primitives/Select";
import { Badge } from "../primitives/Badge";
import { Toggle } from "../primitives/Toggle";
import { dayOptions, parseDays, sendTimeOptions } from "@/lib/contexto/reminders";
import type { Reminders } from "@/lib/contexto/types";
import "./contexto.css";

export type ReminderSectionProps = {
  v: Reminders;
  setDays: (days: number | null) => void;
  setTime: (time: string) => void;
  setPaid: (on: boolean) => void;
  /** Locked while the screen cannot save (loading, error, saving). */
  readOnly?: boolean;
};

export function ReminderSection({ v, setDays, setTime, setPaid, readOnly }: ReminderSectionProps) {
  return (
    <Panel as="section" label="Lembretes e avisos" className="contexto-section" title={<h2>Lembretes e avisos</h2>}>
      {v.paidSupported && (
        <div className="contexto-section">
          <h3 className="contexto-subtitle">Avisos pagos do WhatsApp</h3>
          <p className="contexto-section-desc">
            Quando a clínica age na agenda (confirmar, registrar presença, alterar, cancelar, liberar), a secretarIA
            avisa o paciente. Se ele estiver fora da janela de 24 h do WhatsApp, o aviso só sai como mensagem oficial,
            que é cobrada.
          </p>
          <Toggle
            on={v.paidAutoApproved}
            onChange={setPaid}
            label="Enviar avisos pagos sem perguntar"
            disabled={readOnly}
          />
          <p className="contexto-field-tip">
            {v.paidAutoApproved
              ? "Os avisos pagos saem sem perguntar. Desligue para a agenda voltar a perguntar antes de cada aviso pago."
              : "A agenda pergunta antes de cada aviso pago."}
          </p>
        </div>
      )}

      {v.supported && (
        <div className="contexto-section">
          <h3 className="contexto-subtitle">Lembretes de consulta</h3>
          <p className="contexto-section-desc">
            A secretarIA lembra o paciente 1 dia e 1 hora antes da consulta. Aqui você pode acrescentar um lembrete
            extra, mais cedo, num dia e horário que você escolhe.
          </p>
          <div>
            {v.v2Enabled ? (
              <Badge tone="compareceu" icon="check">
                Lembretes ativos
              </Badge>
            ) : (
              <Badge tone="neutral" icon="clock">
                Lembretes ainda não ativos
              </Badge>
            )}
          </div>
          {!v.v2Enabled && (
            <p className="contexto-field-tip">
              O que você escolher aqui fica salvo e passa a valer quando os lembretes forem ativados para a sua clínica.
            </p>
          )}
          <Select
            value={v.extraDaysBefore === null ? "off" : String(v.extraDaysBefore)}
            onChange={(next) => setDays(parseDays(next))}
            label="Lembrete extra"
            options={dayOptions(v.extraDaysBefore)}
            disabled={readOnly}
          />
          {v.extraDaysBefore !== null && (
            <Select
              value={v.extraSendTime ?? ""}
              onChange={setTime}
              label="Horário do envio"
              options={sendTimeOptions(v.extraSendTime)}
              disabled={readOnly}
            />
          )}
          <p className="contexto-field-tip">
            {v.extraDaysBefore === null
              ? "Fica desligado até você escolher quantos dias antes."
              : "Enviado nesse dia e horário, no fuso horário da clínica, antes dos lembretes fixos de 1 dia e 1 hora."}
          </p>
        </div>
      )}
    </Panel>
  );
}
```

`components/contexto/ContextoView.tsx` — replace

```tsx
        setLead={(minutes) => dispatch({ type: "set_reminder_lead", extraLeadMinutes: minutes })}
```

with

```tsx
        setDays={(days) => dispatch({ type: "set_reminder_days", days })}
        setTime={(time) => dispatch({ type: "set_reminder_time", time })}
```

- [ ] **Step 7: Run to verify**

Run: `npm test -- lib/contexto/__tests__/reminders.test.ts components/contexto/__tests__/reminder-section.test.tsx` → PASS.
Run: `npm run typecheck` → PASS. `grep -rn "extraLeadMinutes\|set_reminder_lead\|leadInRange\|leadOptions\|parseLead\|formatLead\|LEAD_PRESET_DAYS" lib components app` → nothing.
Run: `npm test` → PASS (a snapshot/dirty-section test that built `Reminders` with `extraLeadMinutes` is updated to the new fields).

- [ ] **Step 8: Commit**

```bash
git add lib/contexto/reminders.ts lib/contexto/types.ts lib/contexto/hub-mapping.ts lib/contexto/snapshot.ts lib/contexto/screen.ts lib/real/secretaria-hub.ts components/contexto/ReminderSection.tsx components/contexto/ContextoView.tsx lib/contexto/__tests__/reminders.test.ts components/contexto/__tests__/reminder-section.test.tsx
git commit -m "feat(contexto): extra reminder chosen as days before + local hour (TASK-048 R9)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task B4: Front checkpoint and full gates

**Files:**
- Create: `docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`
- Modify: `docs/CHECKPOINT_console_precheck.md` (banner at the top), `docs/CHECKPOINT_meus_pacientes_seletor_medico.md` (one line), `docs/CHECKPOINT_brain_message_lembretes_agenda.md` (one line)

**Interfaces:**
- Consumes: Tasks B1–B3 (commit SHAs, test counts).
- Produces: documentation.

- [ ] **Step 1: Full gates** — `npm test` (record files/tests, 0 failures), `npm run typecheck`, `npm run build` (static export; the existing rewrites warning is pre-existing).

- [ ] **Step 2: Write `docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`**

```markdown
# CHECKPOINT — R9: console sem PreCheck + lembrete extra por dia e horário (TASK-048)

Spec: secretarIA `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §6.
Plano: secretarIA `docs/superpowers/plans/2026-10-10-lembretes-r9-sem-precheck-e-lembrete-dia-hora.md` (Parte B).

## Estado

- Branch `task/TASK-048-sem-precheck-lembrete-dia-hora-front` (base: <SHA base>). Commits: <SHAs B1..B4>.
- **Local e commitado; não mesclado, não pushado, não deployado.**
- `npm test` <N> passed / <F> files; typecheck e build verdes.

## Console sem PreCheck (§6.1)

- As conversas do PreCheck entravam no console por uma chamada do próprio front ao PreCheck
  (`console-api.real.ts::precheckList/precheckThread`, cliente `precheck-hub.ts`). Removidas; o
  backend não mudou (a secretarIA nunca serviu conversas do PreCheck).
- Id `precheck:…` (link antigo/colado) → 404 "Conversa não encontrada." antes de qualquer requisição
  (`refusePrecheckId`).
- `ConversationProduct` = só `"secretaria"`; selo/estilo do PreCheck na lista, ramo "somente leitura" da
  conversa, rótulos `PRODUCT_*` e a exceção do PreCheck no filtro "Meus pacientes" removidos. Conversa
  `c-camila` do mock removida.
- Continuam: módulo Anamneses, selo "Status do PreCheck" no contexto, aba PreCheck do portal do paciente,
  `toPrecheckMessages` (cobertura dos ajudantes que o portal compartilha — decisão D17 do plano).

## Lembrete extra (§6.2)

- Configuração: "Lembrete extra" (Desligado, 2..14 dias antes) + "Horário do envio" (06:00..22:00 de 15
  em 15), no fuso da clínica. Liga com 09:00 pré-escolhido. Valor gravado fora da grade aparece no lugar e
  não é reenviado.
- Contrato: `reminder_extra_days_before` + `reminder_extra_send_time`, sempre juntos; backend anterior ao
  R9 (sem a chave) esconde o campo.

## Deploy (quando o dono autorizar)

Depois da migração e da secretarIA (API e worker juntos). O console sem PreCheck não depende do backend.

## Pendências

- Mover os testes de `toPrecheckMessages` para o caminho do portal e então remover a função.
- Prova no Chrome após deploy autorizado (lista sem PreCheck; link antigo → não encontrada; salvar
  lembrete extra).
```

Fill the `<…>` values from Step 1 and `git log`.

- [ ] **Step 3: Pointers** — add as the first line under the title of `docs/CHECKPOINT_console_precheck.md`:

```markdown
> **Revertido em 2026-10-10 (R9):** conversas do PreCheck não aparecem mais no console — ver `docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`.
```

append to `docs/CHECKPOINT_meus_pacientes_seletor_medico.md`:

```markdown
- 2026-10-10 (R9): a exceção do PreCheck no filtro "Meus pacientes" saiu — o console não lista mais conversas do PreCheck (`docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`).
```

append to `docs/CHECKPOINT_brain_message_lembretes_agenda.md`:

```markdown
- 2026-10-10 (R9): o lembrete extra passou a "N dias antes, às HH:MM" — ver `docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md`.
```

- [ ] **Step 4: Commit**

```bash
git add docs/CHECKPOINT_r9_sem_precheck_lembrete_dia_hora.md docs/CHECKPOINT_console_precheck.md docs/CHECKPOINT_meus_pacientes_seletor_medico.md docs/CHECKPOINT_brain_message_lembretes_agenda.md
git commit -m "docs: checkpoint for R9 (console without PreCheck, day+time extra reminder) (TASK-048)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Report to the owner** (lay Portuguese, no code names): o que muda para a clínica (conversas do questionário não aparecem mais no atendimento; lembrete extra escolhido por dia e horário), que nada foi publicado, e a ordem de publicação quando ele autorizar.

---

## Self-review (done while writing)

- **Spec coverage:** §6.1 → B1 (list/open/deep link/no PreCheck call), B2 (UI branches, R8 filter simplification in B1 Step 7), D18 (scope). §6.2 → A1 (rule, ranges, DST, guard, legacy conversion), A2 (columns, migration conversion), A3 (planning + replan incl. time zone, past = cancelled), A4 (wire, legacy tolerance, replan trigger incl. time zone), B3 (UI). Warning window (due + 2 h) unchanged and asserted in A3. Docs: A5, B4.
- **Placeholders:** the only `<…>` are SHAs/counts the executor copies from `git log` and the test run into the checkpoints.
- **Type consistency:** `custom_due_at`, `from_legacy_lead`, `legacy_lead_minutes`, `is_valid_send_time`, `EXTRA_DAYS_MIN/MAX` used with the same names in A1–A4; `extraDaysBefore`/`extraSendTime`, `set_reminder_days`/`set_reminder_time`, `setDays`/`setTime`, `dayOptions`/`sendTimeOptions`/`parseDays`/`extraInRange`/`isClockTime`/`DEFAULT_SEND_TIME` consistent across B3; `refusePrecheckId` and `isPrecheckConversationId` consistent across B1/B2.
- **Review Focus:** each of the five lines has a named test in its owning task.
