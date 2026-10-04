# Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta, ações, legenda) e campo do lembrete extra Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the clinic's agenda (Brain-Message-Frontend) draw each appointment from its real confirmation data — neutral / green ✓ / green ✓✓ / red — with a legend, a short reminder timeline and the three red-state actions (Liberar horário with Pix-retention acknowledgement, Enviar mensagem ao paciente, Marcar como confirmado), and give the clinic a "Lembretes" configuration field for `reminder_extra_lead_minutes`.

**Architecture:** Pure logic first (`lib/agenda/confirmation.ts`, `lib/agenda/release.ts`) so every rule is unit-tested in the repo's node-only vitest (no jsdom: markup via `renderToStaticMarkup`, reducers/controllers via fakes). The wire mapping is strictly additive and fail-open to today's look when the backend omits the new keys. Presentational components (`ConfirmationBadge`, legend, `ReleaseCard`, `MessageModal`, drawer sections) are fed by `Appt` fields; all writes go through the existing `createAgendaController` (session re-resolution, tenant guard, generation guard). The configuration field is a new `rem` section in the existing Contexto screen (`/contexto`), hydrated/saved through the existing tenant-level `PUT /tenants/me/configuration`, and only sent when the backend exposed the key.

**Tech Stack:** Next.js 15 static export, React 19, TypeScript 5, hand-written CSS design system (`app/styles/tokens.css` + per-module CSS), vitest 2 (node env). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` (§4.1 display states, §4.4 agenda, §4.5 configuration; success criteria 8 and 9). Backend contract: `docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md` (Task 7 `GET /events`, Task 8 `PATCH …/status`) and `docs/superpowers/plans/2026-10-03-lembretes-r4-avisos-e-liberar.md` (release / message; shapes copied under "Consumes").

**Repo / worktree:** all code lives in `Brain-Message-Frontend`. The main checkout `C:\TECH\BRAIN\Brain-Message-Frontend` is read-only for this plan; execution happens in a fresh worktree `C:\TECH\BRAIN-worktrees\TASK-0NN\Brain-Message-Frontend`, branch `task/TASK-0NN-lembretes-r5-front` (cut from `main`; if the owner has moved the Pix phase, from `feature/out-of-mvp` per `AI_WORKFLOW.md`). Run `git status` first: the checkout had a clean tree and head `50615f5` when this plan was written.

## Global Constraints

- UI text in **Portuguese** (sentence case, "paciente"/"clínica"), code, identifiers and comments in **English**.
- Hand-written CSS only; reuse the existing vocabulary (`btn`, `badge`, `agm-*`, `cal-*`, `agenda-*`, `contexto-*`) and tokens; no Tailwind, no new dependency, no inline hex outside `app/styles/tokens.css`. Define the new colour tokens in **both** the `[data-theme="light"]` and `[data-theme="dark"]` blocks of `tokens.css`.
- **Colour is never the only signal:** every state has a text label and an icon/tick glyph, and the accessible name of a slot/badge/action says the state in words (skill `custom-control-accessible-name`: custom controls take a required `label`; never rely on a wrapping `<label>`; visible text must be inside the accessible name).
- Motion: add **no** animation or transition for the new states; the global `@media (prefers-reduced-motion: reduce)` in `app/styles/base.css` stays the only motion rule (pinned by a CSS test, Task 2).
- Labels derived from dates come from the data's own timestamps (skill `date-derived-ui-labels`): the timeline formats the wire's ISO strings, never a constant.
- Static export rules (skill `front-brain`): no new dynamic route, no `fetch` outside `lib/agenda/secretaria-hub-agenda.ts` / `lib/real/secretaria-hub.ts` (`hubFetch`: hub token, 401 re-mint, `HubApiError`).
- Every new wire key is **optional**: an older backend (no `status`, `confirmation_count`, `display_state`, `attention`, `reminders`, no config keys) must render and behave exactly as today. Unknown `display_state`/`kind`/`answer` strings are ignored, never guessed.
- Terminal statuses keep their own look: `cancelado`, `compareceu`, `faltou` (and `bloqueio`) are never recoloured by the confirmation state; Google-only events (no `appointment_id`) and blocks are mapped exactly as before.
- Display cap: ticks never exceed **two** (`confirmation_count` is clamped to 0–2 on read).
- Validation commands (PowerShell, from the worktree): `.\node_modules\.bin\tsc.cmd --noEmit`, `npm test`, `npm run build`. **Never** `npm run lint` (ESLint is not installed; it blocks on an interactive prompt).
- `git add` only the files named in each task (never `git add -A`; parallel sessions share this machine). Commit locally; **no push, no deploy** (see "Deploy" at the end).
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ`.

## Review Focus

Most likely to bite first. Each line names the task whose tests pin it.

1. **Terminal statuses keep their own look.** An appointment whose status is `cancelled`, `attended` or `no_show` must never turn red/green/neutral from `confirmation_count`/`display_state` (a cancelled slot with `attention: true` stays struck-through; no badge, no red actions). Task 1 (`confirmationLook`), Task 2 (grid class), Task 3 (drawer).
2. **Red → green live after confirm.** After "Marcar como confirmado" succeeds, the slot and the open drawer flip to green before (and independent of) the refetch; the refetch result then wins. Task 5 (reducer/controller).
3. **✓✓ capped at two.** `confirmation_count: 5` (or `display_state: "confirmed_twice"` with a larger count) renders two ticks and "Confirmada duas vezes", never three; a negative/NaN count renders as zero. Task 1.
4. **Google-only block events unchanged.** Events with no `appointment_id` and `Bloqueado…` summaries map to the same `Appt` as before even when stray confirmation keys are present; blocks show no badge, no timeline, no red actions. Task 1, Task 3.
5. **Pix-paid release needs the acknowledgement.** With `deposit.status === "confirmado_pago"` the "Liberar horário" confirm button is disabled until the retention checkbox is ticked, no request is sent without `acknowledge_retention: true`, and a backend `retention_ack_required` refusal shows the backend's retention text and re-opens the step instead of failing silently. Task 4 (body builder), Task 5 (controller), Task 6 (card).
6. **Release failure shows an error and keeps state.** A failed release (including Google unavailable, 502) leaves the card open with the error, the appointment red and selectable, `modal.pending` false, and no toast of success; `not_live` is explained, not retried; `already_confirmed` (the patient confirmed meanwhile) needs a second, explicit click ("Liberar mesmo assim"). A release whose patient notice did not go out (`whatsapp_outside_window`, `no_channel`, failures) says so in a danger toast. Task 5, Task 6.
7. **Keyboard / screen-reader names.** The badge exposes its state as text; the red actions are real `<button>`s with names that contain their visible text; the acknowledgement is a labelled checkbox; the timeline is an ordered list with a name; icons are `aria-hidden`. Tasks 2, 3, 6.
8. **Old backend without the new keys (graceful).** No badge, no legend, no timeline, no red actions, old status look, config section hidden and `reminder_extra_lead_minutes` never sent. Tasks 1, 2, 3, 7, 8.
9. **Reduced motion and dark theme.** No new animation/transition; the new tokens exist in both theme blocks. Task 2.
10. **Lead field edge cases.** A stored value that is not one of the presets (e.g. `1500`) still shows and round-trips unchanged; "Desligado" sends `null`; the value is never sent while the section is read-only/unsupported. Tasks 7, 8.

## Consumes — backend contract (exact shapes, and what is assumed)

**From R1 (present in the plan; used verbatim).** `GET /tenants/me/calendar/events?start=&end=` items gain, all optional/nullable and `null` for events with no local `Appointment` (blocks, Google-only):

```ts
status?: "scheduled" | "confirmed" | "rescheduled" | "cancelled" | "attended" | "no_show" | null;
confirmation_count?: number | null;          // 0..2
display_state?: string | null;               // "unconfirmed" | "confirmed" | "confirmed_twice" | "attention" (terminal statuses are always "unconfirmed")
attention?: boolean | null;                  // === (display_state == "attention")
reminders?: Array<{
  kind: string;                              // "custom" | "day" | "hour" | "chat"
  status: string;                            // "pending" | "sending" | "sent" | "failed" | "skipped" | "cancelled"
  due_at: string;                            // aware UTC ISO
  sent_at?: string | null; answered_at?: string | null;
  answer?: string | null;                    // "confirm" | "cancel" | "other"
  warned_at?: string | null; warn_kind?: string | null;   // "unconfirmed" | "delivery_failed"
}> | null;                                   // only rows of the appointment's CURRENT start, ordered by due_at
```

`PATCH …/appointments/{id}/status` with `{status:"confirmed"}` goes through the counter (R1 Task 8) and answers `AppointmentRead` (the existing `AppointmentWire`).

**From R4 (`2026-10-03-lembretes-r4-avisos-e-liberar.md`, "Contract" section, read 2026-10-04; used verbatim).** Both routes need the hub bearer token; a foreign or malformed id is `404 {"detail":"Appointment not found"}`; structured errors are `{"detail": {code, message, ...}}` (the hub client exposes `HubApiError.code` and `.message` for that shape, and `.detail` as the parsed object).

```text
POST /tenants/me/calendar/appointments/{id}/release
  body   { "acknowledge_retention": false, "release_confirmed": false, "notify_outside_window": false, "justification": null }   # all optional
  200    AppointmentRead (status "cancelled", deposit_status, deposit_outcome, confirmation_count) + "patient_notice":
         "whatsapp_queued" | "whatsapp_outside_window" | "portal_chat" | "portal_chat_email" | "no_channel" | "queue_unavailable" | "notice_failed"
  409    retention_ack_required  {message (pt-BR retention text), deposit_outcome, amount_cents}   # paid Pix deposit, no acknowledgement
  409    not_live                {status}                       # already released / cancelled / attended
  409    already_confirmed       {confirmation_count}           # the patient confirmed meanwhile; re-POST with release_confirmed:true only if the clinic agrees
  409    calendar_unresolved
  422    not_a_patient_appointment (a block)  |  plain-string 422 when Google Calendar is not connected
  502    calendar_unavailable    {message}                      # Google refused: nothing changed, safe to retry
  (window/cost for the "notify outside 24 h" opt-in come from the existing GET .../cancel-preview)

POST /tenants/me/calendar/appointments/{id}/message
  body   { "text": "<1..1000 chars, trimmed, not blank>", "notify_outside_window": false }
  200    { "delivery": "whatsapp_text" | "whatsapp_template" | "portal_chat", "email_nudge": null | "sent" | "no_email" | "not_sent", "message_id": uuid | null }
  409    outside_window_not_authorised { message, template_cost_brl, whatsapp_link }   # WhatsApp patient outside 24 h and not authorised
  422    no_channel  |  502 delivery_failed  |  422 (pydantic) blank / too long text
```

R4 also says the clinic's warning e-mail links to the agenda as `DOCTOR_AGENDA_URL?consulta=<appointment_id>` and that "R5 opens the drawer of that appointment when `consulta` is present". The agenda today reads `?consultaId=` (and `?data=`), so **Task 5 adds `consulta` as an accepted alias**. The link carries no date, and the screen only selects an appointment inside the range it loaded, so the deep link lands only when the appointment is in the current week: **recommend R4 append `&data=YYYY-MM-DD`** (the agenda already honours it).

**Backend gap owned elsewhere — clinic configuration fields.** `GET/PUT /tenants/me/config` and `PUT /tenants/me/configuration` (`src/secretaria/api/hub/config.py` → `hubcfg.tenant_read_model`, `schemas/config.py::TenantConfigRead/TenantConfigUpdate`) do **not** expose `reminders_v2_enabled` / `reminder_extra_lead_minutes` in R1 (R1 only adds the columns to `Tenant`; R2 and R4, read 2026-10-04, do not add them either). **Ask R4 (or an R1 addendum) to own this small change**, with this shape, because R5 cannot ship the field without it:

```text
TenantConfigRead   += reminders_v2_enabled: bool            (read-only, derived from Tenant.reminders_v2_enabled)
                      reminder_extra_lead_minutes: int | None
TenantConfigUpdate += reminder_extra_lead_minutes: int | None = Field(default=None, ge=1500, le=20160)
                      # >= 1500 min (more than the 1-day reminder), <= 14 days; None/omitted = leave untouched, explicit null = turn off
                      # reminders_v2_enabled is NOT writable here (spec 4.5: the owner turns it on per clinic)
```

Until that lands the front hides the "Lembretes" section (the keys are absent from the GET) and never sends the field — this is the graceful path pinned in Tasks 7–8. Explicit `null` must be distinguishable from "omitted" on the PUT (`exclude_unset` already does it).

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lib/agenda/types.ts` | Modify | `Appt.confirmation`, `Appt.reminders`, their types |
| `lib/agenda/secretaria-hub-agenda.ts` | Modify | wire types for the new event keys; `releaseAppointment`, `messageAppointment` |
| `lib/agenda/confirmation.ts` | Create | wire→`Appt` mapping, `confirmationLook`, legend entries, reminder timeline (all pure) |
| `lib/agenda/release.ts` | Create | `releaseNeedsAck`, `buildReleaseBody`, copy constants, message suggestion/validation |
| `lib/agenda/hub-mapping.ts` | Modify | thread status + confirmation fields through `mapHubEventToAppt` |
| `lib/agenda/screen.ts` | Modify | modals `release`/`message`, optimistic confirmation, controller methods, `AgendaHub` members, `?consulta=` alias |
| `app/styles/tokens.css` | Modify | `--st-neutral-*`, `--st-confirm2-*`, `--st-attn-*` in both themes |
| `components/agenda/ConfirmationBadge.tsx` | Create | `ConfirmationMark`, `ConfirmationBadge`, `ConfirmationLegend` |
| `components/agenda/CalendarViews.tsx` | Modify | tone class, marks, accessible names, month summary |
| `components/agenda/confirmation.css` | Create | badge, grid mark, legend (no motion) |
| `components/agenda/calendar-views.css`, `agenda-modals.css` | Modify | grid tone classes; timeline, red-state actions, release/message card |
| `components/agenda/Drawer.tsx` | Modify | badge, timeline, red-state actions |
| `components/agenda/ReleaseCard.tsx`, `MessageModal.tsx` | Create | the two new sheets |
| `components/agenda/AgendaView.tsx`, `AgendaScreen.tsx` | Modify | legend, new sheets, HUB wiring |
| `lib/real/secretaria-hub.ts` | Modify | `TenantConfigWire`/`Payload` gain the two config keys |
| `lib/contexto/reminders.ts` | Create | lead presets: `formatLead`, `leadOptions`, `parseLead` |
| `lib/contexto/types.ts`, `hub-mapping.ts`, `snapshot.ts`, `screen.ts` | Modify | `Reminders` slice, hydration, payload, dirty detection, reducer action |
| `components/contexto/ReminderSection.tsx` | Create | the "Lembretes" section |
| `components/contexto/ContextoView.tsx` | Modify | `rem` section in the order and the record |
| `docs/CHECKPOINT_brain_message_lembretes_agenda.md` | Create | state, what went where, pending items |
| Tests (all new/modified listed per task) | | `lib/agenda/__tests__/*`, `components/agenda/__tests__/*`, `lib/contexto/__tests__/*`, `components/contexto/__tests__/*` |

---

## Task 1: Wire types, mapping and the pure confirmation logic

**Files:**
- Modify: `lib/agenda/types.ts` (after `ApptDeposit`; end of `Appt`)
- Modify: `lib/agenda/secretaria-hub-agenda.ts` (`CalendarEventWire`, new `CalendarReminderWire`)
- Create: `lib/agenda/confirmation.ts`
- Modify: `lib/agenda/hub-mapping.ts` (`mapHubEventToAppt`)
- Test: `lib/agenda/__tests__/confirmation.test.ts`

**Interfaces:**
- Consumes: R1 event keys (see "Consumes").
- Produces (later tasks rely on these exact names):
  - `type ConfirmationState = "unconfirmed" | "confirmed" | "confirmed_twice" | "attention"`; `type ApptConfirmation = { state: ConfirmationState; count: 0 | 1 | 2 }`; `type ApptReminder = { kind: string; status: string; dueAt: string; sentAt?: string; answeredAt?: string; answer?: string; warnedAt?: string; warnKind?: string }`; `Appt.confirmation?: ApptConfirmation`, `Appt.reminders?: ApptReminder[]` (in `types.ts`).
  - In `confirmation.ts`: `MAX_CONFIRMATIONS = 2`; `mapEventStatus(e: CalendarEventWire): ApptStatus`; `mapConfirmationFields(e): Pick<Appt, "confirmation" | "reminders">`; `type ConfirmationTone = "neutral" | "confirm" | "confirm2" | "attn"`; `type ConfirmationLook = { state; tone; ticks: 0|1|2; icon: "check"|"check-double"|"alert"|null; label: string; ariaText: string; hint: string }`; `confirmationLook(appt: Pick<Appt,"status"|"confirmation">): ConfirmationLook | null`; `LEGEND_ENTRIES: readonly ConfirmationLook[]`; `type TimelineEntry = { at: string; when: string; text: string; tone: "info"|"ok"|"warn" }`; `reminderTimeline(reminders: readonly ApptReminder[] | undefined): TimelineEntry[]`; `fmtStamp(iso: string): string | null`.

- [ ] **Step 1: Write the failing test**

Create `lib/agenda/__tests__/confirmation.test.ts`:

```ts
// Confirmation mapping + look + timeline (TASK-032 R5). Pure: no DOM.
import { describe, expect, it } from "vitest";

import {
  LEGEND_ENTRIES,
  confirmationLook,
  fmtStamp,
  mapConfirmationFields,
  mapEventStatus,
  reminderTimeline,
} from "../confirmation";
import { mapHubEventToAppt } from "../hub-mapping";
import type { CalendarEventWire } from "../secretaria-hub-agenda";
import type { Appt, ApptReminder } from "../types";

const START = new Date(2026, 8, 29, 9, 0).toISOString();
const END = new Date(2026, 8, 29, 9, 30).toISOString();
const ev = (over: Partial<CalendarEventWire> = {}): CalendarEventWire => ({
  id: "g1",
  summary: "Consulta — Maria Souza",
  start: START,
  end: END,
  appointment_id: "ap-1",
  ...over,
});
const at = (h: number, m = 0) => new Date(2026, 8, 28, h, m).toISOString();

describe("mapEventStatus", () => {
  it.each([
    ["scheduled", "agendado"],
    ["rescheduled", "agendado"],
    ["confirmed", "confirmou"],
    ["attended", "compareceu"],
    ["no_show", "faltou"],
    ["cancelled", "cancelado"],
  ] as const)("%s -> %s", (wire, local) => {
    expect(mapEventStatus(ev({ status: wire }))).toBe(local);
  });

  it("falls back to agendado for an absent, null or unknown status (old/newer backend)", () => {
    expect(mapEventStatus(ev())).toBe("agendado");
    expect(mapEventStatus(ev({ status: null }))).toBe("agendado");
    expect(mapEventStatus(ev({ status: "paused" as never }))).toBe("agendado");
    expect(mapEventStatus(ev({ status: "toString" as never }))).toBe("agendado");
  });

  it("ignores a status on an event with no local appointment (Google-only)", () => {
    expect(mapEventStatus(ev({ appointment_id: null, status: "confirmed" }))).toBe("agendado");
  });
});

describe("mapConfirmationFields", () => {
  it("adds NO key when the backend sent none of the new fields", () => {
    expect(mapConfirmationFields(ev())).toEqual({});
  });

  it("zero + attention is the red state", () => {
    expect(mapConfirmationFields(ev({ confirmation_count: 0, attention: true })).confirmation).toEqual({
      state: "attention",
      count: 0,
    });
  });

  it("one confirmation is green, two is double green", () => {
    expect(mapConfirmationFields(ev({ confirmation_count: 1 })).confirmation).toEqual({ state: "confirmed", count: 1 });
    expect(mapConfirmationFields(ev({ confirmation_count: 2 })).confirmation).toEqual({
      state: "confirmed_twice",
      count: 2,
    });
  });

  it("caps at two and floors at zero (a count of 5 never draws three ticks)", () => {
    expect(mapConfirmationFields(ev({ confirmation_count: 5 })).confirmation).toEqual({
      state: "confirmed_twice",
      count: 2,
    });
    expect(mapConfirmationFields(ev({ confirmation_count: -3 })).confirmation?.count).toBe(0);
  });

  it("a confirmation count wins over a contradicting attention flag", () => {
    expect(mapConfirmationFields(ev({ confirmation_count: 1, attention: true })).confirmation?.state).toBe("confirmed");
  });

  it("reads display_state when the count is missing, and ignores an unknown display_state", () => {
    expect(mapConfirmationFields(ev({ display_state: "confirmed_twice" })).confirmation).toEqual({
      state: "confirmed_twice",
      count: 2,
    });
    expect(mapConfirmationFields(ev({ display_state: "attention" })).confirmation?.state).toBe("attention");
    expect(mapConfirmationFields(ev({ display_state: "weird" }))).toEqual({});
    expect(mapConfirmationFields(ev({ confirmation_count: Number.NaN as never }))).toEqual({});
  });

  it("maps reminders, keeps only well-formed rows and never copies unknown keys", () => {
    const out = mapConfirmationFields(
      ev({
        confirmation_count: 0,
        reminders: [
          { kind: "day", status: "sent", due_at: at(9), sent_at: at(9, 1), answered_at: null, answer: null, secret: "x" } as never,
          { kind: 5, status: "sent", due_at: at(9) } as never,
          null as never,
        ],
      }),
    );
    expect(out.reminders).toEqual([{ kind: "day", status: "sent", dueAt: at(9), sentAt: at(9, 1) }]);
  });

  it("is empty for a Google-only event even when stray keys are present", () => {
    expect(mapConfirmationFields(ev({ appointment_id: null, confirmation_count: 2, attention: true, reminders: [] }))).toEqual({});
  });
});

describe("confirmationLook", () => {
  const live = (status: Appt["status"], count: 0 | 1 | 2, state: NonNullable<Appt["confirmation"]>["state"]) => ({
    status,
    confirmation: { state, count },
  });

  it("neutral / green / double green / red, each with a text label and an icon where it is not neutral", () => {
    expect(confirmationLook(live("agendado", 0, "unconfirmed"))).toMatchObject({ tone: "neutral", ticks: 0, icon: null, label: "Marcada" });
    expect(confirmationLook(live("agendado", 1, "confirmed"))).toMatchObject({ tone: "confirm", ticks: 1, icon: "check", label: "Confirmada" });
    expect(confirmationLook(live("confirmou", 2, "confirmed_twice"))).toMatchObject({
      tone: "confirm2",
      ticks: 2,
      icon: "check-double",
      label: "Confirmada duas vezes",
    });
    expect(confirmationLook(live("agendado", 0, "attention"))).toMatchObject({ tone: "attn", icon: "alert", label: "Sem confirmação" });
  });

  it("terminal statuses and blocks keep their own look (no look at all), even flagged for attention", () => {
    for (const status of ["cancelado", "compareceu", "faltou", "bloqueio"] as const) {
      expect(confirmationLook(live(status, 0, "attention"))).toBeNull();
      expect(confirmationLook(live(status, 2, "confirmed_twice"))).toBeNull();
    }
  });

  it("is null without confirmation data (older backend keeps the old look)", () => {
    expect(confirmationLook({ status: "agendado" })).toBeNull();
  });

  it("a staff-confirmed status never renders as unconfirmed or red", () => {
    expect(confirmationLook(live("confirmou", 0, "attention"))?.state).toBe("confirmed");
    expect(confirmationLook(live("confirmou", 0, "unconfirmed"))?.state).toBe("confirmed");
  });

  it("the legend lists the four states once, in order, each with a label and a hint", () => {
    expect(LEGEND_ENTRIES.map((l) => l.state)).toEqual(["unconfirmed", "confirmed", "confirmed_twice", "attention"]);
    expect(LEGEND_ENTRIES.every((l) => l.label && l.hint && l.ariaText)).toBe(true);
  });
});

describe("reminderTimeline", () => {
  const r = (over: Partial<ApptReminder>): ApptReminder => ({ kind: "day", status: "sent", dueAt: at(9), ...over });

  it("is empty for nothing", () => {
    expect(reminderTimeline(undefined)).toEqual([]);
    expect(reminderTimeline([])).toEqual([]);
  });

  it("lists sent, answered and clinic-warned in time order", () => {
    const out = reminderTimeline([
      r({ kind: "day", sentAt: at(9, 1), warnedAt: at(11, 5), warnKind: "unconfirmed" }),
      r({ kind: "custom", sentAt: at(8), answeredAt: at(8, 30), answer: "confirm" }),
    ]);
    expect(out.map((e) => e.text)).toEqual([
      "Lembrete extra enviado",
      "Paciente confirmou presença",
      "Lembrete de 1 dia enviado",
      "Clínica avisada: sem confirmação",
    ]);
    expect(out[1].tone).toBe("ok");
    expect(out[3].tone).toBe("warn");
    expect(out[0].when).toBe(fmtStamp(at(8)));
  });

  it("explains a failed delivery and the matching clinic warning", () => {
    const out = reminderTimeline([r({ status: "failed", dueAt: at(9), warnedAt: at(9, 10), warnKind: "delivery_failed" })]);
    expect(out.map((e) => e.text)).toEqual([
      "Lembrete de 1 dia: não foi possível entregar",
      "Clínica avisada: o lembrete não foi entregue",
    ]);
  });

  it("shows only the NEXT scheduled reminder, never the whole future schedule", () => {
    const out = reminderTimeline([
      r({ kind: "hour", status: "pending", dueAt: at(18) }),
      r({ kind: "day", status: "pending", dueAt: at(12) }),
      r({ kind: "custom", status: "cancelled", dueAt: at(7) }),
    ]);
    expect(out.map((e) => e.text)).toEqual(["Lembrete de 1 dia programado"]);
  });

  it("an unknown answer or kind still reads honestly, and invalid dates are dropped", () => {
    const out = reminderTimeline([
      r({ kind: "zzz", sentAt: at(9), answeredAt: at(10), answer: "maybe" }),
      r({ kind: "day", sentAt: "not-a-date" }),
    ]);
    expect(out.map((e) => e.text)).toEqual(["Lembrete enviado", "Paciente respondeu"]);
  });
});

describe("mapHubEventToAppt with the new keys", () => {
  it("carries status, confirmation and reminders on an appointment", () => {
    const a = mapHubEventToAppt(ev({ status: "confirmed", confirmation_count: 1, display_state: "confirmed", attention: false }));
    expect(a?.status).toBe("confirmou");
    expect(a?.confirmation).toEqual({ state: "confirmed", count: 1 });
    expect(a?.appointmentId).toBe("ap-1");
  });

  it("an event without the new keys maps exactly as before (agendado, no confirmation key)", () => {
    const a = mapHubEventToAppt(ev());
    expect(a?.status).toBe("agendado");
    expect("confirmation" in (a as object)).toBe(false);
    expect("reminders" in (a as object)).toBe(false);
  });

  it("a cancelled appointment keeps its own status whatever the confirmation says", () => {
    const a = mapHubEventToAppt(ev({ status: "cancelled", confirmation_count: 0, attention: true }));
    expect(a?.status).toBe("cancelado");
    expect(confirmationLook(a as Appt)).toBeNull();
  });

  it("a Google-only block is untouched by stray confirmation keys", () => {
    const base = ev({ appointment_id: null, summary: "Bloqueado: Almoço" });
    const plain = mapHubEventToAppt(base);
    const noisy = mapHubEventToAppt({ ...base, status: "confirmed", confirmation_count: 2, attention: true, reminders: [] });
    expect(noisy).toEqual(plain);
    expect(noisy?.status).toBe("bloqueio");
  });

  it("a Google-only appointment (no appointment_id) is untouched too", () => {
    const base = ev({ appointment_id: null });
    expect(mapHubEventToAppt({ ...base, status: "attended", confirmation_count: 1 })).toEqual(mapHubEventToAppt(base));
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run (PowerShell): `npx vitest run lib/agenda/__tests__/confirmation.test.ts`
Expected: FAIL — `Cannot find module '../confirmation'` (and type errors on the new wire keys).

- [ ] **Step 3: Add the types**

In `lib/agenda/types.ts`, directly after the `ApptDeposit` type, add:

```ts
/** The four display states of an appointment's confirmation (spec 4.1). */
export type ConfirmationState = "unconfirmed" | "confirmed" | "confirmed_twice" | "attention";

/** What the events read said about confirmation. ABSENT (no key) on an older backend or a Google-only event. */
export type ApptConfirmation = {
  state: ConfirmationState;
  /** Clamped to 0..2 on read: the display never draws more than two ticks. */
  count: 0 | 1 | 2;
};

/** One reminder row of the appointment's current start (schemas/calendar.py::CalendarReminderRead), camel-cased. */
export type ApptReminder = {
  /** "custom" | "day" | "hour" | "chat", kept as a string: an unknown kind still renders. */
  kind: string;
  status: string;
  dueAt: string;
  sentAt?: string;
  answeredAt?: string;
  answer?: string;
  warnedAt?: string;
  warnKind?: string;
};
```

and at the end of the `Appt` type, after the `deposit?: ApptDeposit;` line (before the closing `};`), add:

```ts
  /**
   * The confirmation state from the events read (TASK-032). ABSENT (no key) when
   * the backend sent none of it, or for an event with no local appointment; the
   * view then keeps today's look. Never set on a block.
   */
  confirmation?: ApptConfirmation;
  /** Reminder rows for the drawer's timeline. ABSENT when not sent. */
  reminders?: ApptReminder[];
```

In `lib/agenda/secretaria-hub-agenda.ts`, add above `CalendarEventWire`:

```ts
/** GET /calendar/events item's reminder row (schemas/calendar.py::CalendarReminderRead). */
export type CalendarReminderWire = {
  kind: string;
  status: string;
  due_at: string;
  sent_at?: string | null;
  answered_at?: string | null;
  answer?: string | null;
  warned_at?: string | null;
  warn_kind?: string | null;
};
```

and inside `CalendarEventWire`, after the `deposit?` field:

```ts
  /** TASK-032 R1. Absent on an older backend; null for a block or a Google-only event. */
  status?: AppointmentStatusWire | null;
  confirmation_count?: number | null;
  /** "unconfirmed" | "confirmed" | "confirmed_twice" | "attention" — a plain string on purpose. */
  display_state?: string | null;
  attention?: boolean | null;
  reminders?: CalendarReminderWire[] | null;
```

- [ ] **Step 4: Implement `lib/agenda/confirmation.ts`**

```ts
// lib/agenda/confirmation.ts — everything the agenda knows about an appointment's
// CONFIRMATION (TASK-032 R5), as pure functions: the wire->Appt mapping, the four
// display looks, the legend and the drawer's reminder timeline. No React, no fetch.
//
// Two rules run through all of it:
//  - fail open to today's look: with none of the new keys (older backend, a
//    Google-only event, a block) nothing here adds a key or returns a look;
//  - terminal statuses keep their own look: a cancelled/attended/no-show slot is
//    never recoloured by its confirmation state.

import type { Appt, ApptConfirmation, ApptReminder, ApptStatus, ConfirmationState } from "./types";
import type { AppointmentStatusWire, CalendarEventWire, CalendarReminderWire } from "./secretaria-hub-agenda";

/** The display never shows more than two confirmations (spec 4.1). */
export const MAX_CONFIRMATIONS = 2;

const STATUS_FROM_WIRE: Readonly<Record<AppointmentStatusWire, ApptStatus>> = {
  scheduled: "agendado",
  rescheduled: "agendado",
  confirmed: "confirmou",
  attended: "compareceu",
  no_show: "faltou",
  cancelled: "cancelado",
};

/** True only for an event backed by a local Appointment row (a block or a Google-only event has none). */
function hasAppointment(e: CalendarEventWire): boolean {
  return typeof e.appointment_id === "string" && e.appointment_id.length > 0;
}

/** The slot's own status. Absent/unknown/Google-only -> "agendado", exactly what the read used to imply. */
export function mapEventStatus(e: CalendarEventWire): ApptStatus {
  const s = e.status;
  if (!hasAppointment(e) || typeof s !== "string") return "agendado";
  const mapped = Object.prototype.hasOwnProperty.call(STATUS_FROM_WIRE, s) ? STATUS_FROM_WIRE[s] : undefined;
  return mapped ?? "agendado";
}

const STATE_COUNT: Readonly<Record<ConfirmationState, 0 | 1 | 2>> = {
  unconfirmed: 0,
  attention: 0,
  confirmed: 1,
  confirmed_twice: 2,
};

function isConfirmationState(v: string): v is ConfirmationState {
  return Object.prototype.hasOwnProperty.call(STATE_COUNT, v);
}

function clampCount(n: unknown): 0 | 1 | 2 | null {
  if (typeof n !== "number" || !Number.isFinite(n)) return null;
  const t = Math.trunc(n);
  if (t <= 0) return 0;
  return t === 1 ? 1 : 2;
}

function mapConfirmation(e: CalendarEventWire): Pick<Appt, "confirmation"> {
  if (!hasAppointment(e)) return {};
  const count = clampCount(e.confirmation_count);
  const display = typeof e.display_state === "string" && isConfirmationState(e.display_state) ? e.display_state : null;
  if (count === null && display === null && typeof e.attention !== "boolean") return {};
  const n: 0 | 1 | 2 = count ?? (display ? STATE_COUNT[display] : 0);
  const state: ConfirmationState =
    n >= 2 ? "confirmed_twice" : n === 1 ? "confirmed" : e.attention === true || display === "attention" ? "attention" : "unconfirmed";
  const confirmation: ApptConfirmation = { state, count: n };
  return { confirmation };
}

const str = (v: unknown): string | undefined => (typeof v === "string" && v.length > 0 ? v : undefined);

function reminderFrom(r: CalendarReminderWire): ApptReminder {
  const out: ApptReminder = { kind: r.kind, status: r.status, dueAt: r.due_at };
  const sentAt = str(r.sent_at);
  if (sentAt) out.sentAt = sentAt;
  const answeredAt = str(r.answered_at);
  if (answeredAt) out.answeredAt = answeredAt;
  const answer = str(r.answer);
  if (answer) out.answer = answer;
  const warnedAt = str(r.warned_at);
  if (warnedAt) out.warnedAt = warnedAt;
  const warnKind = str(r.warn_kind);
  if (warnKind) out.warnKind = warnKind;
  return out;
}

function mapReminders(e: CalendarEventWire): Pick<Appt, "reminders"> {
  if (!hasAppointment(e) || !Array.isArray(e.reminders)) return {};
  const out: ApptReminder[] = [];
  for (const r of e.reminders) {
    if (!r || typeof r.kind !== "string" || typeof r.status !== "string" || typeof r.due_at !== "string") continue;
    out.push(reminderFrom(r));
  }
  return { reminders: out };
}

/** The confirmation + reminders keys for an Appt; `{}` (no keys at all) when the backend sent none. */
export function mapConfirmationFields(e: CalendarEventWire): Pick<Appt, "confirmation" | "reminders"> {
  return { ...mapConfirmation(e), ...mapReminders(e) };
}

// ---------------------------------------------------------------------------
// The four looks
// ---------------------------------------------------------------------------

export type ConfirmationTone = "neutral" | "confirm" | "confirm2" | "attn";

export type ConfirmationLook = {
  state: ConfirmationState;
  tone: ConfirmationTone;
  /** How many ticks to draw: 0, 1 or 2 (never more). */
  ticks: 0 | 1 | 2;
  /** The non-colour cue. null = the neutral baseline, which has none on purpose. */
  icon: "check" | "check-double" | "alert" | null;
  /** Visible label (badge, legend). */
  label: string;
  /** Appended to a slot's accessible name. */
  ariaText: string;
  /** One sentence for the legend. */
  hint: string;
};

const LOOKS: Readonly<Record<ConfirmationState, ConfirmationLook>> = {
  unconfirmed: {
    state: "unconfirmed",
    tone: "neutral",
    ticks: 0,
    icon: null,
    label: "Marcada",
    ariaText: "marcada, sem confirmação ainda",
    hint: "Consulta marcada, sem confirmação ainda.",
  },
  confirmed: {
    state: "confirmed",
    tone: "confirm",
    ticks: 1,
    icon: "check",
    label: "Confirmada",
    ariaText: "confirmada",
    hint: "O paciente confirmou a presença.",
  },
  confirmed_twice: {
    state: "confirmed_twice",
    tone: "confirm2",
    ticks: 2,
    icon: "check-double",
    label: "Confirmada duas vezes",
    ariaText: "confirmada duas vezes",
    hint: "O paciente confirmou a presença duas vezes.",
  },
  attention: {
    state: "attention",
    tone: "attn",
    ticks: 0,
    icon: "alert",
    label: "Sem confirmação",
    ariaText: "sem confirmação, precisa de atenção",
    hint: "Um lembrete saiu, o prazo passou e o paciente não confirmou.",
  },
};

const LIVE_STATUSES: readonly ApptStatus[] = ["agendado", "confirmou"];

/**
 * The look of a LIVE appointment, or null when the slot keeps its own look:
 * terminal statuses (cancelado / compareceu / faltou), blocks, and any slot the
 * backend sent no confirmation data for.
 */
export function confirmationLook(appt: Pick<Appt, "status" | "confirmation">): ConfirmationLook | null {
  if (!LIVE_STATUSES.includes(appt.status)) return null;
  const c = appt.confirmation;
  if (!c) return null;
  // A staff-confirmed status is a confirmation even if the counter has not caught up.
  const state: ConfirmationState =
    appt.status === "confirmou" && (c.state === "unconfirmed" || c.state === "attention") ? "confirmed" : c.state;
  return LOOKS[state];
}

/** The four states in legend order. */
export const LEGEND_ENTRIES: readonly ConfirmationLook[] = [
  LOOKS.unconfirmed,
  LOOKS.confirmed,
  LOOKS.confirmed_twice,
  LOOKS.attention,
];

// ---------------------------------------------------------------------------
// Reminder timeline (drawer)
// ---------------------------------------------------------------------------

export type TimelineEntry = {
  /** The ISO instant it is sorted by. */
  at: string;
  /** "DD/MM HH:MM" in the browser's local time, from `at`. */
  when: string;
  text: string;
  tone: "info" | "ok" | "warn";
};

const KIND_LABEL: Readonly<Record<string, string>> = {
  custom: "Lembrete extra",
  day: "Lembrete de 1 dia",
  hour: "Lembrete de 1 hora",
  chat: "Mensagem de abertura da conversa",
};

const ANSWER_TEXT: Readonly<Record<string, string>> = {
  confirm: "confirmou presença",
  cancel: "pediu para cancelar",
  other: "pediu outra opção",
};

const own = (table: Readonly<Record<string, string>>, key: string | undefined): string | undefined =>
  key !== undefined && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;

/** "DD/MM HH:MM" local, derived from the timestamp itself. null for an unparseable value. */
export function fmtStamp(iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

type RawEntry = { at: string; text: string; tone: TimelineEntry["tone"] };

/**
 * A short, honest history: reminder sent, patient answered, clinic warned, plus
 * the single NEXT scheduled reminder. Cancelled/skipped rows and unparseable
 * dates are dropped; nothing is invented from a missing field.
 */
export function reminderTimeline(reminders: readonly ApptReminder[] | undefined): TimelineEntry[] {
  if (!reminders || reminders.length === 0) return [];
  const raw: RawEntry[] = [];
  let next: ApptReminder | null = null;
  for (const r of reminders) {
    const label = own(KIND_LABEL, r.kind) ?? "Lembrete";
    if (r.sentAt) raw.push({ at: r.sentAt, text: `${label} enviado`, tone: "info" });
    else if (r.status === "failed") raw.push({ at: r.dueAt, text: `${label}: não foi possível entregar`, tone: "warn" });
    else if (r.status === "pending" && (next === null || Date.parse(r.dueAt) < Date.parse(next.dueAt))) next = r;
    if (r.answeredAt) {
      const what = own(ANSWER_TEXT, r.answer);
      raw.push({ at: r.answeredAt, text: what ? `Paciente ${what}` : "Paciente respondeu", tone: r.answer === "confirm" ? "ok" : "info" });
    }
    if (r.warnedAt) {
      raw.push({
        at: r.warnedAt,
        text: r.warnKind === "delivery_failed" ? "Clínica avisada: o lembrete não foi entregue" : "Clínica avisada: sem confirmação",
        tone: "warn",
      });
    }
  }
  if (next) raw.push({ at: next.dueAt, text: `${own(KIND_LABEL, next.kind) ?? "Lembrete"} programado`, tone: "info" });

  const entries: (TimelineEntry & { order: number })[] = [];
  raw.forEach((e, order) => {
    const when = fmtStamp(e.at);
    if (when) entries.push({ ...e, when, order });
  });
  entries.sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.order - b.order);
  return entries.map(({ order: _order, ...e }) => e);
}
```

- [ ] **Step 5: Thread the fields through `mapHubEventToAppt`**

In `lib/agenda/hub-mapping.ts`, add to the imports: `import { mapConfirmationFields, mapEventStatus } from "./confirmation";`. In the appointment return of `mapHubEventToAppt`, replace

```ts
    status: "agendado",
    anamnese: "—",
```

with

```ts
    status: mapEventStatus(e),
    anamnese: "—",
```

and after `...mapDepositField(e),` add `...mapConfirmationFields(e),`. Do **not** touch the block branch.

- [ ] **Step 6: Run the tests, then the whole suite and the type check**

Run: `npx vitest run lib/agenda/__tests__/confirmation.test.ts` — Expected: PASS.
Run: `npm test` — Expected: all green (existing agenda tests still see `status: "agendado"` for events without status).
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add lib/agenda/types.ts lib/agenda/secretaria-hub-agenda.ts lib/agenda/confirmation.ts lib/agenda/hub-mapping.ts lib/agenda/__tests__/confirmation.test.ts
git commit -m "feat(agenda): map confirmation state, status and reminders from the events read (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 2: Colour tokens, badge/mark/legend components, grid tones and legend

**Files:**
- Modify: `app/styles/tokens.css` (the two theme blocks, next to the existing `--st-block` line of each)
- Create: `components/agenda/confirmation.css`, `components/agenda/ConfirmationBadge.tsx`
- Modify: `components/agenda/calendar-views.css` (append), `components/agenda/CalendarViews.tsx`, `components/agenda/AgendaView.tsx`
- Test: `components/agenda/__tests__/confirmation-views.test.tsx`, `components/agenda/__tests__/confirmation-css.test.ts`

**Interfaces:**
- Consumes: `confirmationLook`, `ConfirmationLook`, `LEGEND_ENTRIES` (Task 1).
- Produces: `ConfirmationMark({ look: ConfirmationLook | null; inline?: boolean })` (decorative, `aria-hidden`, renders nothing for `null`/neutral); `ConfirmationBadge({ look: ConfirmationLook })` (icon `aria-hidden` + visible label); `ConfirmationLegend()` (a named `<ul>`); CSS tone classes `cal-appt--neutral|confirm2|attn`, `cal-cell-item--neutral|confirm2|attn`, `conf-badge--neutral|confirm|confirm2|attn`.

- [ ] **Step 1: Write the failing tests**

Create `components/agenda/__tests__/confirmation-views.test.tsx`:

```tsx
// Grid tones, marks and legend for the confirmation states (TASK-032 R5).
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { DayView, MonthView, WeekView } from "../CalendarViews";
import { ConfirmationBadge, ConfirmationLegend, ConfirmationMark } from "../ConfirmationBadge";
import { confirmationLook } from "../../../lib/agenda/confirmation";
import { monthGrid, weekDays } from "../../../lib/agenda/calendar-dates";
import type { Appt, ConfirmationState } from "../../../lib/agenda/types";

const weekStart = new Date(2026, 8, 27);
const today = new Date(2026, 8, 29, 10, 30);
const days = weekDays(weekStart, today);
const noop = () => {};

const COUNT = { unconfirmed: 0, attention: 0, confirmed: 1, confirmed_twice: 2 } as const;
const appt = (state: ConfirmationState | null, over: Partial<Appt> = {}): Appt => ({
  id: "a1", date: "2026-09-29", day: 2, start: 9 * 60, dur: 30,
  patient: "Maria Souza", type: "Consulta", status: "agendado",
  ...(state ? { confirmation: { state, count: COUNT[state] } } : {}),
  ...over,
});
const week = (items: Appt[]) =>
  renderToStaticMarkup(<WeekView days={days} items={items} onSelect={noop} onDayClick={noop} now={today} />);

describe("WeekView — confirmation looks", () => {
  it("draws each state with its tone class, a non-colour mark and a spoken state", () => {
    const neutral = week([appt("unconfirmed")]);
    expect(neutral).toContain("cal-appt--neutral");
    expect(neutral).not.toContain("cal-appt-conf");
    expect(neutral).toContain("marcada, sem confirmação ainda");

    const one = week([appt("confirmed")]);
    expect(one).toContain("cal-appt--confirm");
    expect(one).toContain('data-ticks="1"');
    expect(one).toContain("confirmada");

    const two = week([appt("confirmed_twice")]);
    expect(two).toContain("cal-appt--confirm2");
    expect(two).toContain('data-ticks="2"');
    expect(two).not.toContain('data-ticks="3"');
    expect(two).toContain("confirmada duas vezes");

    const red = week([appt("attention")]);
    expect(red).toContain("cal-appt--attn");
    expect(red).toContain('data-state="attention"');
    expect(red).toContain("sem confirmação, precisa de atenção");
  });

  it("the accessible name still starts with the visible time and patient (label in name)", () => {
    expect(week([appt("attention")])).toContain('aria-label="09:00 Maria Souza · Consulta, sem confirmação, precisa de atenção"');
  });

  it("an older backend (no confirmation) keeps the previous tone and name exactly", () => {
    const html = week([appt(null)]);
    expect(html).toContain("cal-appt--pending");
    expect(html).not.toContain("cal-appt-conf");
    expect(html).toContain('aria-label="09:00 Maria Souza · Consulta"');
  });

  it("cancelled / attended / no-show keep their own look even flagged for attention", () => {
    for (const [status, tone] of [["cancelado", "block"], ["compareceu", "attend"], ["faltou", "miss"]] as const) {
      const html = week([appt("attention", { status })]);
      expect(html).toContain(`cal-appt--${tone}`);
      expect(html).not.toContain("cal-appt--attn");
      expect(html).not.toContain("cal-appt-conf");
    }
    expect(week([appt("attention", { status: "cancelado" })])).toContain("cal-appt--cancelled");
  });

  it("a block is untouched", () => {
    const html = week([appt(null, { status: "bloqueio", reason: "Almoço", patient: undefined })]);
    expect(html).toContain("cal-block");
    expect(html).not.toContain("cal-appt-conf");
  });
});

describe("DayView / MonthView", () => {
  it("DayView shows the confirmation label instead of the bare status on a tall slot", () => {
    const html = renderToStaticMarkup(
      <DayView day={days[2]} items={[appt("confirmed", { dur: 60 })]} onSelect={noop} now={today} />,
    );
    expect(html).toContain("Confirmada");
    expect(html).toContain("cal-appt--confirm");
  });

  it("MonthView counts unconfirmed appointments in the day's spoken summary", () => {
    const cells = monthGrid(new Date(2026, 8, 29), today);
    const html = renderToStaticMarkup(
      <MonthView cells={cells} items={[appt("attention"), appt("confirmed", { id: "a2" })]} onDayClick={noop} />,
    );
    expect(html).toContain("2 consultas, 1 sem confirmação");
    expect(html).toContain("cal-cell-item--attn");
  });

  it("MonthView summary is unchanged without confirmation data", () => {
    const cells = monthGrid(new Date(2026, 8, 29), today);
    const html = renderToStaticMarkup(<MonthView cells={cells} items={[appt(null)]} onDayClick={noop} />);
    expect(html).toContain("1 consulta\"");
    expect(html).not.toContain("sem confirmação");
  });
});

describe("badge, mark and legend", () => {
  it("the badge always carries the state as text; the icon is decorative", () => {
    const html = renderToStaticMarkup(<ConfirmationBadge look={confirmationLook(appt("confirmed_twice"))!} />);
    expect(html).toContain("Confirmada duas vezes");
    expect(html).toContain('data-ticks="2"');
    expect(html).toContain('aria-hidden="true"');
  });

  it("the mark renders nothing for the neutral baseline and for null", () => {
    expect(renderToStaticMarkup(<ConfirmationMark look={null} />)).toBe("");
    expect(renderToStaticMarkup(<ConfirmationMark look={confirmationLook(appt("unconfirmed"))} />)).toBe("");
  });

  it("the legend is a named list with the four states, each with its text", () => {
    const html = renderToStaticMarkup(<ConfirmationLegend />);
    expect(html).toContain('aria-label="Legenda da confirmação das consultas"');
    for (const label of ["Marcada", "Confirmada", "Confirmada duas vezes", "Sem confirmação"]) expect(html).toContain(label);
    expect(html.match(/<li /g)).toHaveLength(4);
  });
});
```

Add to `components/agenda/__tests__/agenda-view.test.tsx` (new `describe` at the end, reusing the file's `render`, `loaded`, `ITEM`):

```tsx
describe("AgendaView — confirmation legend", () => {
  it("shows the legend only when the read carried confirmation data", () => {
    const withData = render(loaded([{ ...ITEM, confirmation: { state: "attention", count: 0 } }]));
    expect(withData).toContain("Legenda da confirmação das consultas");
    expect(render(loaded([ITEM]))).not.toContain("Legenda da confirmação das consultas");
  });

  it("never shows it without a grid (failed or signed-out screens)", () => {
    expect(render(failed("unavailable"))).not.toContain("Legenda da confirmação");
  });
});
```

Create `components/agenda/__tests__/confirmation-css.test.ts` (reduced motion + both themes):

```ts
// The confirmation looks add NO motion (reduced-motion contract) and exist in both themes.
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const root = path.resolve(__dirname, "../../..");
const read = (p: string) => readFileSync(path.join(root, p), "utf8");

/** Every rule block whose selector list mentions one of `needles`. */
function rules(css: string, needles: string[]): string[] {
  return (css.match(/[^{}]+\{[^{}]*\}/g) ?? []).filter((r) => needles.some((n) => r.split("{")[0].includes(n)));
}

describe("confirmation CSS", () => {
  it("adds no animation or transition to the new classes", () => {
    const files = ["components/agenda/confirmation.css", "components/agenda/calendar-views.css", "components/agenda/agenda-modals.css"];
    const needles = [".conf-badge", ".cal-appt-conf", ".agenda-legend", ".cal-appt--neutral", ".cal-appt--confirm2", ".cal-appt--attn", ".agm-timeline", ".agm-confirm", ".agm-release"];
    for (const f of files) {
      for (const rule of rules(read(f), needles)) {
        expect(rule, `${f}: ${rule.slice(0, 60)}`).not.toMatch(/animation|transition|@keyframes/);
      }
    }
  });

  it("the global reduced-motion rule is still the only motion rule", () => {
    expect(read("app/styles/base.css")).toMatch(/prefers-reduced-motion: reduce/);
    expect(read("components/agenda/confirmation.css")).not.toMatch(/@keyframes|animation:/);
  });

  it("defines the three new status token families in the light AND the dark theme block", () => {
    const tokens = read("app/styles/tokens.css");
    const dark = tokens.indexOf('[data-theme="dark"]');
    expect(dark).toBeGreaterThan(0);
    const light = tokens.slice(0, dark);
    const darkBlock = tokens.slice(dark);
    for (const family of ["--st-neutral-bg", "--st-neutral-ink", "--st-neutral-bd", "--st-confirm2-bg", "--st-confirm2-ink", "--st-confirm2-bd", "--st-attn-bg", "--st-attn-ink", "--st-attn-bd"]) {
      expect(light, `light ${family}`).toContain(family + ":");
      expect(darkBlock, `dark ${family}`).toContain(family + ":");
    }
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run components/agenda/__tests__/confirmation-views.test.tsx components/agenda/__tests__/confirmation-css.test.ts components/agenda/__tests__/agenda-view.test.tsx`
Expected: FAIL (`Cannot find module '../ConfirmationBadge'`, missing tokens).

- [ ] **Step 3: Tokens**

In `app/styles/tokens.css`, immediately after **each** of the two lines that start with `  --st-block:` (one in the `[data-theme="light"]` block near line 176, one in the `[data-theme="dark"]` block near line 263) add exactly:

```css
  /* Confirmation looks (TASK-032 R5). Neutral = just booked; confirm2 = confirmed twice (same hue, stronger edge + a double tick); attn = reminder sent, deadline passed, no answer. Each also has an icon and a text label: colour is never the only cue. */
  --st-neutral-bg: var(--surface-2); --st-neutral-ink: var(--ink-soft); --st-neutral-bd: var(--line-strong);
  --st-confirm2-bg: var(--brand-tint); --st-confirm2-ink: var(--brand-ink); --st-confirm2-bd: var(--brand);
  --st-attn-bg: var(--danger-bg); --st-attn-ink: var(--danger); --st-attn-bd: var(--danger);
```

(They reuse pairs that already ship as `--st-block`, `--st-confirm` and `--st-miss`, so contrast follows those; the post-deploy live proof re-checks both themes in the browser.)

- [ ] **Step 4: `components/agenda/confirmation.css`**

```css
/* confirmation.css — the confirmation looks outside the grid (TASK-032 R5):
   badge, grid mark, legend. Tokens only. No animation or transition on purpose
   (the global prefers-reduced-motion rule in base.css stays the only motion rule). */

.conf-badge {
  display: inline-flex; align-items: center; gap: var(--sp-1);
  padding: 2px var(--sp-2); border-radius: var(--radius-pill);
  font-size: var(--fs-xs); font-weight: 600; line-height: 1.4;
  background: var(--tone-bg); color: var(--tone-ink); border: 1px solid var(--tone-bd);
}
.conf-badge-icon { display: inline-flex; }
.conf-badge--neutral { --tone-bg: var(--st-neutral-bg); --tone-ink: var(--st-neutral-ink); --tone-bd: var(--st-neutral-bd); }
.conf-badge--confirm { --tone-bg: var(--st-confirm-bg); --tone-ink: var(--st-confirm-ink); --tone-bd: var(--st-confirm-bd); }
.conf-badge--confirm2 { --tone-bg: var(--st-confirm2-bg); --tone-ink: var(--st-confirm2-ink); --tone-bd: var(--st-confirm2-bd); border-width: 2px; }
.conf-badge--attn { --tone-bg: var(--st-attn-bg); --tone-ink: var(--st-attn-ink); --tone-bd: var(--st-attn-bd); border-width: 2px; }

/* Mark inside a grid slot: decorative, the slot's accessible name carries the state. */
.cal-appt-conf { position: absolute; top: 3px; right: 5px; display: inline-flex; color: var(--tone-ink); }
.cal-appt-conf--inline { position: static; margin-left: var(--sp-1); }

.agenda-legend {
  display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4);
  list-style: none; margin: 0; padding: var(--sp-2) var(--sp-4) 0; flex-shrink: 0;
}
.agenda-legend-item { display: inline-flex; align-items: center; gap: var(--sp-2); }
.agenda-legend-hint { font-size: var(--fs-xs); color: var(--ink-faint); }
@media (max-width: 640px) { .agenda-legend-hint { display: none; } }
```

- [ ] **Step 5: `components/agenda/ConfirmationBadge.tsx`**

```tsx
// ConfirmationBadge / ConfirmationMark / ConfirmationLegend — the visible side of
// the four confirmation looks (lib/agenda/confirmation.ts). Colour is never the
// only cue: every look has a text label, and every non-neutral one an icon that
// is decorative (aria-hidden) because the text beside it already says the same.

import { Icon } from "../icons";
import { LEGEND_ENTRIES, type ConfirmationLook } from "../../lib/agenda/confirmation";
import "./confirmation.css";

/** Icon-only mark inside a grid slot. Decorative: the slot's accessible name carries the state in words. */
export function ConfirmationMark({ look, inline = false }: { look: ConfirmationLook | null; inline?: boolean }) {
  if (!look || !look.icon) return null; // neutral baseline: no mark on purpose
  return (
    <span
      className={`cal-appt-conf cal-appt-conf--${look.tone}${inline ? " cal-appt-conf--inline" : ""}`}
      data-state={look.state}
      data-ticks={look.ticks}
      aria-hidden="true"
    >
      <Icon name={look.icon} size={13} />
    </span>
  );
}

export function ConfirmationBadge({ look }: { look: ConfirmationLook }) {
  return (
    <span className={`conf-badge conf-badge--${look.tone}`} data-state={look.state} data-ticks={look.ticks}>
      {look.icon && (
        <span className="conf-badge-icon" aria-hidden="true">
          <Icon name={look.icon} size={13} />
        </span>
      )}
      <span>{look.label}</span>
    </span>
  );
}

export function ConfirmationLegend() {
  return (
    <ul className="agenda-legend" aria-label="Legenda da confirmação das consultas">
      {LEGEND_ENTRIES.map((look) => (
        <li key={look.state} className="agenda-legend-item">
          <ConfirmationBadge look={look} />
          <span className="agenda-legend-hint">{look.hint}</span>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 6: Grid CSS and `CalendarViews.tsx`**

Append to `components/agenda/calendar-views.css`:

```css
/* Confirmation tones (TASK-032 R5). The mark/badge live in confirmation.css. */
.cal-appt--neutral { --tone-bg: var(--st-neutral-bg); --tone-ink: var(--st-neutral-ink); --tone-bd: var(--st-neutral-bd); }
.cal-appt--confirm2 { --tone-bg: var(--st-confirm2-bg); --tone-ink: var(--st-confirm2-ink); --tone-bd: var(--st-confirm2-bd); }
.cal-appt--confirm2::before { width: 5px; }
.cal-appt--attn { --tone-bg: var(--st-attn-bg); --tone-ink: var(--st-attn-ink); --tone-bd: var(--st-attn-bd); border-width: 2px; }
.cal-appt--marked .cal-appt-name { max-width: calc(100% - 18px); }
.cal-cell-item--neutral { --tone-ink: var(--st-neutral-ink); }
.cal-cell-item--confirm2 { --tone-ink: var(--st-confirm2-ink); }
.cal-cell-item--attn { --tone-ink: var(--st-attn-ink); font-weight: 700; }
```

In `components/agenda/CalendarViews.tsx`:

1. Add imports `import { confirmationLook } from "../../lib/agenda/confirmation";` and `import { ConfirmationMark } from "./ConfirmationBadge";`.
2. Replace `const toneOf = (a: Appt): string => (STATUS_META[a.status] || {}).tone || "pending";` with:

```ts
// The confirmation look wins for a live appointment that has confirmation data;
// everything else (older backend, terminal status, block) keeps the status tone.
const toneOf = (a: Appt): string => confirmationLook(a)?.tone ?? (STATUS_META[a.status] || {}).tone || "pending";
```

3. In `apptLabel`, replace the last two lines (`const status = …; return …`) with:

```ts
  const status = a.status === "cancelado" ? " (cancelada)" : "";
  const look = confirmationLook(a);
  const confirmation = look ? `, ${look.ariaText}` : "";
  return `${t} ${a.patient || "Consulta"}${kind}${status}${confirmation}`;
```

4. In `ApptBlock`'s appointment `<button>`: change the className to ``cal-appt cal-appt--${toneOf(a)}${a.status === "cancelado" ? " cal-appt--cancelled" : ""}${look?.icon ? " cal-appt--marked" : ""}`` (declare `const look = confirmationLook(a);` just before the `return`), and add `<ConfirmationMark look={look} />` as the first child inside the button. Do the same in `DayBlock` (class suffix `cal-appt--marked`, mark first child), and in `DayBlock` replace `{h > 50 && <span className="cal-appt-status">{STATUS_META[a.status].label}</span>}` with `{h > 50 && <span className="cal-appt-status">{look ? look.label : STATUS_META[a.status].label}</span>}`.
5. In `MonthView`, replace the `summary` computation with:

```ts
          const unconfirmed = appts.filter((a) => confirmationLook(a)?.state === "attention").length;
          const summary =
            (count === 0 ? "sem consultas" : `${count} ${count === 1 ? "consulta" : "consultas"}`) +
            (unconfirmed > 0 ? `, ${unconfirmed} sem confirmação` : "");
```

and inside each `cal-cell-item` span, after the `cal-cell-text` span add `<ConfirmationMark look={confirmationLook(a)} inline />`.

- [ ] **Step 7: Legend in `AgendaView.tsx`**

Import `ConfirmationLegend` from `./ConfirmationBadge`. Directly after the `<div className="agenda-notices">…</div>` block add:

```tsx
      {/* Only when the read carried confirmation data: an older backend has nothing to explain. */}
      {showGrid && state.items?.some((a) => a.confirmation !== undefined) && <ConfirmationLegend />}
```

- [ ] **Step 8: Run tests, type check**

Run: `npx vitest run components/agenda` — Expected: PASS (including the pre-existing agenda tests: no confirmation data means unchanged markup).
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add app/styles/tokens.css components/agenda/confirmation.css components/agenda/ConfirmationBadge.tsx components/agenda/calendar-views.css components/agenda/CalendarViews.tsx components/agenda/AgendaView.tsx components/agenda/__tests__/confirmation-views.test.tsx components/agenda/__tests__/confirmation-css.test.ts components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): confirmation tones, ticks, marks and legend on the grid (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 3: Drawer — badge, reminder timeline and the red-state actions

**Files:**
- Modify: `components/agenda/Drawer.tsx`, `components/agenda/agenda-modals.css` (append)
- Test: `components/agenda/__tests__/drawer.test.tsx` (new `describe` blocks at the end)

**Interfaces:**
- Consumes: `confirmationLook`, `reminderTimeline` (Task 1); `ConfirmationBadge` (Task 2); existing `canActOn`, `StatusActionWire`.
- Produces: new optional `DrawerProps`: `onRelease?: (appt: Appt) => void` (opens the release card, Task 6) and `onMessage?: (appt: Appt) => void` (opens the message sheet, Task 6). "Marcar como confirmado" reuses the existing `onStatus(appt, "confirmed")`. Section/button names: region `Confirmação`, buttons `Enviar mensagem ao paciente`, `Marcar como confirmado`, `Liberar horário`; region `Lembretes` with an `<ol aria-label="Linha do tempo dos lembretes">`.

- [ ] **Step 1: Write the failing tests**

Append to `components/agenda/__tests__/drawer.test.tsx` (it already defines `render`, `appt`, `button`, `noop`):

```tsx
describe("Drawer — confirmation state (TASK-032 R5)", () => {
  const red = { state: "attention", count: 0 } as const;
  const reminders = [
    { kind: "day", status: "sent", dueAt: new Date(2026, 8, 28, 9, 0).toISOString(), sentAt: new Date(2026, 8, 28, 9, 1).toISOString(), warnedAt: new Date(2026, 8, 28, 11, 5).toISOString(), warnKind: "unconfirmed" },
  ];

  it("shows the state as TEXT in a badge (not colour alone)", () => {
    expect(render(appt({ confirmation: red }))).toContain("Sem confirmação");
    expect(render(appt({ confirmation: { state: "confirmed", count: 1 } }))).toContain("Confirmada");
    const twice = render(appt({ confirmation: { state: "confirmed_twice", count: 2 } }));
    expect(twice).toContain("Confirmada duas vezes");
    expect(twice).toContain('data-ticks="2"');
  });

  it("red: offers the three actions as real buttons with their visible names", () => {
    const html = render(appt({ confirmation: red }), { onRelease: noop, onMessage: noop });
    expect(html).toContain('aria-label="Confirmação"');
    for (const name of ["Enviar mensagem ao paciente", "Marcar como confirmado", "Liberar horário"]) {
      expect(button(html, name)).not.toContain("disabled");
    }
  });

  it("the actions need their callbacks: absent -> disabled, never a silent no-op", () => {
    const html = render(appt({ confirmation: red }), { onStatus: undefined });
    expect(button(html, "Enviar mensagem ao paciente")).toContain("disabled");
    expect(button(html, "Liberar horário")).toContain("disabled");
    expect(button(html, "Marcar como confirmado")).toContain("disabled");
  });

  it("while a status write is in flight 'Marcar como confirmado' is disabled and busy", () => {
    const html = render(appt({ confirmation: red }), { onRelease: noop, onMessage: noop, statusPending: "confirmed" });
    expect(button(html, "Marcar como confirmado")).toContain("disabled");
    expect(button(html, "Marcar como confirmado")).toContain('aria-busy="true"');
  });

  it("green / neutral: no red actions", () => {
    for (const confirmation of [{ state: "confirmed", count: 1 }, { state: "unconfirmed", count: 0 }] as const) {
      const html = render(appt({ confirmation }), { onRelease: noop, onMessage: noop });
      expect(html).not.toContain("Liberar horário");
      expect(html).not.toContain('aria-label="Confirmação"');
    }
  });

  it("cancelled / attended / no-show keep their own look: no confirmation badge or red actions", () => {
    for (const status of ["cancelado", "compareceu", "faltou"] as const) {
      const html = render(appt({ status, confirmation: red }), { onRelease: noop, onMessage: noop });
      expect(html).not.toContain("Sem confirmação");
      expect(html).not.toContain("Liberar horário");
    }
    const learned = render(appt({ confirmation: red }), { onRelease: noop, onMessage: noop, knownStatus: "attended" });
    expect(learned).not.toContain("Sem confirmação");
    expect(learned).not.toContain("Liberar horário");
    expect(learned).toContain("Compareceu");
  });

  it("a Google-only slot (no appointment id) never offers the red actions", () => {
    const html = render(appt({ confirmation: red, appointmentId: null }), { onRelease: noop, onMessage: noop });
    expect(html).not.toContain("Liberar horário");
    expect(html).toContain(CANCEL_UNAVAILABLE_HINT);
  });

  it("a block is untouched by stray confirmation data", () => {
    const html = render(appt({ status: "bloqueio", reason: "Almoço", confirmation: red, reminders }));
    expect(html).toContain(BLOCK_HINT);
    expect(html).not.toContain("Sem confirmação");
    expect(html).not.toContain("Lembretes");
  });

  it("older backend (no confirmation, no reminders): no badge, no timeline, no confirmation region", () => {
    const html = render(appt());
    expect(html).not.toContain("Lembretes");
    expect(html).not.toContain('aria-label="Confirmação"');
    expect(html).not.toContain("conf-badge");
  });

  it("renders the timeline as a named ordered list, from the data's own timestamps", () => {
    const html = render(appt({ confirmation: red, reminders }));
    expect(html).toContain('<ol class="agm-timeline" aria-label="Linha do tempo dos lembretes">');
    expect(html).toContain("Lembrete de 1 dia enviado");
    expect(html).toContain("Clínica avisada: sem confirmação");
    expect(html).toContain("28/09 09:01");
  });

  it("no reminders -> no timeline region", () => {
    expect(render(appt({ confirmation: red, reminders: [] }))).not.toContain("Linha do tempo");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/agenda/__tests__/drawer.test.tsx`
Expected: FAIL (no badge / no "Liberar horário").

- [ ] **Step 3: CSS**

Append to `components/agenda/agenda-modals.css`:

```css
/* Reminder timeline + red-state actions (TASK-032 R5). No motion on purpose. */
.agm-timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.agm-timeline-item { display: grid; grid-template-columns: 7.5em 1fr; gap: var(--sp-2); font-size: var(--fs-md); color: var(--ink); }
.agm-timeline-when { font-size: var(--fs-sm); color: var(--ink-faint); font-variant-numeric: tabular-nums; }
.agm-timeline-item--ok .agm-timeline-text { color: var(--st-confirm-ink); font-weight: 600; }
.agm-timeline-item--warn .agm-timeline-text { color: var(--st-attn-ink); font-weight: 600; }
.agm-confirm {
  padding: var(--sp-3); border: 2px solid var(--st-attn-bd); background: var(--st-attn-bg);
  border-radius: var(--radius-control);
}
.agm-confirm .agm-hint { color: var(--ink-soft); }
.agm-confirm-actions { display: flex; flex-direction: column; gap: var(--sp-2); }
```

- [ ] **Step 4: Implement in `components/agenda/Drawer.tsx`**

(a) Imports — add:

```tsx
import { confirmationLook, reminderTimeline } from "../../lib/agenda/confirmation";
import { ConfirmationBadge } from "./ConfirmationBadge";
```

(b) In `DrawerProps`, after `onStatus?`:

```tsx
  /** Opens the "Liberar horário" card (red state only). Absent → the action is disabled. */
  onRelease?: (appt: Appt) => void;
  /** Opens the "Enviar mensagem ao paciente" sheet (red state only). Absent → disabled. */
  onMessage?: (appt: Appt) => void;
```

(c) In `AppointmentBody`, change the destructuring to `const { appt, onCancel, onReschedule, onStatus, onRelease, onMessage, statusError } = props;` and, after the `deposit` constant, add:

```tsx
  // A status learned from a write that ended the appointment keeps ITS look; so does a stored terminal status.
  const learnedTerminal = knownStatus === "cancelled" || knownStatus === "attended" || knownStatus === "no_show";
  const look = learnedTerminal ? null : confirmationLook(appt);
  const timeline = reminderTimeline(appt.reminders);
  const showConfirmActions = look?.state === "attention" && actionable;
  const canConfirm = Boolean(onStatus) && props.statusPending == null;
```

(d) Replace the identity badges

```tsx
          {knownStatus && <Badge tone={badgeTone(knownStatus)}>{WIRE_STATUS_LABEL[knownStatus]}</Badge>}
          {!knownStatus && appt.status === "cancelado" && <Badge tone="cancelada">Cancelada</Badge>}
```

with

```tsx
          {look && <ConfirmationBadge look={look} />}
          {!look && knownStatus && <Badge tone={badgeTone(knownStatus)}>{WIRE_STATUS_LABEL[knownStatus]}</Badge>}
          {!look && !knownStatus && appt.status === "cancelado" && <Badge tone="cancelada">Cancelada</Badge>}
```

(e) Directly after the closing `</div>` of `<div className="agm-info">…</div>` and before `{actionable && (<section … aria-label="Status">`, insert:

```tsx
      {timeline.length > 0 && (
        <section className="agm-section" aria-label="Lembretes">
          <h3 className="agm-section-title">Lembretes</h3>
          <ol className="agm-timeline" aria-label="Linha do tempo dos lembretes">
            {timeline.map((e, i) => (
              <li key={`${e.at}-${i}`} className={`agm-timeline-item agm-timeline-item--${e.tone}`}>
                <span className="agm-timeline-when">{e.when}</span>
                <span className="agm-timeline-text">{e.text}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {showConfirmActions && look && (
        <section className="agm-section agm-confirm" aria-label="Confirmação">
          <h3 className="agm-section-title">Confirmação</h3>
          <p className="agm-hint">{look.hint} Escolha o que fazer:</p>
          <div className="agm-confirm-actions">
            <Button icon="send" disabled={!onMessage} onClick={onMessage ? () => onMessage(appt) : undefined}>
              Enviar mensagem ao paciente
            </Button>
            <Button
              icon="check"
              disabled={!canConfirm}
              aria-busy={props.statusPending === "confirmed" || undefined}
              onClick={canConfirm ? () => onStatus!(appt, "confirmed") : undefined}
            >
              Marcar como confirmado
            </Button>
            <Button variant="danger" icon="calendar" disabled={!onRelease} onClick={onRelease ? () => onRelease(appt) : undefined}>
              Liberar horário
            </Button>
          </div>
        </section>
      )}
```

- [ ] **Step 5: Run tests and type check**

Run: `npx vitest run components/agenda` — Expected: PASS (existing drawer tests unchanged: no confirmation data renders nothing new).
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add components/agenda/Drawer.tsx components/agenda/agenda-modals.css components/agenda/__tests__/drawer.test.tsx
git commit -m "feat(agenda): drawer shows the confirmation badge, reminder timeline and red-state actions (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 4: Hub client (release, message) and the release/message rules

**Files:**
- Modify: `lib/agenda/secretaria-hub-agenda.ts`
- Create: `lib/agenda/release.ts`
- Test: `lib/agenda/__tests__/release.test.ts`, `lib/agenda/__tests__/secretaria-hub-agenda-release.test.ts`

**Interfaces:**
- Consumes: `hubFetch`, `HubApiError`, `AppointmentWire`, `Appt`, `patientFirstName` (`modal-forms.ts`); R4's routes exactly as copied under "Consumes".
- Produces (names Tasks 5–6 use):
  - In `secretaria-hub-agenda.ts`: constants `RELEASE_ACK_REQUIRED_CODE = "retention_ack_required"`, `RELEASE_ALREADY_CONFIRMED_CODE = "already_confirmed"`, `RELEASE_NOT_LIVE_CODE = "not_live"`, `MESSAGE_OUTSIDE_WINDOW_CODE = "outside_window_not_authorised"`, `MESSAGE_NO_CHANNEL_CODE = "no_channel"`; types `AppointmentReleasePayload = { acknowledge_retention: boolean; release_confirmed: boolean; notify_outside_window: boolean }`, `AppointmentReleaseWire = AppointmentWire & { patient_notice?: string | null }`, `AppointmentMessagePayload = { text: string; notify_outside_window: boolean }`, `AppointmentMessageWire = { delivery?: string | null; email_nudge?: string | null; message_id?: string | null }`; `releaseAppointment(session, appointmentId, payload): Promise<AppointmentReleaseWire>`; `messageAppointment(session, appointmentId, payload): Promise<AppointmentMessageWire>`.
  - In `release.ts`: `RELEASE_COPY`, `RELEASE_FAILED`, `RELEASE_UNAVAILABLE`, `RELEASE_ALREADY_DONE`, `RELEASE_ALREADY_CONFIRMED`, `RELEASE_ACK_REQUIRED`, `MESSAGE_COPY`, `MESSAGE_FAILED`, `MESSAGE_NO_CHANNEL`, `MESSAGE_MAX = 1000`, `type ReleaseConfirm = { acknowledged: boolean; optIn: boolean }`, `type ReleaseOptions`, `releaseNeedsAck(appt, forced?)`, `buildReleaseBody(appt, options) => AppointmentReleasePayload | null`, `releaseConfirmLabel(...)`, `messageSuggestion(appt)`, `validateMessage(text)`.

- [ ] **Step 1: Write the failing tests**

Create `lib/agenda/__tests__/release.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { MESSAGE_MAX, RELEASE_COPY, buildReleaseBody, messageSuggestion, releaseConfirmLabel, releaseNeedsAck, validateMessage } from "../release";
import type { Appt } from "../types";

const base: Appt = {
  id: "hub-1", date: "2026-09-29", day: 2, start: 9 * 60 + 30, dur: 30,
  patient: "Maria Souza", type: "Consulta", summary: "Consulta — Maria Souza", status: "agendado", appointmentId: "ap-1",
};
const paid: Appt = { ...base, deposit: { status: "confirmado_pago", amountCents: 4500 } };
const opts = (over: Partial<Parameters<typeof buildReleaseBody>[1]> = {}) => ({
  acknowledged: false, forceAck: false, confirmedSeen: false, notifyOutsideWindow: false, ...over,
});

describe("releaseNeedsAck / buildReleaseBody", () => {
  it("a PAID deposit needs the acknowledgement; no deposit / pending / refunded do not", () => {
    expect(releaseNeedsAck(paid)).toBe(true);
    expect(releaseNeedsAck(base)).toBe(false);
    for (const status of ["aguardando_sinal", "expirado", "cancelado_reembolsado", "cancelado_retido", "no_show_retido"] as const) {
      expect(releaseNeedsAck({ ...base, deposit: { status, amountCents: null } })).toBe(false);
    }
  });

  it("paid: nothing is built until the clinic acknowledged; then acknowledge_retention is true", () => {
    expect(buildReleaseBody(paid, opts())).toBeNull();
    expect(buildReleaseBody(paid, opts({ acknowledged: true }))).toEqual({
      acknowledge_retention: true, release_confirmed: false, notify_outside_window: false,
    });
  });

  it("not paid: the body is built with no acknowledgement and says false", () => {
    expect(buildReleaseBody(base, opts())).toEqual({ acknowledge_retention: false, release_confirmed: false, notify_outside_window: false });
  });

  it("a backend refusal (forceAck) makes the step mandatory even when the read showed no deposit", () => {
    expect(buildReleaseBody(base, opts({ forceAck: true }))).toBeNull();
    expect(buildReleaseBody(base, opts({ forceAck: true, acknowledged: true }))?.acknowledge_retention).toBe(true);
  });

  it("release_confirmed is only true after the clinic saw 'already confirmed'; the paid notice only when authorised", () => {
    expect(buildReleaseBody(base, opts({ confirmedSeen: true }))?.release_confirmed).toBe(true);
    expect(buildReleaseBody(base, opts({ notifyOutsideWindow: true }))?.notify_outside_window).toBe(true);
  });

  it("labels the confirm button for what it will do", () => {
    expect(releaseConfirmLabel({ pending: false, checking: false, confirmedSeen: false, paidNotice: false })).toBe(RELEASE_COPY.confirm);
    expect(releaseConfirmLabel({ pending: false, checking: false, confirmedSeen: true, paidNotice: false })).toBe(RELEASE_COPY.confirmAnyway);
    expect(releaseConfirmLabel({ pending: false, checking: false, confirmedSeen: false, paidNotice: true })).toBe(RELEASE_COPY.confirmPaidNotice);
    expect(releaseConfirmLabel({ pending: false, checking: true, confirmedSeen: false, paidNotice: false })).toBe(RELEASE_COPY.checking);
    expect(releaseConfirmLabel({ pending: true, checking: false, confirmedSeen: true, paidNotice: true })).toBe(RELEASE_COPY.pending);
  });
});

describe("message helpers", () => {
  it("suggests a short message from the slot's own date and time, naming the patient only when it is a name", () => {
    expect(messageSuggestion(base)).toBe(
      "Olá, Maria! Passando para confirmar sua consulta no dia 29/09 às 09:30. Você consegue vir? É só responder por aqui.",
    );
    expect(messageSuggestion({ ...base, summary: "Dentista da Maria" })).toContain("Olá! Passando");
  });

  it("validates: empty and whitespace refused, trimmed otherwise, capped", () => {
    expect(validateMessage("   ")).toEqual({ ok: false, reason: "empty" });
    expect(validateMessage("  oi  ")).toEqual({ ok: true, text: "oi" });
    expect(validateMessage("x".repeat(MESSAGE_MAX))).toMatchObject({ ok: true });
    expect(validateMessage("x".repeat(MESSAGE_MAX + 1))).toEqual({ ok: false, reason: "too_long" });
  });
});
```

Create `lib/agenda/__tests__/secretaria-hub-agenda-release.test.ts`:

```ts
// The two R4 calls through the real hubFetch (fetch stubbed, brain-session mocked), same idiom as lib/real/__tests__/secretaria-hub.test.ts.
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HubApiError, SECRETARIA_HUB_BASE } from "../../real/secretaria-hub";
import type { Session } from "../../real/brain-session";
import {
  MESSAGE_OUTSIDE_WINDOW_CODE,
  RELEASE_ACK_REQUIRED_CODE,
  messageAppointment,
  releaseAppointment,
} from "../secretaria-hub-agenda";

vi.mock("../../real/brain-session", () => ({
  mintHubToken: vi.fn(async () => ({ hubToken: "fake-hub-token", expiresIn: 3600 })),
}));

const session: Session = { token: "t", tenantId: "tenant-1", email: "d@x.com", role: "doctor", userId: "u1" };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal("fetch", vi.fn());
});

describe("releaseAppointment", () => {
  it("POSTs the three flags to the appointment's release route (id URL-encoded) and returns the notice", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { id: "ap 1", status: "cancelled", patient_notice: "whatsapp_queued" }));
    const res = await releaseAppointment(session, "ap 1", { acknowledge_retention: true, release_confirmed: false, notify_outside_window: false });
    expect(res.status).toBe("cancelled");
    expect(res.patient_notice).toBe("whatsapp_queued");
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/calendar/appointments/ap%201/release`);
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBe('{"acknowledge_retention":true,"release_confirmed":false,"notify_outside_window":false}');
  });

  it("exposes the structured refusal: status, code and the backend's own message", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json(409, { detail: { code: RELEASE_ACK_REQUIRED_CODE, message: "O sinal pago será retido.", deposit_outcome: "retained", amount_cents: 4500 } }),
    );
    const err = await releaseAppointment(session, "ap-1", { acknowledge_retention: false, release_confirmed: false, notify_outside_window: false }).catch((e) => e);
    expect(err).toBeInstanceOf(HubApiError);
    expect(err.status).toBe(409);
    expect(err.code).toBe(RELEASE_ACK_REQUIRED_CODE);
    expect(err.message).toBe("O sinal pago será retido.");
  });
});

describe("messageAppointment", () => {
  it("POSTs text + the paid-notice flag and returns the delivery facts", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { delivery: "portal_chat", email_nudge: "sent", message_id: null }));
    const res = await messageAppointment(session, "ap-1", { text: "Olá", notify_outside_window: false });
    expect(res.delivery).toBe("portal_chat");
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/calendar/appointments/ap-1/message`);
    expect((init as RequestInit).body).toBe('{"text":"Olá","notify_outside_window":false}');
  });

  it("keeps the cost and wa.me link of an outside-window refusal on error.detail", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json(409, { detail: { code: MESSAGE_OUTSIDE_WINDOW_CODE, message: "fora da janela", template_cost_brl: "R$ 0,35", whatsapp_link: "https://wa.me/5511999990000" } }),
    );
    const err = await messageAppointment(session, "ap-1", { text: "Olá", notify_outside_window: false }).catch((e) => e);
    expect(err.code).toBe(MESSAGE_OUTSIDE_WINDOW_CODE);
    expect((err.detail as { template_cost_brl: string }).template_cost_brl).toBe("R$ 0,35");
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/release.test.ts lib/agenda/__tests__/secretaria-hub-agenda-release.test.ts`
Expected: FAIL (modules/exports missing).

- [ ] **Step 3: Add the client calls**

In `lib/agenda/secretaria-hub-agenda.ts`, add to the header route list the two routes (`POST …/appointments/{id}/release`, `POST …/appointments/{id}/message`, TASK-032 R4), and after `AppointmentReschedulePayload` add:

```ts
// R4 refusal codes (`detail.code`) the agenda reacts to.
export const RELEASE_ACK_REQUIRED_CODE = "retention_ack_required"; // paid Pix deposit, no acknowledgement (409)
export const RELEASE_ALREADY_CONFIRMED_CODE = "already_confirmed"; // the patient confirmed meanwhile (409)
export const RELEASE_NOT_LIVE_CODE = "not_live"; // already released / cancelled / attended (409)
export const MESSAGE_OUTSIDE_WINDOW_CODE = "outside_window_not_authorised"; // WhatsApp, outside 24 h, not authorised (409)
export const MESSAGE_NO_CHANNEL_CODE = "no_channel"; // nothing to deliver on (422)

export type AppointmentReleasePayload = {
  /** true only after the clinic ticked the Pix-retention warning. */
  acknowledge_retention: boolean;
  /** true only after the clinic saw "the patient just confirmed" and chose to release anyway. */
  release_confirmed: boolean;
  /** Authorises the PAID WhatsApp template for the patient notice outside the 24 h window. */
  notify_outside_window: boolean;
};

/**
 * AppointmentRead plus how the patient notice went. Kept a plain string on purpose:
 * "whatsapp_queued" | "whatsapp_outside_window" | "portal_chat" | "portal_chat_email" |
 * "no_channel" | "queue_unavailable" | "notice_failed" (an unknown value is just not reported).
 */
export type AppointmentReleaseWire = AppointmentWire & { patient_notice?: string | null };

export type AppointmentMessagePayload = { text: string; notify_outside_window: boolean };

/** `delivery`: "whatsapp_text" | "whatsapp_template" | "portal_chat"; `email_nudge` only for the Portal: "sent" | "no_email" | "not_sent". */
export type AppointmentMessageWire = {
  delivery?: string | null;
  email_nudge?: string | null;
  message_id?: string | null;
};
```

and at the end of the file:

```ts
/** Frees the slot: deletes the Google event, cancels the appointment, tells the patient (R4). */
export function releaseAppointment(
  session: Session,
  appointmentId: string,
  payload: AppointmentReleasePayload,
): Promise<AppointmentReleaseWire> {
  return hubFetch<AppointmentReleaseWire>(session, apptPath(appointmentId, "release"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Free text to the appointment's patient, on the patient's own channel (R4). */
export function messageAppointment(
  session: Session,
  appointmentId: string,
  payload: AppointmentMessagePayload,
): Promise<AppointmentMessageWire> {
  return hubFetch<AppointmentMessageWire>(session, apptPath(appointmentId, "message"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
```

- [ ] **Step 4: `lib/agenda/release.ts`**

```ts
// lib/agenda/release.ts — the rules behind "Liberar horário" and "Enviar mensagem ao
// paciente" (TASK-032 R5). Pure, so the Pix-retention gate is testable without a DOM.
//
// The gate: releasing an appointment whose Pix deposit is PAID can make the clinic
// retain the money (secretarIA services/payments/deposit_lifecycle.py). The clinic
// must acknowledge that before any request leaves; `buildReleaseBody` returns null
// until it did, and the backend refuses a release without it (409
// retention_ack_required), which makes the step mandatory (`forceAck`) even when
// the read did not show the deposit.

import type { Appt } from "./types";
import { fmtTime } from "./types";
import { patientFirstName } from "./modal-forms";
import type { AppointmentReleasePayload } from "./secretaria-hub-agenda";

export const RELEASE_COPY = {
  title: "Liberar horário",
  intro:
    "O horário volta a ficar livre na agenda, a consulta é cancelada e o paciente é avisado, com a opção de remarcar.",
  noticeTitle: "Aviso ao paciente",
  ackTitle: "Este paciente já pagou o sinal",
  ackBody:
    "Liberar o horário agora, dentro da janela de reembolso, pode fazer a clínica reter o valor pago. Confirme que você entende antes de continuar.",
  ackLabel: "Entendo que o sinal pago pode ser retido",
  confirm: "Liberar horário",
  confirmAnyway: "Liberar mesmo assim",
  confirmPaidNotice: "Liberar e enviar aviso pago",
  checking: "Verificando…",
  pending: "Liberando…",
  back: "Voltar",
} as const;

export const RELEASE_FAILED = "Não foi possível liberar o horário. Nada foi alterado. Tente novamente.";
export const RELEASE_UNAVAILABLE = "O Google Calendar não respondeu. Nada foi alterado. Tente novamente em instantes.";
export const RELEASE_ALREADY_DONE = "Esta consulta já não está ativa (foi cancelada ou liberada). Feche o cartão e atualize a agenda.";
export const RELEASE_ALREADY_CONFIRMED =
  "O paciente acabou de confirmar esta consulta. Se mesmo assim quiser liberar o horário, clique em Liberar mesmo assim.";
export const RELEASE_ACK_REQUIRED = "O sinal desta consulta já foi pago. Marque a confirmação abaixo para continuar.";

export const MESSAGE_COPY = {
  title: "Enviar mensagem ao paciente",
  label: "Mensagem para o paciente",
  hint: "Vai pelo canal do paciente (WhatsApp ou conversa do Portal).",
  send: "Enviar mensagem",
  pending: "Enviando…",
  back: "Voltar",
  outsideTitle: "O paciente está fora da janela de 24 h do WhatsApp",
  outsideBody:
    "Mensagem livre só chega dentro de 24 h da última resposta dele. Você pode enviar uma mensagem oficial (cobrada) ou escrever pelo seu próprio WhatsApp.",
  outsideLabel: "Enviar como mensagem oficial (cobrada)",
  openWhatsapp: "Abrir o WhatsApp",
} as const;

export const MESSAGE_FAILED = "Não foi possível enviar a mensagem. Nada foi enviado. Tente novamente.";
export const MESSAGE_NO_CHANNEL = "Este paciente não tem um canal para receber mensagens pela secretarIA.";
export const MESSAGE_MAX = 1000;

/** What the release card hands the controller: the two ticks the clinic made. */
export type ReleaseConfirm = { acknowledged: boolean; optIn: boolean };

export type ReleaseOptions = {
  /** The clinic ticked the Pix-retention warning. */
  acknowledged: boolean;
  /** The backend already asked for the acknowledgement (409 retention_ack_required). */
  forceAck: boolean;
  /** The backend already said "the patient just confirmed" and the clinic clicked again. */
  confirmedSeen: boolean;
  /** The clinic authorised the paid template for a patient outside the 24 h window. */
  notifyOutsideWindow: boolean;
};

/** True when the clinic must acknowledge a possible retention before releasing. */
export function releaseNeedsAck(appt: Pick<Appt, "deposit">, forced = false): boolean {
  return forced || appt.deposit?.status === "confirmado_pago";
}

/** The POST body, or null while a needed acknowledgement is missing (nothing may be sent then). */
export function buildReleaseBody(appt: Pick<Appt, "deposit">, o: ReleaseOptions): AppointmentReleasePayload | null {
  const needs = releaseNeedsAck(appt, o.forceAck);
  if (needs && !o.acknowledged) return null;
  return { acknowledge_retention: needs, release_confirmed: o.confirmedSeen, notify_outside_window: o.notifyOutsideWindow };
}

/** The confirm button's text for what it will do right now. */
export function releaseConfirmLabel(s: { pending: boolean; checking: boolean; confirmedSeen: boolean; paidNotice: boolean }): string {
  if (s.pending) return RELEASE_COPY.pending;
  if (s.checking) return RELEASE_COPY.checking;
  if (s.confirmedSeen) return RELEASE_COPY.confirmAnyway;
  return s.paidNotice ? RELEASE_COPY.confirmPaidNotice : RELEASE_COPY.confirm;
}

/** An opt-in starting text, built only from what the slot knows (its own date and time, and a name only when it is one). */
export function messageSuggestion(appt: Appt): string {
  const name = patientFirstName(appt);
  const greeting = name ? `Olá, ${name}!` : "Olá!";
  const [, month, day] = appt.date.split("-");
  return `${greeting} Passando para confirmar sua consulta no dia ${day}/${month} às ${fmtTime(appt.start)}. Você consegue vir? É só responder por aqui.`;
}

export type MessageCheck = { ok: true; text: string } | { ok: false; reason: "empty" | "too_long" };

export function validateMessage(text: string): MessageCheck {
  const trimmed = text.trim();
  if (!trimmed) return { ok: false, reason: "empty" };
  if (trimmed.length > MESSAGE_MAX) return { ok: false, reason: "too_long" };
  return { ok: true, text: trimmed };
}
```

- [ ] **Step 5: Run tests and type check**

Run: `npx vitest run lib/agenda` — Expected: PASS.
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add lib/agenda/secretaria-hub-agenda.ts lib/agenda/release.ts lib/agenda/__tests__/release.test.ts lib/agenda/__tests__/secretaria-hub-agenda-release.test.ts
git commit -m "feat(agenda): hub client and rules for releasing a slot and messaging the patient (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 5: Controller and reducer — release, message, deep link, and the live red-to-green flip

**Files:**
- Modify: `lib/agenda/confirmation.ts` (add `applyStaffConfirmation`), `lib/agenda/screen.ts`
- Test: `lib/agenda/__tests__/confirmation.test.ts` (append), `lib/agenda/__tests__/screen.test.ts` (modify `fakeHub`, append)

**Interfaces:**
- Consumes: Task 4 client/rules, `canActOn`, `cancelNoticeState`, the controller's existing `loadCheck(appointmentId)` (cancel-preview lookup), `HubApiError`.
- Produces (Task 6 wires these):
  - `applyStaffConfirmation(a: Appt): Appt` in `confirmation.ts`.
  - In `screen.ts`: `ReleaseModalError = { kind: "already_done" } | { kind: "ack_required"; message: string } | { kind: "already_confirmed" } | { kind: "failed"; message: string }`; `MessageModalError = { kind: "failed"; message: string } | { kind: "no_channel" } | { kind: "outside_window"; costBrl: string; waLink: string | null }`; `AgendaModal` gains `{ type: "release"; appt; pending; error: ReleaseModalError | null; forceAck: boolean; confirmedSeen: boolean }` and `{ type: "message"; appt; pending; error: MessageModalError | null }`; scoped action `{ type: "confirmation_recorded"; appointmentId: string }`; `AgendaHub.releaseAppointment`/`messageAppointment`; `releasedToast(res)`, `messageSentToast(res)`; controller methods `openRelease(appt): Promise<void>` (opens and reads the 24 h window), `confirmRelease(c: ReleaseConfirm): Promise<void>`, `openMessage(appt): void`, `sendMessage(text: string, notifyOutsideWindow: boolean): Promise<void>`.
  - `parseAgendaParams` also accepts `?consulta=` (the id R4's warning e-mail links with) as an alias of `?consultaId=`.
  - Behaviour contract: `confirmRelease` sends **nothing** while the Pix gate is open or the 24 h verdict is still loading; any failure leaves the modal open with its error, `pending` false, `selectedId`/`items`/`knownStatus` untouched and no success toast.

- [ ] **Step 1: Write the failing tests**

Append to `lib/agenda/__tests__/confirmation.test.ts` (and add `applyStaffConfirmation` to its import list from `"../confirmation"`):

```ts
describe("applyStaffConfirmation (the live flip)", () => {
  const base: Appt = { id: "h", date: "2026-09-29", day: 2, start: 540, dur: 30, patient: "M", status: "agendado", appointmentId: "ap-1" };

  it("red -> confirmed, status confirmou", () => {
    const out = applyStaffConfirmation({ ...base, confirmation: { state: "attention", count: 0 } });
    expect(out.status).toBe("confirmou");
    expect(out.confirmation).toEqual({ state: "confirmed", count: 1 });
  });

  it("never lowers or exceeds the cap: two stays two", () => {
    expect(applyStaffConfirmation({ ...base, confirmation: { state: "confirmed_twice", count: 2 } }).confirmation).toEqual({
      state: "confirmed_twice",
      count: 2,
    });
  });

  it("terminal statuses and blocks come back untouched (same object)", () => {
    for (const status of ["cancelado", "compareceu", "faltou", "bloqueio"] as const) {
      const a = { ...base, status, confirmation: { state: "attention", count: 0 } as const };
      expect(applyStaffConfirmation(a)).toBe(a);
    }
  });

  it("an older-backend slot (no confirmation data) gets the status only, never a confirmation key", () => {
    const out = applyStaffConfirmation(base);
    expect(out.status).toBe("confirmou");
    expect("confirmation" in out).toBe(false);
  });
});
```

In `lib/agenda/__tests__/screen.test.ts`:

(a) add to the import from `"../screen"`: `releasedToast`, `messageSentToast`; add `import { MESSAGE_FAILED, RELEASE_FAILED, RELEASE_UNAVAILABLE } from "../release";` and `import { MESSAGE_NO_CHANNEL_CODE, MESSAGE_OUTSIDE_WINDOW_CODE, RELEASE_ACK_REQUIRED_CODE, RELEASE_ALREADY_CONFIRMED_CODE, RELEASE_NOT_LIVE_CODE } from "../secretaria-hub-agenda";`.

(b) in `fakeHub`, after the `updateAppointmentStatus` line, add:

```ts
    releaseAppointment: vi.fn(async (_s, id) => ({ ...wire(id, "cancelled"), patient_notice: "whatsapp_queued" })),
    messageAppointment: vi.fn(async () => ({ delivery: "whatsapp_text", email_nudge: null, message_id: null })),
```

(c) extend the existing parse test (`it("parses ?data=, ?view= and ?consultaId=, ignoring junk"`) with one more `it` right after it:

```ts
  it("accepts ?consulta= (the id the clinic's warning e-mail links with); ?consultaId= wins when both are present", () => {
    expect(parseAgendaParams("?consulta=ap-9").consultaId).toBe("ap-9");
    expect(parseAgendaParams("?consultaId=a&consulta=b").consultaId).toBe("a");
    expect(parseAgendaParams("?consulta=%20").consultaId).toBeNull();
  });
```

(d) append at the end of the file:

```ts
// ---------------------------------------------------------------------------
// TASK-032 R5 — confirmation flip, release, message
// ---------------------------------------------------------------------------

const redEvent = (over: Partial<CalendarEventWire> = {}): CalendarEventWire => ({
  ...event("e1", 29, 9),
  status: "scheduled",
  confirmation_count: 0,
  display_state: "attention",
  attention: true,
  ...over,
});
const greenEvent = (over: Partial<CalendarEventWire> = {}) =>
  redEvent({ status: "confirmed", confirmation_count: 1, display_state: "confirmed", attention: false, ...over });
const gate = { acknowledged: false, optIn: false };

describe("confirmation — red turns green live", () => {
  it("flips right after 'Marcar como confirmado', before the refetch lands; the refetch then wins", async () => {
    const second = deferred<CalendarEventWire[]>();
    const list = vi.fn().mockResolvedValueOnce([redEvent()]).mockReturnValueOnce(second.promise);
    const s = setup({ hub: fakeHub({ listCalendarEvents: list }) });
    await s.controller.start();
    expect(appt(s.state()).confirmation?.state).toBe("attention");

    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(appt(s.state()).status).toBe("confirmou");
    expect(appt(s.state()).confirmation).toEqual({ state: "confirmed", count: 1 });

    second.resolve([greenEvent({ confirmation_count: 2, display_state: "confirmed_twice" })]);
    await flush();
    expect(appt(s.state()).confirmation).toEqual({ state: "confirmed_twice", count: 2 });
  });

  it("a failed confirm leaves the slot red", async () => {
    const s = setup({
      hub: fakeHub({
        listCalendarEvents: vi.fn(async () => [redEvent()]),
        updateAppointmentStatus: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))),
      }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(appt(s.state()).confirmation?.state).toBe("attention");
    expect(appt(s.state()).status).toBe("agendado");
  });

  it("registering attended / no_show does not touch the confirmation", async () => {
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [redEvent()]) }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(appt(s.state()).confirmation?.state).toBe("attention");
  });

  it("only the confirmed appointment flips, and a write answered for another clinic is dropped", () => {
    const items: Appt[] = [
      { id: "a", date: "2026-09-29", day: 2, start: 540, dur: 30, status: "agendado", appointmentId: "ap-a", confirmation: { state: "attention", count: 0 } },
      { id: "b", date: "2026-09-29", day: 2, start: 600, dur: 30, status: "agendado", appointmentId: "ap-b", confirmation: { state: "attention", count: 0 } },
    ];
    let st = agendaReducer(INITIAL_AGENDA_STATE, { type: "session_resolved", session: session() });
    st = agendaReducer(st, { type: "range_started", generation: 1 });
    st = agendaReducer(st, scoped(TENANT_A, { type: "range_loaded", generation: 1, items }));
    st = agendaReducer(st, scoped(TENANT_A, { type: "confirmation_recorded", appointmentId: "ap-a" }));
    expect(st.items?.map((i) => i.confirmation?.state)).toEqual(["confirmed", "attention"]);
    const other = agendaReducer(st, scoped(TENANT_B, { type: "confirmation_recorded", appointmentId: "ap-b" }));
    expect(other.items?.[1].confirmation?.state).toBe("attention");
  });
});

describe("release", () => {
  const paid = () => redEvent({ deposit: { status: "confirmado_pago", amount_cents: 4500 } });
  const opened = async (hubOver: Partial<AgendaHub> = {}, ev: CalendarEventWire = redEvent()) => {
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [ev]), ...hubOver }) });
    await s.controller.start();
    s.controller.select(appt(s.state()));
    await s.controller.openRelease(appt(s.state()));
    return s;
  };
  const call = (s: Awaited<ReturnType<typeof opened>>) => (s.hub.releaseAppointment as ReturnType<typeof vi.fn>).mock.calls[0];

  it("does not open for a Google-only slot or a terminal appointment", async () => {
    const g = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [event("g1", 29, 9, null)]) }) });
    await g.controller.start();
    await g.controller.openRelease(appt(g.state(), "hub-g1"));
    expect(g.state().modal).toBeNull();

    const t = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [redEvent({ status: "cancelled" })]) }) });
    await t.controller.start();
    await t.controller.openRelease(appt(t.state()));
    expect(t.state().modal).toBeNull();
  });

  it("opening reads the 24 h window of the patient notice (cancel-preview)", async () => {
    const s = await opened();
    expect(s.hub.getCancelPreview).toHaveBeenCalledWith(expect.anything(), "appt-e1");
    expect(s.state().check.status).toBe("loaded");
  });

  it("unpaid: sends the three flags false, closes card and drawer, toasts the notice and refetches", async () => {
    const s = await opened();
    await s.controller.confirmRelease(gate);
    await flush();
    expect(call(s)[2]).toEqual({ acknowledge_retention: false, release_confirmed: false, notify_outside_window: false });
    expect(s.state().modal).toBeNull();
    expect(s.state().selectedId).toBeNull();
    expect(s.state().knownStatus["appt-e1"]).toBe("cancelled");
    expect(s.state().toast?.tone).toBe("success");
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("outside the 24 h window the paid notice is sent only when the clinic opted in", async () => {
    const outside = { getCancelPreview: vi.fn(async () => preview({ inside_window: false })) };
    const off = await opened(outside);
    await off.controller.confirmRelease({ acknowledged: false, optIn: false });
    expect(call(off)[2].notify_outside_window).toBe(false);
    const on = await opened({ getCancelPreview: vi.fn(async () => preview({ inside_window: false })) });
    await on.controller.confirmRelease({ acknowledged: false, optIn: true });
    expect(call(on)[2].notify_outside_window).toBe(true);
  });

  it("sends nothing while the 24 h verdict is still loading", async () => {
    const p = deferred<ReturnType<typeof preview>>();
    const s = setup({ hub: fakeHub({ getCancelPreview: vi.fn(() => p.promise) }) });
    await s.controller.start();
    void s.controller.openRelease(appt(s.state()));
    await s.controller.confirmRelease(gate);
    expect(s.hub.releaseAppointment).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({ type: "release", pending: false });
  });

  it("paid: nothing is sent until the retention is acknowledged", async () => {
    const s = await opened({}, paid());
    await s.controller.confirmRelease(gate);
    expect(s.hub.releaseAppointment).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({ type: "release", pending: false });
    await s.controller.confirmRelease({ acknowledged: true, optIn: false });
    expect(call(s)[2].acknowledge_retention).toBe(true);
    expect(s.state().modal).toBeNull();
  });

  it("a backend 'retention acknowledgement required' refusal shows ITS text, makes the step mandatory and sends nothing more without it", async () => {
    const refuse = vi.fn(async () => Promise.reject(new HubApiError(409, "O sinal pago será retido.", RELEASE_ACK_REQUIRED_CODE)));
    const s = await opened({ releaseAppointment: refuse });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({
      type: "release", pending: false, forceAck: true, error: { kind: "ack_required", message: "O sinal pago será retido." },
    });
    expect(appt(s.state()).confirmation?.state).toBe("attention");
    await s.controller.confirmRelease(gate);
    expect(refuse).toHaveBeenCalledTimes(1);
  });

  it("'the patient just confirmed': explained, the slot refreshes, and only a second click sends release_confirmed", async () => {
    const refuse = vi
      .fn()
      .mockRejectedValueOnce(new HubApiError(409, "confirmou", RELEASE_ALREADY_CONFIRMED_CODE))
      .mockResolvedValueOnce({ ...wire("appt-e1", "cancelled"), patient_notice: "whatsapp_queued" });
    const s = await opened({ releaseAppointment: refuse });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({ type: "release", pending: false, confirmedSeen: true, error: { kind: "already_confirmed" } });
    expect(refuse.mock.calls[0][2].release_confirmed).toBe(false);
    await s.controller.confirmRelease(gate);
    expect(refuse.mock.calls[1][2].release_confirmed).toBe(true);
    expect(s.state().modal).toBeNull();
  });

  it("a failure shows an error and keeps everything as it was", async () => {
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))) });
    await s.controller.confirmRelease(gate);
    await flush();
    expect(s.state().modal).toMatchObject({ type: "release", pending: false, error: { kind: "failed", message: RELEASE_FAILED } });
    expect(s.state().selectedId).toBe("hub-e1");
    expect(s.state().knownStatus).toEqual({});
    expect(s.state().toast).toBeNull();
    expect(appt(s.state()).confirmation?.state).toBe("attention");
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(1);
  });

  it("Google unavailable (502): says so, nothing changed, safe to retry", async () => {
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(502, "x", "calendar_unavailable"))) });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({ pending: false, error: { kind: "failed", message: RELEASE_UNAVAILABLE } });
    expect(s.state().selectedId).toBe("hub-e1");
  });

  it("'not live' means already cancelled/released: explained, not retried", async () => {
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(409, "gone", RELEASE_NOT_LIVE_CODE))) });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({ type: "release", error: { kind: "already_done" } });
    expect(s.state().knownStatus["appt-e1"]).toBe("cancelled");
  });

  it("any other 409 is a failure, not 'already done'", async () => {
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(409, "x", "calendar_unresolved"))) });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({ error: { kind: "failed", message: RELEASE_FAILED } });
    expect(s.state().knownStatus).toEqual({});
  });

  it("Google disconnected is named, not a generic failure", async () => {
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(422, "Google Calendar not connected."))) });
    await s.controller.confirmRelease(gate);
    expect(s.state().modal).toMatchObject({ error: { kind: "failed", message: CALENDAR_NOT_CONNECTED_WRITE } });
  });

  it("a double click sends once", async () => {
    const d = deferred<Awaited<ReturnType<AgendaHub["releaseAppointment"]>>>();
    const s = await opened({ releaseAppointment: vi.fn(() => d.promise) });
    const first = s.controller.confirmRelease(gate);
    await s.controller.confirmRelease(gate);
    d.resolve({ ...wire("appt-e1", "cancelled") });
    await first;
    expect(s.hub.releaseAppointment).toHaveBeenCalledTimes(1);
  });

  it("tells the clinic how the patient notice went, and never claims one that did not go out", () => {
    for (const notice of ["whatsapp_outside_window", "no_channel", "queue_unavailable", "notice_failed"]) {
      expect(releasedToast({ patient_notice: notice }).tone, notice).toBe("danger");
    }
    expect(releasedToast({ patient_notice: "whatsapp_queued" })).toMatchObject({ tone: "success" });
    expect(releasedToast({ patient_notice: "portal_chat" }).message).toContain("Portal");
    expect(releasedToast({ patient_notice: "portal_chat_email" }).message).toContain("e-mail");
    const unknown = releasedToast({});
    expect(unknown.tone).toBe("success");
    expect(unknown.message).not.toMatch(/avisando|avisou|aviso ficou/);
    expect(releasedToast({ patient_notice: "something_new" }).tone).toBe("success");
  });
});

describe("message to the patient", () => {
  const opened = async (hubOver: Partial<AgendaHub> = {}) => {
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [redEvent()]), ...hubOver }) });
    await s.controller.start();
    s.controller.openMessage(appt(s.state()));
    return s;
  };

  it("sends the trimmed text, closes the sheet and confirms by delivery", async () => {
    const s = await opened();
    await s.controller.sendMessage("  Olá, tudo bem?  ", false);
    expect(s.hub.messageAppointment).toHaveBeenCalledWith(expect.anything(), "appt-e1", { text: "Olá, tudo bem?", notify_outside_window: false });
    expect(s.state().modal).toBeNull();
    expect(s.state().toast?.tone).toBe("success");
  });

  it("an empty message sends nothing and keeps the sheet", async () => {
    const s = await opened();
    await s.controller.sendMessage("   ", false);
    expect(s.hub.messageAppointment).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({ type: "message", pending: false });
  });

  it("outside the 24 h window: keeps the sheet, carries the cost and the wa.me link, and only a second send with the opt-in goes paid", async () => {
    const refuse = vi
      .fn()
      .mockRejectedValueOnce(
        new HubApiError(409, "fora da janela", MESSAGE_OUTSIDE_WINDOW_CODE, {
          code: MESSAGE_OUTSIDE_WINDOW_CODE, message: "fora da janela", template_cost_brl: "R$ 0,35", whatsapp_link: "https://wa.me/5511999990000",
        }),
      )
      .mockResolvedValueOnce({ delivery: "whatsapp_template", email_nudge: null, message_id: null });
    const s = await opened({ messageAppointment: refuse });
    await s.controller.sendMessage("Oi", false);
    expect(s.state().modal).toMatchObject({
      type: "message", pending: false, error: { kind: "outside_window", costBrl: "R$ 0,35", waLink: "https://wa.me/5511999990000" },
    });
    expect(refuse.mock.calls[0][2].notify_outside_window).toBe(false);
    await s.controller.sendMessage("Oi", true);
    expect(refuse.mock.calls[1][2].notify_outside_window).toBe(true);
    expect(s.state().modal).toBeNull();
  });

  it("never trusts a link that is not a wa.me address", async () => {
    const refuse = vi.fn(async () =>
      Promise.reject(new HubApiError(409, "x", MESSAGE_OUTSIDE_WINDOW_CODE, { template_cost_brl: "", whatsapp_link: "javascript:alert(1)" })),
    );
    const s = await opened({ messageAppointment: refuse });
    await s.controller.sendMessage("Oi", false);
    expect(s.state().modal).toMatchObject({ error: { kind: "outside_window", waLink: null } });
  });

  it("no channel and delivery failures keep the sheet open with an honest error", async () => {
    const none = await opened({ messageAppointment: vi.fn(async () => Promise.reject(new HubApiError(422, "x", MESSAGE_NO_CHANNEL_CODE))) });
    await none.controller.sendMessage("Oi", false);
    expect(none.state().modal).toMatchObject({ type: "message", pending: false, error: { kind: "no_channel" } });

    const down = await opened({ messageAppointment: vi.fn(async () => Promise.reject(new HubApiError(502, "x", "delivery_failed"))) });
    await down.controller.sendMessage("Oi", false);
    expect(down.state().modal).toMatchObject({ type: "message", pending: false, error: { kind: "failed", message: MESSAGE_FAILED } });
    expect(down.state().toast).toBeNull();
  });

  it("the success toast says how it was delivered and never invents an e-mail", () => {
    expect(messageSentToast({ delivery: "whatsapp_template" }).message).toContain("oficial");
    expect(messageSentToast({ delivery: "portal_chat", email_nudge: "sent" }).message).toContain("e-mail avisou");
    expect(messageSentToast({ delivery: "portal_chat", email_nudge: "no_email" }).message).toContain("não tem e-mail");
    const plain = messageSentToast({ delivery: "portal_chat", email_nudge: "not_sent" }).message;
    expect(plain).toContain("Portal");
    expect(plain).not.toContain("e-mail avisou");
    expect(messageSentToast({}).tone).toBe("success");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run lib/agenda/__tests__/confirmation.test.ts lib/agenda/__tests__/screen.test.ts`
Expected: FAIL (`applyStaffConfirmation`/`openRelease`/`releaseAppointment` missing; `tsc` errors on `fakeHub`).

- [ ] **Step 3: `applyStaffConfirmation` in `lib/agenda/confirmation.ts`**

Append:

```ts
// ---------------------------------------------------------------------------
// Live flip
// ---------------------------------------------------------------------------

const NOT_CONFIRMABLE: readonly ApptStatus[] = ["cancelado", "compareceu", "faltou", "bloqueio"];

/**
 * What a slot looks like right after the clinic confirmed it, before the next read
 * says so (red -> green "live"). Counts ONE confirmation (spec 4.1: a staff
 * confirmation counts as one), capped at two. Terminal statuses and blocks come
 * back untouched; a slot with no confirmation data (older backend) only gets the
 * status, never a confirmation key. The refetch that follows always wins.
 */
export function applyStaffConfirmation(a: Appt): Appt {
  if (NOT_CONFIRMABLE.includes(a.status)) return a;
  if (!a.confirmation) return { ...a, status: "confirmou" };
  const count: 1 | 2 = a.confirmation.count >= 2 ? 2 : 1;
  return { ...a, status: "confirmou", confirmation: { state: count === 2 ? "confirmed_twice" : "confirmed", count } };
}
```

- [ ] **Step 4: `lib/agenda/screen.ts`**

(a) Imports: `import { applyStaffConfirmation } from "./confirmation";`, `import { MESSAGE_FAILED, RELEASE_FAILED, RELEASE_UNAVAILABLE, buildReleaseBody, validateMessage } from "./release";`, `import type { ReleaseConfirm } from "./release";`, and from `./secretaria-hub-agenda` the values `MESSAGE_NO_CHANNEL_CODE, MESSAGE_OUTSIDE_WINDOW_CODE, RELEASE_ACK_REQUIRED_CODE, RELEASE_ALREADY_CONFIRMED_CODE, RELEASE_NOT_LIVE_CODE` plus the types `AppointmentMessagePayload, AppointmentMessageWire, AppointmentReleasePayload, AppointmentReleaseWire` (add to the existing type import). Make sure `canActOn` is in the existing `./modal-forms` import list (and `cancelNoticeState` — it is already used by `confirmCancel`).

(b) Parse the alias. In `parseAgendaParams` replace `const consultaId = q.get("consultaId");` with:

```ts
  // `consulta` is the id R4's warning e-mail links with (…/agenda?consulta=<appointment_id>).
  const consultaId = q.get("consultaId") ?? q.get("consulta");
```

(c) After `export type CancelModalError = …;` add:

```ts
export type ReleaseModalError =
  | { kind: "already_done" }
  /** The backend's own retention text (pt-BR): shown as is. */
  | { kind: "ack_required"; message: string }
  | { kind: "already_confirmed" }
  | { kind: "failed"; message: string };

export type MessageModalError =
  | { kind: "failed"; message: string }
  | { kind: "no_channel" }
  | { kind: "outside_window"; costBrl: string; waLink: string | null };
```

and in the `AgendaModal` union, before the final `| null;`, add:

```ts
  | { type: "release"; appt: Appt; pending: boolean; error: ReleaseModalError | null; forceAck: boolean; confirmedSeen: boolean }
  | { type: "message"; appt: Appt; pending: boolean; error: MessageModalError | null }
```

(d) In `TenantScopedAction`: change the `modal_error` member to `| { type: "modal_error"; error: string | CancelModalError | ReleaseModalError | MessageModalError | null }` and add after the `known_status` member:

```ts
  /** The clinic just confirmed this appointment (PATCH confirmed answered): flip it live, before the refetch. */
  | { type: "confirmation_recorded"; appointmentId: string }
```

(e) In `applyScoped`, replace

```ts
    case "modal_error":
      return withModal(state, { error: action.error });
```

with

```ts
    case "modal_error": {
      const next = withModal(state, { error: action.error });
      const m = next.modal;
      const kind = typeof action.error === "object" && action.error !== null ? action.error.kind : null;
      if (m?.type === "release") {
        // The backend asked for something the read did not foresee: the step becomes mandatory.
        if (kind === "ack_required") return { ...next, modal: { ...m, forceAck: true } };
        if (kind === "already_confirmed") return { ...next, modal: { ...m, confirmedSeen: true } };
      }
      return next;
    }
```

and after the `known_status` case add:

```ts
    case "confirmation_recorded":
      if (!state.items) return state;
      return {
        ...state,
        items: state.items.map((a) => (a.appointmentId === action.appointmentId ? applyStaffConfirmation(a) : a)),
      };
```

(f) After `cancelledToast`/`rescheduledToast` add:

```ts
const RELEASE_NOT_NOTIFIED: Readonly<Record<string, string>> = {
  whatsapp_outside_window:
    "o paciente está fora da janela de 24 h do WhatsApp e não foi avisado. Escreva para ele pelo seu próprio WhatsApp.",
  no_channel: "não há um canal para avisar o paciente.",
  queue_unavailable: "o aviso ao paciente não pôde ser enviado agora. Fale com ele pelo chat.",
  notice_failed: "o aviso ao paciente não pôde ser enviado. Fale com ele pelo chat.",
};

/**
 * What the clinic is told after a release went through, from R4's `patient_notice`.
 * A notice that did not go out is a danger toast; an absent or unknown value claims nothing.
 */
export function releasedToast(res: { patient_notice?: string | null }): Toast {
  const notice = res.patient_notice ?? null;
  const base = "Horário liberado e consulta cancelada";
  if (notice !== null && Object.prototype.hasOwnProperty.call(RELEASE_NOT_NOTIFIED, notice)) {
    return { tone: "danger", message: `${base}, mas ${RELEASE_NOT_NOTIFIED[notice]}` };
  }
  if (notice === "whatsapp_queued") return { tone: "success", message: `${base}. A secretarIA está avisando o paciente pelo WhatsApp.` };
  if (notice === "portal_chat") return { tone: "success", message: `${base}. O aviso ficou na conversa do paciente no Portal.` };
  if (notice === "portal_chat_email") {
    return { tone: "success", message: `${base}. O aviso ficou na conversa do paciente no Portal e seguiu por e-mail.` };
  }
  return { tone: "success", message: `${base}.` };
}

/** What the clinic is told after a patient message went out, from R4's `delivery` / `email_nudge`. */
export function messageSentToast(res: AppointmentMessageWire): Toast {
  if (res.delivery === "whatsapp_template") return { tone: "success", message: "Mensagem oficial enviada ao paciente pelo WhatsApp." };
  if (res.delivery === "portal_chat") {
    const base = "Mensagem deixada na conversa do paciente no Portal.";
    if (res.email_nudge === "sent") return { tone: "success", message: `${base} Um e-mail avisou o paciente.` };
    if (res.email_nudge === "no_email") return { tone: "success", message: `${base} Ele não tem e-mail cadastrado para ser avisado.` };
    return { tone: "success", message: base };
  }
  return { tone: "success", message: "Mensagem enviada ao paciente." };
}

/** The wa.me link and cost of an outside-window refusal. The link is untrusted data going into an href: only wa.me passes. */
function outsideWindowFacts(e: HubApiError): { costBrl: string; waLink: string | null } {
  const d = e.detail && typeof e.detail === "object" ? (e.detail as Record<string, unknown>) : {};
  const link = typeof d.whatsapp_link === "string" ? d.whatsapp_link : "";
  return {
    costBrl: typeof d.template_cost_brl === "string" ? d.template_cost_brl : "",
    waLink: link.startsWith("https://wa.me/") ? link : null,
  };
}
```

(g) In `AgendaHub`, after `updateAppointmentStatus`:

```ts
  releaseAppointment: (
    s: Session,
    appointmentId: string,
    payload: AppointmentReleasePayload,
  ) => Promise<AppointmentReleaseWire>;
  messageAppointment: (
    s: Session,
    appointmentId: string,
    payload: AppointmentMessagePayload,
  ) => Promise<AppointmentMessageWire>;
```

(h) In `setStatus`, replace `      own({ type: "status_done", error: null });` with:

```ts
      if (status === "confirmed") own({ type: "confirmation_recorded", appointmentId });
      own({ type: "status_done", error: null });
```

(i) Add the four controller methods after `setStatus` (before `return {`):

```ts
  /** Opens the release card and reads the 24 h window of the patient notice (same lookup as the cancel card). */
  function openRelease(appt: Appt): Promise<void> {
    if (!canWrite(getState()) || !appt.appointmentId || !canActOn(appt)) return Promise.resolve();
    dispatch({
      type: "open_modal",
      modal: { type: "release", appt, pending: false, error: null, forceAck: false, confirmedSeen: false },
    });
    return loadCheck(appt.appointmentId);
  }

  /**
   * "Liberar horário". Nothing is sent while the Pix-retention gate is open (a PAID
   * deposit, or the backend asked for it) or while the 24 h verdict is still being
   * read. Any failure keeps the card open with its error and leaves the appointment
   * exactly as it was.
   */
  async function confirmRelease(c: ReleaseConfirm): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "release" || m.pending || !m.appt.appointmentId) return;
    const notice = cancelNoticeState(m.appt, getState().check);
    if (notice.kind === "loading") return;
    const body = buildReleaseBody(m.appt, {
      acknowledged: c.acknowledged,
      forceAck: m.forceAck,
      confirmedSeen: m.confirmedSeen,
      notifyOutsideWindow: notice.kind === "whatsapp_outside_window" && c.optIn,
    });
    if (!body) return; // the acknowledgement step is not done: nothing is sent
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("release");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      const res = await hub.releaseAppointment(s, appointmentId, body);
      own({ type: "known_status", appointmentId, status: "cancelled" });
      own({ type: "cancel_done" });
      own({ type: "toast", toast: releasedToast(res ?? {}) });
      if (getState().tenantId === s.tenantId) syncUrl();
      refreshAfter(s.tenantId);
    } catch (e) {
      console.error("agenda: failed to release appointment", errorFacts(e));
      if (isTenantSwitched(e)) {
        await fresh();
      } else if (e instanceof HubApiError && e.status === 409 && e.code === RELEASE_ACK_REQUIRED_CODE) {
        own({ type: "modal_error", error: { kind: "ack_required", message: e.message } });
      } else if (e instanceof HubApiError && e.status === 409 && e.code === RELEASE_ALREADY_CONFIRMED_CODE) {
        own({ type: "modal_error", error: { kind: "already_confirmed" } });
        refreshAfter(s.tenantId); // the read is stale: the slot is no longer red
      } else if (e instanceof HubApiError && e.status === 409 && e.code === RELEASE_NOT_LIVE_CODE) {
        own({ type: "known_status", appointmentId, status: "cancelled" });
        own({ type: "modal_error", error: { kind: "already_done" } });
        refreshAfter(s.tenantId);
      } else {
        const message =
          isCalendarNotConnected(e) ? CALENDAR_NOT_CONNECTED_WRITE
          : e instanceof HubApiError && e.status === 502 ? RELEASE_UNAVAILABLE
          : RELEASE_FAILED;
        own({ type: "modal_error", error: { kind: "failed", message } });
      }
    } finally {
      own({ type: "modal_pending", pending: false });
    }
  }

  function openMessage(appt: Appt): void {
    if (!canWrite(getState()) || !appt.appointmentId || !canActOn(appt)) return;
    dispatch({ type: "open_modal", modal: { type: "message", appt, pending: false, error: null } });
  }

  async function sendMessage(text: string, notifyOutsideWindow: boolean): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "message" || m.pending || !m.appt.appointmentId) return;
    const checked = validateMessage(text);
    if (!checked.ok) return; // the sheet keeps its send button disabled; this is the backstop
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("message");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      const res = await hub.messageAppointment(s, appointmentId, {
        text: checked.text,
        notify_outside_window: notifyOutsideWindow,
      });
      own({ type: "modal_done" });
      own({ type: "toast", toast: messageSentToast(res ?? {}) });
    } catch (e) {
      console.error("agenda: failed to message patient", errorFacts(e));
      if (isTenantSwitched(e)) {
        await fresh();
      } else if (e instanceof HubApiError && e.status === 409 && e.code === MESSAGE_OUTSIDE_WINDOW_CODE) {
        own({ type: "modal_error", error: { kind: "outside_window", ...outsideWindowFacts(e) } });
      } else if (e instanceof HubApiError && e.status === 422 && e.code === MESSAGE_NO_CHANNEL_CODE) {
        own({ type: "modal_error", error: { kind: "no_channel" } });
      } else {
        own({ type: "modal_error", error: { kind: "failed", message: MESSAGE_FAILED } });
      }
    } finally {
      own({ type: "modal_pending", pending: false });
    }
  }
```

and add `openRelease, confirmRelease, openMessage, sendMessage,` to the controller's returned object, after `setStatus,`.

- [ ] **Step 5: Run tests and type check**

Run: `npx vitest run lib/agenda` — Expected: PASS.
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors. (If `tsc` flags the `AgendaModal` union in `AgendaView.tsx`, it is only read there by `modal?.type === "…"` checks and Task 6 adds the two new cases.)

- [ ] **Step 6: Commit**

```bash
git add lib/agenda/confirmation.ts lib/agenda/screen.ts lib/agenda/__tests__/confirmation.test.ts lib/agenda/__tests__/screen.test.ts
git commit -m "feat(agenda): controller for release, patient message, deep link and the live confirm flip (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 6: ReleaseCard, MessageModal and the AgendaView/AgendaScreen wiring

**Files:**
- Create: `components/agenda/ReleaseCard.tsx`, `components/agenda/MessageModal.tsx`
- Modify: `components/agenda/agenda-modals.css` (append), `components/agenda/AgendaView.tsx`, `components/agenda/AgendaScreen.tsx`
- Test: `components/agenda/__tests__/release-card.test.tsx`, `components/agenda/__tests__/message-modal.test.tsx`, `components/agenda/__tests__/agenda-view.test.tsx` (append)

**Interfaces:**
- Consumes: Task 4 copy/rules, Task 5 modal types and controller methods, existing `Sheet`, `Button`, `TextArea`, `Icon`, `CancelNoticeBlock` (exported by `CancelCard.tsx`: the same 24 h notice + paid opt-in the cancel card shows), `cancelNoticeState`, `WindowCheck`.
- Produces: `ReleaseCardView(props: ReleaseCardViewProps)` (stateless) and `ReleaseCard(props: ReleaseCardProps)` (owns the two ticks; resets on every open); `MessageModalView`/`MessageModal` (owns text and opt-in; pre-fills `messageSuggestion(appt)` on every open); new **optional** `AgendaViewProps`: `onOpenRelease?: (appt: Appt) => void`, `onConfirmRelease?: (c: ReleaseConfirm) => void`, `onOpenMessage?: (appt: Appt) => void`, `onSendMessage?: (text: string, notifyOutsideWindow: boolean) => void`.

```ts
export type ReleaseCardViewProps = {
  open: boolean; appt: Appt | null; check: WindowCheck; forceAck: boolean; confirmedSeen: boolean;
  pending: boolean; error: ReleaseModalError | null;
  ack: boolean; onAck: (next: boolean) => void; optIn: boolean; onOptIn: (next: boolean) => void;
  onClose: () => void; onConfirm: (c: ReleaseConfirm) => void;
};
export type MessageModalViewProps = {
  open: boolean; appt: Appt | null; pending: boolean; error: MessageModalError | null;
  text: string; onText: (next: string) => void; optIn: boolean; onOptIn: (next: boolean) => void;
  onClose: () => void; onSend: (text: string, notifyOutsideWindow: boolean) => void;
};
```

- [ ] **Step 1: Write the failing tests**

Create `components/agenda/__tests__/release-card.test.tsx`:

```tsx
// ReleaseCard — the Pix-retention acknowledgement gate, the 24 h notice and the failure states (TASK-032 R5).
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { ReleaseCardView, type ReleaseCardViewProps } from "../ReleaseCard";
import {
  RELEASE_ACK_REQUIRED, RELEASE_ALREADY_CONFIRMED, RELEASE_ALREADY_DONE, RELEASE_COPY, RELEASE_FAILED,
} from "../../../lib/agenda/release";
import type { WindowCheck } from "../../../lib/agenda/modal-forms";
import type { Appt } from "../../../lib/agenda/types";

const noop = () => {};
const base: Appt = {
  id: "hub-1", date: "2026-09-29", day: 2, start: 540, dur: 30, patient: "Maria Souza", type: "Consulta",
  status: "agendado", appointmentId: "ap-1", confirmation: { state: "attention", count: 0 },
};
const paid: Appt = { ...base, deposit: { status: "confirmado_pago", amountCents: 4500 } };
const inside: WindowCheck = {
  status: "loaded",
  preview: { inside_window: true, professional_name: "Dra. Ana", template_cost_brl: "R$ 0,35", cost_is_estimate: false, whatsapp_link: "https://wa.me/5511999990000" },
};
const outside: WindowCheck = {
  status: "loaded",
  preview: { inside_window: false, professional_name: "Dra. Ana", template_cost_brl: "R$ 0,35", cost_is_estimate: false, whatsapp_link: "https://wa.me/5511999990000" },
};
const render = (over: Partial<ReleaseCardViewProps> = {}) =>
  renderToStaticMarkup(
    <ReleaseCardView
      open appt={base} check={inside} forceAck={false} confirmedSeen={false} pending={false} error={null}
      ack={false} onAck={noop} optIn={false} onOptIn={noop} onClose={noop} onConfirm={noop} {...over}
    />,
  );
const tag = (html: string, text: string): string => {
  const m = new RegExp(`<button[^>]*>(?:(?!</button>).)*${text}(?:(?!</button>).)*</button>`).exec(html);
  if (!m) throw new Error(`no button "${text}"`);
  return m[0];
};

describe("ReleaseCard", () => {
  it("unpaid: explains the effect, shows the patient notice and the confirm button is enabled without any acknowledgement", () => {
    const html = render();
    expect(html).toContain(RELEASE_COPY.intro);
    expect(html).toContain(RELEASE_COPY.noticeTitle);
    expect(html).not.toContain(RELEASE_COPY.ackLabel);
    expect(tag(html, RELEASE_COPY.confirm)).not.toContain("disabled");
  });

  it("paid: shows the retention warning and a LABELLED checkbox; confirm stays disabled until it is ticked", () => {
    const html = render({ appt: paid });
    expect(html).toContain(RELEASE_COPY.ackTitle);
    expect(html).toContain(RELEASE_COPY.ackBody);
    const id = /<input id="([^"]+)" type="checkbox"/.exec(html)![1];
    expect(html).toContain(`<label for="${id}">${RELEASE_COPY.ackLabel}</label>`);
    expect(tag(html, RELEASE_COPY.confirm)).toContain('disabled=""');
    expect(tag(render({ appt: paid, ack: true }), RELEASE_COPY.confirm)).not.toContain("disabled");
    expect(render({ appt: paid, ack: true })).toContain('checked=""');
  });

  it("a backend-forced step (forceAck) shows the BACKEND's retention text and behaves like a paid deposit", () => {
    const html = render({ forceAck: true, error: { kind: "ack_required", message: "O sinal pago será retido (R$ 45,00)." } });
    expect(html).toContain(RELEASE_COPY.ackLabel);
    expect(html).toContain("O sinal pago será retido (R$ 45,00).");
    expect(tag(html, RELEASE_COPY.confirm)).toContain('disabled=""');
  });

  it("an empty backend retention text falls back to the local one", () => {
    expect(render({ forceAck: true, error: { kind: "ack_required", message: "" } })).toContain(RELEASE_ACK_REQUIRED);
  });

  it("outside the 24 h window the paid-notice opt-in is offered, and the button says what it will do", () => {
    const off = render({ check: outside });
    expect(off).toContain('type="checkbox"');
    expect(tag(off, RELEASE_COPY.confirm)).not.toContain("disabled");
    expect(tag(render({ check: outside, optIn: true }), RELEASE_COPY.confirmPaidNotice)).not.toContain("disabled");
  });

  it("while the 24 h verdict loads the confirm button is disabled and says so", () => {
    expect(tag(render({ check: { status: "loading" } }), RELEASE_COPY.checking)).toContain("disabled");
  });

  it("'already confirmed': explained, and the button becomes 'Liberar mesmo assim' (a second, deliberate click)", () => {
    const html = render({ error: { kind: "already_confirmed" }, confirmedSeen: true });
    expect(html).toContain(RELEASE_ALREADY_CONFIRMED);
    expect(tag(html, RELEASE_COPY.confirmAnyway)).not.toContain("disabled");
  });

  it("failure: the error is announced and the card stays usable; pending locks it", () => {
    const failed = render({ error: { kind: "failed", message: RELEASE_FAILED } });
    expect(failed).toContain(RELEASE_FAILED);
    expect(failed).toContain('role="alert"');
    expect(tag(failed, RELEASE_COPY.confirm)).not.toContain("disabled");

    const pending = render({ pending: true });
    expect(tag(pending, RELEASE_COPY.pending)).toContain("disabled");
    expect(tag(pending, RELEASE_COPY.pending)).toContain('aria-busy="true"');
    expect(tag(pending, RELEASE_COPY.back)).toContain("disabled");
  });

  it("already released/cancelled: explained, and confirming is blocked", () => {
    const html = render({ error: { kind: "already_done" } });
    expect(html).toContain(RELEASE_ALREADY_DONE);
    expect(tag(html, RELEASE_COPY.confirm)).toContain("disabled");
  });

  it("names the appointment so the clinic knows what is being released", () => {
    const html = render();
    expect(html).toContain("Maria Souza");
    expect(html).toContain("Terça, 29/09 · 09:00–09:30");
  });

  it("renders no body for a null appointment", () => {
    expect(render({ appt: null })).not.toContain(RELEASE_COPY.intro);
  });
});
```

Create `components/agenda/__tests__/message-modal.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { MessageModalView, type MessageModalViewProps } from "../MessageModal";
import { MESSAGE_COPY, MESSAGE_FAILED, MESSAGE_MAX, MESSAGE_NO_CHANNEL } from "../../../lib/agenda/release";
import type { Appt } from "../../../lib/agenda/types";

const noop = () => {};
const appt: Appt = { id: "hub-1", date: "2026-09-29", day: 2, start: 540, dur: 30, patient: "Maria Souza", status: "agendado", appointmentId: "ap-1" };
const render = (over: Partial<MessageModalViewProps> = {}) =>
  renderToStaticMarkup(
    <MessageModalView open appt={appt} pending={false} error={null} text="Olá" onText={noop} optIn={false} onOptIn={noop} onClose={noop} onSend={noop} {...over} />,
  );
const send = (html: string) => /<button[^>]*>(?:(?!<\/button>).)*Enviar mensagem(?:(?!<\/button>).)*<\/button>/.exec(html)![0];

describe("MessageModal", () => {
  it("has a labelled textarea, the channel hint and the character limit", () => {
    const html = render();
    expect(html).toContain(`>${MESSAGE_COPY.label}</label>`);
    expect(html).toContain(MESSAGE_COPY.hint);
    expect(html).toContain(`maxLength="${MESSAGE_MAX}"`);
  });

  it("send is disabled for an empty/blank text, enabled otherwise", () => {
    expect(send(render({ text: "   " }))).toContain("disabled");
    expect(send(render({ text: "Olá" }))).not.toContain("disabled");
  });

  it("outside the 24 h window: explains, offers the paid message with its cost and the free wa.me link; send waits for the opt-in", () => {
    const error = { kind: "outside_window", costBrl: "R$ 0,35", waLink: "https://wa.me/5511999990000" } as const;
    const html = render({ error });
    expect(html).toContain(MESSAGE_COPY.outsideTitle);
    expect(html).toContain("R$ 0,35");
    expect(html).toContain('href="https://wa.me/5511999990000"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(send(html)).toContain("disabled");
    expect(send(render({ error, optIn: true }))).not.toContain("disabled");
  });

  it("outside the window without a usable link shows no link", () => {
    expect(render({ error: { kind: "outside_window", costBrl: "", waLink: null } })).not.toContain("href=");
  });

  it("no channel and failures are announced; the text is kept and the user can retry", () => {
    const none = render({ error: { kind: "no_channel" } });
    expect(none).toContain(MESSAGE_NO_CHANNEL);
    expect(none).toContain('role="alert"');
    const failed = render({ error: { kind: "failed", message: MESSAGE_FAILED }, text: "Olá" });
    expect(failed).toContain(MESSAGE_FAILED);
    expect(failed).toContain(">Olá</textarea>");
    expect(send(failed)).not.toContain("disabled");
  });

  it("pending locks the controls", () => {
    const pending = render({ pending: true });
    expect(pending).toMatch(/<textarea[^>]*disabled=""/);
    expect(pending).toMatch(/<button[^>]*disabled=""[^>]*>(?:(?!<\/button>).)*Enviando…/);
    expect(pending).toMatch(/<button[^>]*aria-busy="true"/);
  });
});
```

Append to `components/agenda/__tests__/agenda-view.test.tsx`:

```tsx
describe("AgendaView — release and message sheets", () => {
  const red: Appt = { ...ITEM, confirmation: { state: "attention", count: 0 } };
  const withModal = (modal: NonNullable<AgendaState["modal"]>): AgendaState => ({ ...loaded([red]), selectedId: red.id, modal });

  it("renders the release card for an open release modal", () => {
    const html = render(withModal({ type: "release", appt: red, pending: false, error: null, forceAck: false, confirmedSeen: false }));
    expect(html).toContain("O horário volta a ficar livre na agenda");
  });

  it("renders the message sheet for an open message modal", () => {
    const html = render(withModal({ type: "message", appt: red, pending: false, error: null }));
    expect(html).toContain("Mensagem para o paciente");
  });

  it("the drawer's red actions are enabled when the screen is writable and wired", () => {
    const html = render({ ...loaded([red]), selectedId: red.id }, { onOpenRelease: noop, onOpenMessage: noop });
    expect(html).toContain("Marcar como confirmado");
    expect(buttonTag(html, "Liberar horário")).not.toContain("disabled");
  });

  it("a caller that does not pass the new callbacks still renders (actions disabled)", () => {
    const html = render({ ...loaded([red]), selectedId: red.id });
    expect(buttonTag(html, "Liberar horário")).toContain("disabled");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/agenda/__tests__/release-card.test.tsx components/agenda/__tests__/message-modal.test.tsx components/agenda/__tests__/agenda-view.test.tsx`
Expected: FAIL (modules missing).

- [ ] **Step 3: CSS**

Append to `components/agenda/agenda-modals.css`:

```css
/* Release card + message sheet (TASK-032 R5). No motion on purpose. */
.agm-release-ack {
  display: flex; flex-direction: column; gap: var(--sp-2);
  padding: var(--sp-3); border: 2px solid var(--st-attn-bd); background: var(--st-attn-bg); border-radius: var(--radius-control);
}
.agm-release-ack-title { margin: 0; font-size: var(--fs-md); font-weight: 700; color: var(--st-attn-ink); }
.agm-release-ack p { margin: 0; color: var(--ink); }
.agm-release-outside { display: flex; flex-direction: column; gap: var(--sp-2); }
```

- [ ] **Step 4: `components/agenda/ReleaseCard.tsx`**

```tsx
"use client";

// ReleaseCard — "Liberar horário" (TASK-032 R5, spec 4.4). Frees the slot: the
// secretarIA deletes the Google event, cancels the appointment and tells the
// patient (with the option to book again).
//
// THE GATE: when the appointment's Pix deposit is PAID, releasing inside the
// refund window can make the clinic RETAIN the money. The card then shows the
// warning and a labelled checkbox, and the confirm button stays disabled until
// it is ticked — and the controller sends nothing without it either
// (lib/agenda/release.ts::buildReleaseBody). `forceAck` makes the same step
// mandatory when the backend asked for it although the read showed no deposit.
//
// The patient notice reuses the cancel card's 24 h block (CancelNoticeBlock): a
// WhatsApp patient outside the window is told only if the clinic opts in to the
// paid template. "The patient just confirmed" needs a second, deliberate click
// ("Liberar mesmo assim").
//
// Stateless view + a stateful shell (same split as CancelCard): the shell owns
// the ticks and resets them on every open, so a card never starts acknowledged.

import { useEffect, useId, useState } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { Icon } from "../icons";
import { CancelNoticeBlock } from "./CancelCard";
import { fmtRange } from "../../lib/agenda/types";
import type { Appt } from "../../lib/agenda/types";
import { dayLabelFromKey } from "../../lib/agenda/calendar-dates";
import { cancelNoticeState } from "../../lib/agenda/modal-forms";
import type { WindowCheck } from "../../lib/agenda/modal-forms";
import {
  RELEASE_ACK_REQUIRED,
  RELEASE_ALREADY_CONFIRMED,
  RELEASE_ALREADY_DONE,
  RELEASE_COPY,
  releaseConfirmLabel,
  releaseNeedsAck,
} from "../../lib/agenda/release";
import type { ReleaseConfirm } from "../../lib/agenda/release";
import type { ReleaseModalError } from "../../lib/agenda/screen";
import "./agenda-modals.css";

export type ReleaseCardViewProps = {
  open: boolean;
  appt: Appt | null;
  check: WindowCheck;
  forceAck: boolean;
  confirmedSeen: boolean;
  pending: boolean;
  error: ReleaseModalError | null;
  ack: boolean;
  onAck: (next: boolean) => void;
  optIn: boolean;
  onOptIn: (next: boolean) => void;
  onClose: () => void;
  onConfirm: (c: ReleaseConfirm) => void;
};

function errorText(error: ReleaseModalError): string {
  switch (error.kind) {
    case "already_done":
      return RELEASE_ALREADY_DONE;
    case "already_confirmed":
      return RELEASE_ALREADY_CONFIRMED;
    case "ack_required":
      return error.message || RELEASE_ACK_REQUIRED;
    case "failed":
      return error.message;
  }
}

export function ReleaseCardView(p: ReleaseCardViewProps) {
  const ackId = useId();
  const appt = p.appt;
  const needsAck = appt ? releaseNeedsAck(appt, p.forceAck) : false;
  const notice = appt ? cancelNoticeState(appt, p.check) : ({ kind: "loading" } as const);
  const checking = notice.kind === "loading";
  const blocked = p.pending || checking || p.error?.kind === "already_done" || (needsAck && !p.ack);
  const label = releaseConfirmLabel({
    pending: p.pending,
    checking,
    confirmedSeen: p.confirmedSeen,
    paidNotice: notice.kind === "whatsapp_outside_window" && p.optIn,
  });

  const footer = (
    <>
      <Button variant="ghost" onClick={p.onClose} disabled={p.pending}>
        {RELEASE_COPY.back}
      </Button>
      <Button
        variant="danger"
        icon="calendar"
        disabled={blocked}
        aria-busy={p.pending || undefined}
        onClick={!blocked ? () => p.onConfirm({ acknowledged: p.ack, optIn: p.optIn }) : undefined}
      >
        {label}
      </Button>
    </>
  );

  return (
    <Sheet open={p.open} onClose={p.onClose} title={RELEASE_COPY.title} footer={footer} className="agm-card" dismissible={!p.pending}>
      {appt && (
        <>
          <div>
            <div className="agm-title">{appt.patient || "Paciente"}</div>
            <div className="agm-sub">
              {dayLabelFromKey(appt.date)} · {fmtRange(appt.start, appt.dur)}
            </div>
          </div>
          <p className="agm-hint">{RELEASE_COPY.intro}</p>

          <section className="agm-section" aria-label={RELEASE_COPY.noticeTitle}>
            <h3 className="agm-section-title">{RELEASE_COPY.noticeTitle}</h3>
            <CancelNoticeBlock state={notice} optIn={p.optIn} onOptIn={p.onOptIn} disabled={p.pending} />
          </section>

          {needsAck && (
            <div className="agm-release-ack">
              <p className="agm-release-ack-title">{RELEASE_COPY.ackTitle}</p>
              <p>{RELEASE_COPY.ackBody}</p>
              <div className="agm-optin">
                <input
                  id={ackId}
                  type="checkbox"
                  checked={p.ack}
                  disabled={p.pending}
                  onChange={(e) => p.onAck(e.target.checked)}
                />
                <label htmlFor={ackId}>{RELEASE_COPY.ackLabel}</label>
              </div>
            </div>
          )}

          {p.error && (
            <p className="agm-note agm-note--danger" role="alert">
              <Icon name="alert" size={16} />
              <span>{errorText(p.error)}</span>
            </p>
          )}
        </>
      )}
    </Sheet>
  );
}

export type ReleaseCardProps = Omit<ReleaseCardViewProps, "ack" | "onAck" | "optIn" | "onOptIn">;

export function ReleaseCard(props: ReleaseCardProps) {
  const [ack, setAck] = useState(false);
  const [optIn, setOptIn] = useState(false);
  const apptId = props.appt?.id;
  // A fresh card never starts acknowledged, and the billed notice is always a deliberate act.
  useEffect(() => {
    if (!props.open) return;
    setAck(false);
    setOptIn(false);
  }, [props.open, apptId]);
  return <ReleaseCardView {...props} ack={ack} onAck={setAck} optIn={optIn} onOptIn={setOptIn} />;
}
```

- [ ] **Step 5: `components/agenda/MessageModal.tsx`**

```tsx
"use client";

// MessageModal — "Enviar mensagem ao paciente" (TASK-032 R5, spec 4.4): free text on
// the patient's own channel. The text is pre-filled with an opt-in suggestion built
// from the slot's own date/time (lib/agenda/release.ts::messageSuggestion) and is
// reset on every open. Send is disabled for a blank or over-long text; a failed
// send keeps the sheet open with the text intact so the clinic can retry.
//
// WhatsApp outside the 24 h window: the backend refuses a free message unless the
// clinic authorises the paid template. The sheet then explains it, shows the cost,
// offers the free wa.me link (clinic's own WhatsApp) and waits for the opt-in.

import { useEffect, useId, useState } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { TextArea } from "../primitives/Input";
import { Icon } from "../icons";
import type { Appt } from "../../lib/agenda/types";
import { MESSAGE_COPY, MESSAGE_MAX, MESSAGE_NO_CHANNEL, messageSuggestion, validateMessage } from "../../lib/agenda/release";
import type { MessageModalError } from "../../lib/agenda/screen";
import "./agenda-modals.css";

export type MessageModalViewProps = {
  open: boolean;
  appt: Appt | null;
  pending: boolean;
  error: MessageModalError | null;
  text: string;
  onText: (next: string) => void;
  optIn: boolean;
  onOptIn: (next: boolean) => void;
  onClose: () => void;
  onSend: (text: string, notifyOutsideWindow: boolean) => void;
};

export function MessageModalView(p: MessageModalViewProps) {
  const hintId = useId();
  const optInId = useId();
  const check = validateMessage(p.text);
  const outside = p.error?.kind === "outside_window" ? p.error : null;
  const blocked = p.pending || !check.ok || (outside !== null && !p.optIn);

  const footer = (
    <>
      <Button variant="ghost" onClick={p.onClose} disabled={p.pending}>
        {MESSAGE_COPY.back}
      </Button>
      <Button
        variant="primary"
        icon="send"
        disabled={blocked}
        aria-busy={p.pending || undefined}
        onClick={!blocked ? () => p.onSend(p.text, outside !== null && p.optIn) : undefined}
      >
        {p.pending ? MESSAGE_COPY.pending : MESSAGE_COPY.send}
      </Button>
    </>
  );

  return (
    <Sheet open={p.open} onClose={p.onClose} title={MESSAGE_COPY.title} footer={footer} className="agm-card" dismissible={!p.pending}>
      {p.appt && (
        <div className="agm-field">
          <TextArea
            label={MESSAGE_COPY.label}
            describedBy={hintId}
            value={p.text}
            onChange={(e) => p.onText(e.target.value)}
            rows={5}
            maxLength={MESSAGE_MAX}
            disabled={p.pending}
          />
          <p id={hintId} className="agm-hint">
            {MESSAGE_COPY.hint}
          </p>

          {outside && (
            <div className="agm-notice agm-notice--warn agm-release-outside">
              <p className="agm-notice-head">{MESSAGE_COPY.outsideTitle}</p>
              <p>{MESSAGE_COPY.outsideBody}</p>
              <div className="agm-optin">
                <input
                  id={optInId}
                  type="checkbox"
                  checked={p.optIn}
                  disabled={p.pending}
                  onChange={(e) => p.onOptIn(e.target.checked)}
                />
                <label htmlFor={optInId}>
                  {MESSAGE_COPY.outsideLabel}
                  {outside.costBrl ? ` (${outside.costBrl})` : ""}
                </label>
              </div>
              {outside.waLink && (
                <a className="agm-link" href={outside.waLink} target="_blank" rel="noopener noreferrer">
                  {MESSAGE_COPY.openWhatsapp}
                </a>
              )}
            </div>
          )}

          {p.error && p.error.kind !== "outside_window" && (
            <p className="agm-note agm-note--danger" role="alert">
              <Icon name="alert" size={16} />
              <span>{p.error.kind === "no_channel" ? MESSAGE_NO_CHANNEL : p.error.message}</span>
            </p>
          )}
        </div>
      )}
    </Sheet>
  );
}

export type MessageModalProps = Omit<MessageModalViewProps, "text" | "onText" | "optIn" | "onOptIn">;

export function MessageModal(props: MessageModalProps) {
  const [text, setText] = useState("");
  const [optIn, setOptIn] = useState(false);
  const apptId = props.appt?.id;
  // Re-seed on every open (not on every appt field change): the text and the billed opt-in never carry over.
  useEffect(() => {
    if (!props.open || !props.appt) return;
    setText(messageSuggestion(props.appt));
    setOptIn(false);
  }, [props.open, apptId]); // eslint-disable-line react-hooks/exhaustive-deps
  return <MessageModalView {...props} text={text} onText={setText} optIn={optIn} onOptIn={setOptIn} />;
}
```

(There is no ESLint here, so the disable comment only documents the intent.)

- [ ] **Step 6: Wire `AgendaView.tsx`**

Imports: `import { ReleaseCard } from "./ReleaseCard";`, `import { MessageModal } from "./MessageModal";`, `import type { ReleaseConfirm } from "@/lib/agenda/release";`. Add to `AgendaViewProps` (all optional so older callers and tests compile):

```ts
  onOpenRelease?: (appt: Appt) => void;
  onConfirmRelease?: (c: ReleaseConfirm) => void;
  onOpenMessage?: (appt: Appt) => void;
  onSendMessage?: (text: string, notifyOutsideWindow: boolean) => void;
```

Next to `const resched = …` add:

```ts
  const release = modal?.type === "release" ? modal : null;
  const message = modal?.type === "message" ? modal : null;
```

On the `<Drawer …>` add `onRelease={writable ? p.onOpenRelease : undefined}` and `onMessage={writable ? p.onOpenMessage : undefined}`. After `<RescheduleModal … />` add:

```tsx
      <ReleaseCard
        open={release !== null}
        appt={release?.appt ?? null}
        check={state.check}
        forceAck={release?.forceAck ?? false}
        confirmedSeen={release?.confirmedSeen ?? false}
        pending={release?.pending ?? false}
        error={release?.error ?? null}
        onClose={p.onCloseModal}
        onConfirm={(c) => p.onConfirmRelease?.(c)}
      />
      <MessageModal
        open={message !== null}
        appt={message?.appt ?? null}
        pending={message?.pending ?? false}
        error={message?.error ?? null}
        onClose={p.onCloseModal}
        onSend={(text, notify) => p.onSendMessage?.(text, notify)}
      />
```

- [ ] **Step 7: Wire `AgendaScreen.tsx`**

Import `messageAppointment` and `releaseAppointment` from `@/lib/agenda/secretaria-hub-agenda` and add both to the `HUB` object. In the `<AgendaView …>` props add:

```tsx
      onOpenRelease={(appt) => void controller.openRelease(appt)}
      onConfirmRelease={(c) => void controller.confirmRelease(c)}
      onOpenMessage={controller.openMessage}
      onSendMessage={(text, notify) => void controller.sendMessage(text, notify)}
```

- [ ] **Step 8: Run tests, type check, build**

Run: `npx vitest run components/agenda lib/agenda` — Expected: PASS.
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.
Run: `npm run build` — Expected: success (static export; no new route).

- [ ] **Step 9: Commit**

```bash
git add components/agenda/ReleaseCard.tsx components/agenda/MessageModal.tsx components/agenda/agenda-modals.css components/agenda/AgendaView.tsx components/agenda/AgendaScreen.tsx components/agenda/__tests__/release-card.test.tsx components/agenda/__tests__/message-modal.test.tsx components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): release card with Pix-retention gate and patient message sheet (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---


## Task 7: Configuration data layer — the `Reminders` slice (hydrate, edit, save)

**Files:**
- Modify: `lib/real/secretaria-hub.ts` (`TenantConfigWire`, `TenantConfigUpdatePayload`)
- Modify: `lib/contexto/types.ts`, `lib/contexto/hub-mapping.ts`, `lib/contexto/snapshot.ts` (`TenantSlices` only), `lib/contexto/screen.ts`
- Create: `lib/contexto/reminders.ts`
- Test: `lib/contexto/__tests__/reminders.test.ts`

**Interfaces:**
- Consumes: the backend fields described under "Backend gap owned elsewhere" (Consumes section). Until the backend exposes them, `supported` is false and nothing below changes behaviour.
- Produces:
  - `type Reminders = { supported: boolean; v2Enabled: boolean; extraLeadMinutes: number | null }`, `DEFAULT_REMINDERS` (`types.ts`); `TenantSlices.reminders: Reminders`.
  - `applyWireReminders(cfg: TenantConfigWire): Reminders` (`hub-mapping.ts`); `buildConfigUpdatePayload(..., clinicDays, reminders?: Reminders)` — the new last parameter is optional; the key `reminder_extra_lead_minutes` is added **only** when `reminders?.supported`.
  - Reducer action `{ type: "set_reminders"; extraLeadMinutes: number | null }`.
  - `lib/contexto/reminders.ts`: `LEAD_PRESET_DAYS`, `formatLead(min)`, `leadOptions(current)`, `parseLead(value)`.

- [ ] **Step 1: Write the failing tests**

Create `lib/contexto/__tests__/reminders.test.ts`:

```ts
// The "Lembretes" slice (TASK-032 R5): hydration is fail-open, the field is only written when the backend exposed it.
import { describe, expect, it } from "vitest";

import { applyWireReminders, buildConfigUpdatePayload } from "../hub-mapping";
import { DEFAULT_REMINDERS } from "../types";
import { tenantSlicesFromWire } from "../snapshot";
import { INITIAL_SCREEN_STATE, screenReducer } from "../screen";
import { formatLead, leadOptions, parseLead } from "../reminders";
import { TENANT_ID, tenantWire } from "./fixtures";

const payloadFor = (wire = tenantWire(), reminders?: Parameters<typeof buildConfigUpdatePayload>[7]) => {
  const t = tenantSlicesFromWire(wire);
  return buildConfigUpdatePayload(t.ctx, t.messages, t.postConsult, t.pixDeposit, t.prefs.defaultDur, t.gcal.mode, t.clinicDays, reminders);
};

describe("applyWireReminders", () => {
  it("an older backend (neither key) is NOT supported and reads as off", () => {
    expect(applyWireReminders(tenantWire())).toEqual(DEFAULT_REMINDERS);
    expect(DEFAULT_REMINDERS.supported).toBe(false);
  });

  it("the lead key present (even null) makes the section supported; null is 'no extra reminder'", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: false, reminder_extra_lead_minutes: null }))).toEqual({
      supported: true,
      v2Enabled: false,
      extraLeadMinutes: null,
    });
  });

  it("reads the lead and the read-only v2 flag", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: 7200 }))).toEqual({
      supported: true,
      v2Enabled: true,
      extraLeadMinutes: 7200,
    });
  });

  it("garbage leads (zero, negative, NaN) read as off, never as a number", () => {
    for (const bad of [0, -5, Number.NaN]) {
      expect(applyWireReminders(tenantWire({ reminder_extra_lead_minutes: bad })).extraLeadMinutes).toBeNull();
    }
  });

  it("only the v2 flag without the lead key is NOT editable (nothing to write)", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: true })).supported).toBe(false);
  });

  it("tenantSlicesFromWire carries it", () => {
    expect(tenantSlicesFromWire(tenantWire({ reminder_extra_lead_minutes: 4320 })).reminders.extraLeadMinutes).toBe(4320);
  });
});

describe("buildConfigUpdatePayload — reminder_extra_lead_minutes", () => {
  it("is NEVER sent when the backend did not expose it (parameter omitted, or not supported)", () => {
    expect("reminder_extra_lead_minutes" in payloadFor()).toBe(false);
    expect("reminder_extra_lead_minutes" in payloadFor(tenantWire(), DEFAULT_REMINDERS)).toBe(false);
  });

  it("is sent when supported; 'off' travels as an explicit null (distinct from omitted)", () => {
    const on = payloadFor(tenantWire(), { supported: true, v2Enabled: false, extraLeadMinutes: 7200 });
    expect(on.reminder_extra_lead_minutes).toBe(7200);
    const off = payloadFor(tenantWire(), { supported: true, v2Enabled: false, extraLeadMinutes: null });
    expect("reminder_extra_lead_minutes" in off).toBe(true);
    expect(off.reminder_extra_lead_minutes).toBeNull();
  });

  it("never sends the read-only v2 flag", () => {
    const p = payloadFor(tenantWire(), { supported: true, v2Enabled: true, extraLeadMinutes: 7200 });
    expect("reminders_v2_enabled" in p).toBe(false);
  });
});

describe("set_reminders", () => {
  it("changes only the lead; supported and v2Enabled stay as hydrated", () => {
    let st = screenReducer(INITIAL_SCREEN_STATE, { type: "session_resolved", session: { token: "x.e30.y", tenantId: TENANT_ID, email: "d@exemplo.test", role: "manager", userId: "u" } });
    st = screenReducer(st, { type: "hydration", action: { type: "hydration_started", generation: 2, rosterGeneration: 2 } });
    st = screenReducer(st, { type: "tenant_loaded", generation: 2, tenantId: TENANT_ID, wire: tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: null }) });
    st = screenReducer(st, { type: "set_reminders", extraLeadMinutes: 7200 });
    expect(st.tenant.reminders).toEqual({ supported: true, v2Enabled: true, extraLeadMinutes: 7200 });
    st = screenReducer(st, { type: "set_reminders", extraLeadMinutes: null });
    expect(st.tenant.reminders.extraLeadMinutes).toBeNull();
  });
});

describe("lead presets", () => {
  it("offers Desligado first, then the presets in ascending order", () => {
    const opts = leadOptions(null);
    expect(opts[0]).toEqual({ value: "off", label: "Desligado" });
    expect(opts.map((o) => o.value)).toEqual(["off", "2880", "4320", "7200", "10080", "14400", "20160"]);
    expect(opts.find((o) => o.value === "7200")?.label).toBe("5 dias antes");
  });

  it("a stored value that is not a preset still shows, in place, and round-trips unchanged", () => {
    const opts = leadOptions(1500);
    expect(opts.map((o) => o.value)).toEqual(["off", "1500", "2880", "4320", "7200", "10080", "14400", "20160"]);
    expect(opts.find((o) => o.value === "1500")?.label).toBe("1500 minutos antes");
    expect(parseLead("1500")).toBe(1500);
  });

  it("formats whole days and whole hours naturally", () => {
    expect(formatLead(1440 * 3)).toBe("3 dias antes");
    expect(formatLead(1440)).toBe("1 dia antes");
    expect(formatLead(180)).toBe("3 horas antes");
    expect(formatLead(60)).toBe("1 hora antes");
  });

  it("parseLead: off -> null, junk -> null", () => {
    expect(parseLead("off")).toBeNull();
    expect(parseLead("")).toBeNull();
    expect(parseLead("abc")).toBeNull();
    expect(parseLead("-3")).toBeNull();
    expect(parseLead("7200")).toBe(7200);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run lib/contexto/__tests__/reminders.test.ts`
Expected: FAIL (`../reminders` missing, `Reminders` types missing).

- [ ] **Step 3: Wire types**

In `lib/real/secretaria-hub.ts`, in `TenantConfigWire`, after `asaas_connected: boolean;` add:

```ts
  // TASK-032 reminders. Both ABSENT on a backend that predates them (or before its
  // config endpoint exposes them): the Lembretes section is hidden then and the
  // field is never written. `reminder_extra_lead_minutes` null = no extra reminder.
  reminders_v2_enabled?: boolean;
  reminder_extra_lead_minutes?: number | null;
```

and in `TenantConfigUpdatePayload` (the `Partial<{ … }>`), after `google_calendar_mode: GoogleCalendarMode;` add `reminder_extra_lead_minutes: number | null;` (with a one-line comment: `// reminders_v2_enabled is READ-ONLY: never part of a PUT body`).

- [ ] **Step 4: Types, mapping, payload, slices, reducer**

`lib/contexto/types.ts` — after `DEFAULT_PIX_DEPOSIT`:

```ts
// ---------------------------------------------------------------------------
// Lembretes (TASK-032 R5) — the clinic's extra reminder
// ---------------------------------------------------------------------------

// `supported` is true only when the backend exposed `reminder_extra_lead_minutes`;
// an older backend hides the section and the field is never written. `v2Enabled`
// (reminders_v2_enabled) is READ-ONLY: the owner turns it on per clinic.
export type Reminders = {
  supported: boolean;
  v2Enabled: boolean;
  /** Minutes before the appointment; null = no extra reminder. */
  extraLeadMinutes: number | null;
};

export const DEFAULT_REMINDERS: Reminders = { supported: false, v2Enabled: false, extraLeadMinutes: null };
```

`lib/contexto/hub-mapping.ts` — import `DEFAULT_REMINDERS` and `type Reminders` from `./types` (add to the existing import lists), then add after `applyWirePixDeposit`:

```ts
// Lembretes: fail open. Neither key (older backend) -> unsupported and off. A
// non-positive or non-numeric lead reads as "no extra reminder", never as a number.
export function applyWireReminders(cfg: TenantConfigWire): Reminders {
  if (cfg.reminder_extra_lead_minutes === undefined) return DEFAULT_REMINDERS;
  const lead = cfg.reminder_extra_lead_minutes;
  return {
    supported: true,
    v2Enabled: cfg.reminders_v2_enabled === true,
    extraLeadMinutes: typeof lead === "number" && Number.isFinite(lead) && lead > 0 ? Math.trunc(lead) : null,
  };
}
```

and in `buildConfigUpdatePayload`: add the last parameter and rework the return. Replace the signature tail `  clinicDays: DayConfig[],\n): TenantConfigUpdatePayload {\n  return {` with

```ts
  clinicDays: DayConfig[],
  reminders?: Reminders,
): TenantConfigUpdatePayload {
  const payload: TenantConfigUpdatePayload = {
```

and replace the end `    google_calendar_mode: gcalMode,\n  };\n}` with

```ts
    google_calendar_mode: gcalMode,
  };
  // Only when the backend exposed the field: an explicit null turns the extra reminder off.
  if (reminders?.supported) payload.reminder_extra_lead_minutes = reminders.extraLeadMinutes;
  return payload;
}
```

Also extend the comment above the function with `Lembretes (reminder_extra_lead_minutes, only when the backend exposes it)`.

`lib/contexto/snapshot.ts` — import `applyWireReminders` and `DEFAULT_REMINDERS`/`type Reminders`; add `reminders: Reminders;` to `TenantSlices` (after `pixDeposit`); add `reminders: applyWireReminders(cfg),` to `tenantSlicesFromWire` and `reminders: DEFAULT_REMINDERS,` to `emptyTenantSlices`. (Do not touch `SectionId`/`dirtySections` here: Task 8.)

`lib/contexto/screen.ts` — add to `ScreenAction` after `set_pix`: `| { type: "set_reminders"; extraLeadMinutes: number | null }`; add the reducer case after `set_pix`:

```ts
    case "set_reminders":
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...state.tenant.reminders, extraLeadMinutes: action.extraLeadMinutes } },
      };
```

and pass `tenant.reminders` as the new last argument of `buildConfigUpdatePayload(...)` inside `buildSaveDeps`.

- [ ] **Step 5: `lib/contexto/reminders.ts`**

```ts
// lib/contexto/reminders.ts — the choices behind the "Lembrete extra" field (TASK-032 R5).
// Pure, so a stored value that is not a preset is testable without a DOM.

export const LEAD_PRESET_DAYS = [2, 3, 5, 7, 10, 14] as const;
const DAY_MIN = 1440;

export type LeadOption = { value: string; label: string };

/** "3 dias antes" / "3 horas antes" / "1500 minutos antes", derived from the minutes themselves. */
export function formatLead(min: number): string {
  if (min % DAY_MIN === 0) {
    const d = min / DAY_MIN;
    return `${d} ${d === 1 ? "dia" : "dias"} antes`;
  }
  if (min % 60 === 0) {
    const h = min / 60;
    return `${h} ${h === 1 ? "hora" : "horas"} antes`;
  }
  return `${min} minutos antes`;
}

/**
 * "Desligado" first, then the presets ascending. A stored value that is not a
 * preset (set through the API) is added IN PLACE, so the select shows what is
 * really stored instead of silently showing another option.
 */
export function leadOptions(current: number | null): LeadOption[] {
  const minutes: number[] = LEAD_PRESET_DAYS.map((d) => d * DAY_MIN);
  if (current !== null && !minutes.includes(current)) minutes.push(current);
  minutes.sort((a, b) => a - b);
  return [{ value: "off", label: "Desligado" }, ...minutes.map((m) => ({ value: String(m), label: formatLead(m) }))];
}

/** The select's value -> minutes; "off" and anything unusable -> null (no extra reminder). */
export function parseLead(value: string): number | null {
  if (value === "off") return null;
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}
```

- [ ] **Step 6: Run tests and type check**

Run: `npx vitest run lib/contexto` — Expected: PASS (existing payload equality tests unchanged: the new parameter is omitted by tests and `supported` is false for the fixtures).
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — if a test builds a `TenantSlices` object literally and now misses `reminders`, add `reminders: DEFAULT_REMINDERS` to that literal (import from `@/lib/contexto/types`). Expected after that: no errors.

- [ ] **Step 7: Commit**

```bash
git add lib/real/secretaria-hub.ts lib/contexto/types.ts lib/contexto/hub-mapping.ts lib/contexto/snapshot.ts lib/contexto/screen.ts lib/contexto/reminders.ts lib/contexto/__tests__/reminders.test.ts
git commit -m "feat(contexto): reminders slice - hydrate, edit and save the extra reminder lead (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

(Plus any test file touched only to add the new slice field; list it in `git add`.)

---

## Task 8: "Lembretes de consulta" section in the Contexto screen

**Files:**
- Create: `components/contexto/ReminderSection.tsx`
- Modify: `lib/contexto/snapshot.ts` (`SectionId`, `dirtySections`), `components/contexto/ContextoView.tsx`
- Test: `components/contexto/__tests__/reminder-section.test.tsx` (new), `components/contexto/__tests__/contexto-screen.test.tsx` (modify the order test + append), `lib/contexto/__tests__/snapshot.test.ts` (append)

**Interfaces:**
- Consumes: `Reminders`, `leadOptions`, `parseLead` (Task 7); primitives `Panel`, `Select`, `Badge`, `Icon`.
- Produces: `ReminderSection({ v: Reminders; setLead: (minutes: number | null) => void; readOnly?: boolean })` (hook-free); `SectionId` gains `"rem"`; `CONTEXTO_SECTION_ORDER` becomes `["prof","gcal","ctx","srv","disp","msg","rem","pix","pos"]`; the `rem` anchor is rendered **only** when `tenant.reminders.supported`.

- [ ] **Step 1: Write the failing tests**

Create `components/contexto/__tests__/reminder-section.test.tsx`:

```tsx
// ReminderSection is hook-free: markup via renderToStaticMarkup, and the element tree walked for the Select's real props
// (same idiom as pix-section.test.tsx).
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactElement } from "react";

import { ReminderSection } from "../ReminderSection";
import { Select } from "../../primitives/Select";
import type { Reminders } from "@/lib/contexto/types";

function findAllByType(el: unknown, type: unknown, out: ReactElement[] = []): ReactElement[] {
  if (!el || typeof el !== "object") return out;
  const node = el as ReactElement<{ children?: unknown }>;
  if (node.type === type) out.push(node);
  const children = node.props?.children;
  for (const child of Array.isArray(children) ? children : [children]) findAllByType(child, type, out);
  return out;
}

const off: Reminders = { supported: true, v2Enabled: false, extraLeadMinutes: null };

describe("ReminderSection", () => {
  it("has a labelled select (Lembrete extra) with Desligado selected when nothing is configured", () => {
    const html = renderToStaticMarkup(<ReminderSection v={off} setLead={() => {}} />);
    expect(html).toContain('aria-label="Lembretes de consulta"');
    expect(html).toMatch(/<label for="[^"]+" class="field-label">Lembrete extra<\/label>/);
    expect(html).toContain('<option value="off" selected="">Desligado</option>');
  });

  it("shows the stored value even when it is not a preset", () => {
    const html = renderToStaticMarkup(<ReminderSection v={{ ...off, extraLeadMinutes: 1500 }} setLead={() => {}} />);
    expect(html).toContain('<option value="1500" selected="">1500 minutos antes</option>');
  });

  it("says in words whether the automatic reminders are on, never by colour alone", () => {
    expect(renderToStaticMarkup(<ReminderSection v={off} setLead={() => {}} />)).toContain("Lembretes ainda não ativos");
    const on = renderToStaticMarkup(<ReminderSection v={{ ...off, v2Enabled: true }} setLead={() => {}} />);
    expect(on).toContain("Lembretes ativos");
    expect(on).not.toContain("ainda não ativos");
  });

  it("wires the select to setLead: a preset becomes minutes, Desligado becomes null", () => {
    const setLead = vi.fn();
    const select = findAllByType(ReminderSection({ v: off, setLead }), Select)[0];
    const props = select.props as { value: string; onChange: (v: string) => void; disabled?: boolean };
    expect(props.value).toBe("off");
    props.onChange("7200");
    props.onChange("off");
    expect(setLead.mock.calls).toEqual([[7200], [null]]);
  });

  it("is locked while read-only", () => {
    const select = findAllByType(ReminderSection({ v: off, setLead: () => {}, readOnly: true }), Select)[0];
    expect((select.props as { disabled?: boolean }).disabled).toBe(true);
  });
});
```

In `components/contexto/__tests__/contexto-screen.test.tsx`: replace the two lines of the order test

```tsx
    expect(CONTEXTO_SECTION_ORDER).toEqual(["prof", "gcal", "ctx", "srv", "disp", "msg", "pix", "pos"]);
```
and
```tsx
    expect(ids).toEqual([...CONTEXTO_SECTION_ORDER]);
```
with
```tsx
    expect(CONTEXTO_SECTION_ORDER).toEqual(["prof", "gcal", "ctx", "srv", "disp", "msg", "rem", "pix", "pos"]);
```
and
```tsx
    // The default fixture's backend exposes no reminder keys, so the "rem" anchor is not drawn.
    expect(ids).toEqual(CONTEXTO_SECTION_ORDER.filter((id) => id !== "rem"));
```

then append at the end of that file:

```tsx
describe("ContextoView — Lembretes (TASK-032 R5)", () => {
  const withWire = (wire: ReturnType<typeof tenantWire>) =>
    reduce([
      ...STARTED,
      { type: "tenant_loaded", generation: 2, tenantId: TENANT_ID, wire },
      { type: "roster_loaded", rosterGeneration: 2, tenantId: TENANT_ID, rows: [row], configs: [professionalWire(PROF_A)] },
      { type: "professional_selected", id: PROF_A },
      { type: "professional_loaded", id: PROF_A, rosterGeneration: 2, wire: professionalWire(PROF_A) },
    ]);

  it("older backend: no Lembretes section at all", () => {
    const html = render(withWire(tenantWire()));
    expect(html).not.toContain("Lembretes de consulta");
    expect(html).not.toContain('<div id="rem"');
  });

  it("backend exposing the field: the section sits between Mensagens and Sinal via Pix", () => {
    const html = render(withWire(tenantWire({ reminders_v2_enabled: false, reminder_extra_lead_minutes: 7200 })));
    const ids = [...html.matchAll(/<div id="([a-z]+)" class="contexto-anchor">/g)].map((m) => m[1]);
    expect(ids).toEqual([...CONTEXTO_SECTION_ORDER]);
    expect(ids.indexOf("rem")).toBe(ids.indexOf("msg") + 1);
    expect(html).toContain('<option value="7200" selected="">5 dias antes</option>');
  });

  it("the field is locked while a save is in flight", () => {
    const html = render({ ...withWire(tenantWire({ reminder_extra_lead_minutes: null })), saving: true });
    const section = /<section[^>]*aria-label="Lembretes de consulta"[\s\S]*?<\/section>/.exec(html)![0];
    expect(section).toContain("disabled");
  });
});
```

Append to `lib/contexto/__tests__/snapshot.test.ts`:

```ts
describe("dirtySections — rem (TASK-032 R5)", () => {
  const base = () => tenantSlicesFromWire(tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: null }));

  it("changing the lead marks only 'rem'", () => {
    const a = base();
    const b = { ...base(), reminders: { ...a.reminders, extraLeadMinutes: 7200 } };
    expect(dirtySections({ tenant: b, professional: null }, { tenant: a, professional: null })).toEqual(["rem"]);
  });

  it("the read-only flags never make the section dirty", () => {
    const a = base();
    const b = { ...base(), reminders: { ...a.reminders, v2Enabled: !a.reminders.v2Enabled, supported: !a.reminders.supported } };
    expect(dirtySections({ tenant: b, professional: null }, { tenant: a, professional: null })).toEqual([]);
  });
});
```

(If `dirtySections`/`tenantWire` are not yet imported in that file, add them to its existing imports.)

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/contexto/__tests__/reminder-section.test.tsx components/contexto/__tests__/contexto-screen.test.tsx lib/contexto/__tests__/snapshot.test.ts`
Expected: FAIL (`../ReminderSection` missing; order mismatch).

- [ ] **Step 3: `SectionId` and dirty detection in `lib/contexto/snapshot.ts`**

Change `export type SectionId = "ctx" | "msg" | "pos" | "pix" | "prof" | "srv" | "disp" | "gcal";` to include `"rem"`:

```ts
export type SectionId = "ctx" | "msg" | "rem" | "pos" | "pix" | "prof" | "srv" | "disp" | "gcal";
```

Add next to `comparablePix`:

```ts
// Only the lead is editable; `supported` and `v2Enabled` are backend-derived.
function comparableReminders(r: Reminders): unknown {
  return { extraLeadMinutes: r.extraLeadMinutes };
}
```

and in `dirtySections`, after the `pix` block:

```ts
  if (!same(comparableReminders(current.tenant.reminders), comparableReminders(baseline.tenant.reminders))) {
    sections.push("rem");
  }
```

- [ ] **Step 4: `components/contexto/ReminderSection.tsx`**

```tsx
// ReminderSection — "Lembretes de consulta" (TASK-032 R5, spec 4.5): the clinic's
// ONE configurable reminder. The 1-day and 1-hour reminders are fixed product
// behaviour; this field only adds an earlier one (`reminder_extra_lead_minutes`).
//
// Hook-free (like PixSection) so tests can walk its element tree. It is rendered
// only when the backend exposed the field (Reminders.supported), so an older
// backend never shows a control that saves nowhere.
//
// Honest about the switch: `reminders_v2_enabled` is the owner's per-clinic
// interruptor and is READ-ONLY here. While it is off the choice is saved and
// takes effect when the owner turns reminders on — the same "prepare ahead"
// idiom as the Pix section — and the badge says so in words.

import { Panel } from "../primitives/Panel";
import { Select } from "../primitives/Select";
import { Badge } from "../primitives/Badge";
import { leadOptions, parseLead } from "@/lib/contexto/reminders";
import type { Reminders } from "@/lib/contexto/types";
import "./contexto.css";

export type ReminderSectionProps = {
  v: Reminders;
  setLead: (minutes: number | null) => void;
  /** Locked while the screen cannot save (loading, error, saving). */
  readOnly?: boolean;
};

export function ReminderSection({ v, setLead, readOnly }: ReminderSectionProps) {
  const options = leadOptions(v.extraLeadMinutes);
  return (
    <Panel as="section" label="Lembretes de consulta" className="contexto-section" title={<h2>Lembretes de consulta</h2>}>
      <p className="contexto-section-desc">
        A secretarIA lembra o paciente 1 dia e 1 hora antes da consulta. Aqui você pode acrescentar um lembrete extra,
        mais cedo.
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

      <div className="contexto-section">
        <Select
          value={String(v.extraLeadMinutes ?? "off")}
          onChange={(next) => setLead(parseLead(next))}
          label="Lembrete extra"
          options={options}
          disabled={readOnly}
        />
        <p className="contexto-field-tip">
          Enviado antes dos lembretes fixos de 1 dia e 1 hora. Fica desligado até você escolher um prazo.
        </p>
      </div>
    </Panel>
  );
}
```

- [ ] **Step 5: Wire `ContextoView.tsx`**

Import `import { ReminderSection } from "./ReminderSection";`. In `CONTEXTO_SECTION_ORDER` insert `"rem",` between `"msg",` and `"pix",`. In the `sections` record add after `msg`:

```tsx
    rem: (
      <ReminderSection
        v={tenant.reminders}
        setLead={(minutes) => dispatch({ type: "set_reminders", extraLeadMinutes: minutes })}
        readOnly={tenantReadOnly}
      />
    ),
```

and replace `{CONTEXTO_SECTION_ORDER.map((id) => (` with:

```tsx
          {CONTEXTO_SECTION_ORDER.filter((id) => id !== "rem" || tenant.reminders.supported).map((id) => (
```

- [ ] **Step 6: Run tests, type check, build**

Run: `npx vitest run components/contexto lib/contexto` — Expected: PASS.
Run: `.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.
Run: `npm run build` — Expected: success.

- [ ] **Step 7: Commit**

```bash
git add components/contexto/ReminderSection.tsx components/contexto/ContextoView.tsx lib/contexto/snapshot.ts components/contexto/__tests__/reminder-section.test.tsx components/contexto/__tests__/contexto-screen.test.tsx lib/contexto/__tests__/snapshot.test.ts
git commit -m "feat(contexto): Lembretes de consulta section with the extra reminder lead (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 9: Checkpoint doc and final validation

**Files:**
- Create: `docs/CHECKPOINT_brain_message_lembretes_agenda.md`
- Modify: `docs/CHECKPOINT_brain_message_agenda.md` (one pointer line)

- [ ] **Step 1: Full validation**

Run, from the worktree (PowerShell), in this order:
`.\node_modules\.bin\tsc.cmd --noEmit` — Expected: no errors.
`npm test` — Expected: all suites green. If a failure looks unrelated, run the same test on the base commit (`git stash` is not allowed here: use `git worktree add` of `main`, or `git diff main --stat` to confirm the failing file is untouched) and report **pre-existing** vs **new** separately; do not "fix" unrelated reds.
`npm run build` — Expected: static export succeeds.
Never `npm run lint`.

- [ ] **Step 2: Write `docs/CHECKPOINT_brain_message_lembretes_agenda.md`**

```markdown
# CHECKPOINT — Lembretes na agenda (TASK-032 R5)

**Estado:** implementado e testado localmente; commitado na branch da tarefa; NÃO deployado. Depende do backend (R1 + R4 + exposição dos campos de configuração).

## O que entrou onde
- `lib/agenda/confirmation.ts` — mapeamento do read (status, `confirmation_count`, `display_state`, `attention`, `reminders`), `confirmationLook` (neutro / ✓ / ✓✓ / vermelho), legenda, linha do tempo, `applyStaffConfirmation` (virada ao vivo).
- `lib/agenda/release.ts` + `secretaria-hub-agenda.ts` — `releaseAppointment`, `messageAppointment`, regra do aviso de retenção do Pix.
- `lib/agenda/screen.ts` — modais `release`/`message`, `confirmation_recorded`, métodos do controller.
- `components/agenda/` — `ConfirmationBadge` (marca/selo/legenda), `Drawer` (selo, linha do tempo, ações do estado vermelho), `ReleaseCard`, `MessageModal`, tons na grade (`CalendarViews`).
- `app/styles/tokens.css` — `--st-neutral-*`, `--st-confirm2-*`, `--st-attn-*` nos dois temas.
- `lib/contexto/` + `components/contexto/ReminderSection.tsx` — seção "Lembretes de consulta" (campo `reminder_extra_lead_minutes`), só aparece se o backend expõe o campo.

## Regras que os testes fixam
Status terminal mantém o visual próprio; vermelho→verde ao confirmar; teto de dois vês; evento só do Google e bloqueio inalterados; liberar com sinal pago exige o aviso marcado e nada é enviado sem ele; falha ao liberar mostra erro e mantém o estado; sem animação nova; backend antigo (sem as chaves novas) renderiza como antes.

## Contrato (copiado do plano R4 em 2026-10-04)
`POST …/appointments/{id}/release` `{acknowledge_retention, release_confirmed, notify_outside_window}` → `AppointmentRead + patient_notice` (409 `retention_ack_required` / `already_confirmed` / `not_live`, 502 `calendar_unavailable`); `POST …/appointments/{id}/message` `{text, notify_outside_window}` → `{delivery, email_nudge, message_id}` (409 `outside_window_not_authorised`, 422 `no_channel`). Ajustes ficam em `secretaria-hub-agenda.ts`. O e-mail de aviso da clínica abre `?consulta=<id>`: a agenda aceita o alias; para cair na semana certa o link precisa de `&data=AAAA-MM-DD`.

## Pendências
- Backend: expor `reminders_v2_enabled` (leitura) e `reminder_extra_lead_minutes` (leitura/escrita) em `GET/PUT /tenants/me/config` e `PUT /tenants/me/configuration`.
- Atualização automática da agenda quando o paciente confirma pelo WhatsApp: hoje a agenda só relê ao navegar/agir; polling ou foco da aba ficou fora do escopo.
- Prova ao vivo (agent-browser, dois temas, teclado, leitor de tela) só depois do deploy do backend e do pedido do dono.
```

- [ ] **Step 3: Pointer line**

Append to `docs/CHECKPOINT_brain_message_agenda.md` one line: `- Estado de confirmação, ✓/✓✓, liberar horário e lembrete extra: ver CHECKPOINT_brain_message_lembretes_agenda.md (TASK-032 R5).`

- [ ] **Step 4: Manual check the executor can do before handing over (no deploy)**

`next dev` runs in mock mode with no real session (the agenda renders its signed-out state), so the visual states cannot be seen locally without a backend. Do **not** invent demo data in the product code. The two-theme / keyboard / screen-reader pass is part of the post-deploy live proof below.

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_brain_message_lembretes_agenda.md docs/CHECKPOINT_brain_message_agenda.md
git commit -m "docs(agenda): checkpoint for reminders on the agenda (TASK-032 R5)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Deploy and live proof (not part of the tasks)

**Deploy is never part of this plan.** The front deploys only **after the backend plans (R1, R2, R3, R4 and the config-field exposure) are deployed** and **after the owner asks for it**. Order stays migration → API and worker together → front, with the clinic switch `reminders_v2_enabled` on only for the test clinic first.

Live proof, after that, with `agent-browser` (skill) against the real console on the test clinic, in light **and** dark theme:
1. A booked appointment with no answer shows the neutral look; after a reminder deadline passes it turns red with the "Sem confirmação" badge and the legend; answering "Confirmar" on the patient side turns it green on the next load; a second confirmation shows the double tick.
2. Drawer: timeline lines match what happened (sent, answered, clinic warned); keyboard-only: Tab reaches the three actions in order, Enter opens each sheet, Escape closes, focus returns to the slot.
3. "Liberar horário" on an unpaid appointment frees the slot and the patient receives the notice (inside and outside the 24 h window, with and without the paid opt-in); on a Pix-paid one the retention warning appears and the button stays disabled until the box is ticked; opening the clinic's warning e-mail link (`?consulta=`) opens that appointment's drawer.
4. "Enviar mensagem ao paciente" delivers on the patient's channel; a forced failure keeps the sheet open with the error.
5. `/contexto` shows "Lembretes de consulta"; choosing "5 dias antes" and saving round-trips (reload shows it); "Desligado" persists as off.
6. Reduced motion (browser emulation) shows no animation on the new elements; a screen reader reads the badge text, the action names and the timeline list.

---

## Self-Review

**Spec coverage.** §4.1 display states: Tasks 1–2 (looks, tokens, ticks, labels). §4.4 agenda draws from appointment data, legend, drawer timeline (sent / answered / clinic warned): Tasks 1–3. Actions — Liberar horário with Pix-retention confirmation (Tasks 4–6), Enviar mensagem ao paciente (Tasks 4–6), Marcar como confirmado (Tasks 3, 5; reuses PATCH status through the counter). Google-only block events unchanged: Tasks 1, 2, 3. §4.5 `reminder_extra_lead_minutes` field and reading `reminders_v2_enabled`: Tasks 7–8; the missing backend exposure is named, owner proposed (R4 or an R1 addendum), and the front is graceful until it lands. Criterion 8 ("confirmar muda para verde na hora") and 9 (release respects Pix): Tasks 5–6. Out of scope here: automatic release, push, polling.

**Review Focus → test.** 1 terminal looks: Task 1 (`confirmationLook`, mapping), Task 2 (grid), Task 3 (drawer). 2 live flip: Task 5. 3 cap at two: Task 1, Task 2. 4 Google-only/blocks: Tasks 1, 2, 3. 5 Pix gate: Task 4 (`buildReleaseBody`), Task 5 (controller sends nothing), Task 6 (disabled button, labelled checkbox). 6 release failure: Tasks 5, 6. 7 names: Tasks 2, 3, 6. 8 old backend: Tasks 1, 2, 3, 7, 8. 9 reduced motion / themes: Task 2 CSS test. 10 lead edge cases: Tasks 7, 8.

**Placeholders.** None: every code step is complete. R4's contract was read from its plan on 2026-10-04; if R4 changes before execution, only the constants/types at the top of `secretaria-hub-agenda.ts` and `release.ts` move.

**Type consistency.** `ConfirmationLook` fields (`tone`, `ticks`, `icon`, `label`, `ariaText`, `hint`) are used identically in Tasks 2, 3. `ReleaseModalError` kinds (`already_done`, `ack_required`, `already_confirmed`, `failed`) and `MessageModalError` kinds (`failed`, `no_channel`, `outside_window`) match between Task 5 (controller/reducer) and Task 6 (`errorText`, `MessageModalView`). `confirmRelease(c: ReleaseConfirm)` matches `ReleaseCardView.onConfirm` and `AgendaViewProps.onConfirmRelease`; `sendMessage(text, notifyOutsideWindow)` matches `MessageModalView.onSend` and `AgendaViewProps.onSendMessage`. `Reminders` fields match across Tasks 7–8. `buildReleaseBody(appt, { acknowledged, forceAck, confirmedSeen, notifyOutsideWindow })` is called with the modal's `forceAck`/`confirmedSeen` in the controller and tested with the same option names.
