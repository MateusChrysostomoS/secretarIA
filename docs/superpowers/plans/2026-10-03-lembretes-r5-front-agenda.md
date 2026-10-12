# Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the clinic's agenda (Brain-Message-Frontend) draw each appointment from its real data — neutral / green ✓ / green ✓✓ / red, terminal statuses in their own colour — with a legend and a reminder timeline; give the drawer the owner's time-gated action matrix (before the start: Liberar horário, Confirmar, Compareceu, Editar/Remarcar, Cancelar consulta, Enviar mensagem; after the start: Compareceu, Faltou, Editar/Remarcar; closed: nothing); ask the clinic before any paid WhatsApp notice (with "Não perguntar novamente"); tell the clinic how the patient was notified after every action; replace "Remarcar" + the disabled "Editar" with one "Editar/Remarcar" sheet on R7's `POST …/edit`, pre-filled from what the agenda now carries (doctor, service, attendee, contact phone, patient channel); mirror who is looking (R7 `GET /calendar/viewer`): a doctor restricted to his own agenda never gets a "Todos / Só os meus" switch and books/blocks only on his own agenda, a manager who is also a doctor gets the switch, a secretary sees everything; and give the clinic a "Lembretes e avisos" configuration section (paid-notice switch, read-only reminders state, extra-reminder lead 1500..20160 min) plus the post-consult text hint.

**Architecture:** Pure logic first (`lib/agenda/confirmation.ts`, `actions.ts`, `notice.ts`, `release.ts`, `edit.ts`), each unit-tested in the repo's node-only vitest (no jsdom: markup via `renderToStaticMarkup`, reducers/controllers via fakes). The wire mapping is strictly additive and fail-open to today's look when the backend omits a key. Presentational components (`ConfirmationBadge`, legend, `Drawer`, `PaidNoticeSheet`, `ReleaseCard`, `MessageModal`, `EditModal`, `CancelCard`) are fed by `Appt` fields and the screen state; every write goes through the existing `createAgendaController` (session re-resolution, tenant guard, generation guard). The paid-notice decision is one pure function (`decidePaidNotice`) over the existing `GET …/cancel-preview` plus the clinic flag `paid_notices_auto_approved`; every action's toast comes from one pure function (`actionToast`) over R4/R7's `patient_notice` vocabulary. Who is a Portal patient comes from the event's `patient_channel`, never from a missing WhatsApp link. Who is looking is one pure value (`lib/agenda/viewer.ts::AgendaViewer`, from R7 `GET /calendar/viewer`, read once before the first range); the server is the authority (it filters the read, answers 404 for what the viewer may not see, decides the doctor of a new row) and the front only stops offering what it would refuse; the "Só os meus" choice lives in the URL (`?meus=1`). The configuration section is a new `rem` section in the existing Contexto screen (`/contexto`), hydrated/saved through the existing `PUT /tenants/me/configuration`, each key sent only when the backend exposed it.

**Tech Stack:** Next.js 15 static export, React 19, TypeScript 5, hand-written CSS design system (`app/styles/tokens.css` + per-module CSS), vitest 2 (node env). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` (owner's decisions of 2026-10-09 — **binding**, wins over everything below) and `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` (§4.1 display states, §4.4 agenda, §4.5 configuration; success criteria 8 and 9). §5 of the 2026-10-09 spec (who sees the agenda, the extra reminder, the Editar data) and the owner's later decision of the same day (a restricted doctor books and blocks only on his own agenda) are part of it. Backend contracts, used verbatim: `docs/superpowers/plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md` ("Produces for R5 — exact wire shapes" and "Decisions taken where the spec is silent", including Tasks 13–16: viewer, `mine`, the six event keys, create on his own agenda, reminders config), `docs/superpowers/plans/2026-10-03-lembretes-r4-avisos-e-liberar.md` (release / message / warning link), `docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md` (Task 7 `GET /events`). The shapes this plan relies on are copied under "Consumes".

**Repo / worktree:** all code lives in `Brain-Message-Frontend`. The main checkout `C:\TECH\BRAIN\Brain-Message-Frontend` is read-only for this plan; execution happens in a fresh worktree `C:\TECH\BRAIN-worktrees\TASK-045\Brain-Message-Frontend`, branch `task/TASK-045-lembretes-r5-front` (TASK-045 = next free id after R7's TASK-044), cut from `main` (if the owner has moved the Pix phase, from `feature/out-of-mvp` per `AI_WORKFLOW.md`); register it in `C:\TECH\BRAIN\tasks\TASK-045\TASK.md` (copy of `tasks/TEMPLATE.md`). **Code-base revalidation (2026-10-09):** `main` = `origin/main` = `c2d3ff7`, clean tree; every path, export and anchor this plan names was re-read on that head (agenda: `lib/agenda/{types,hub-mapping,modal-forms,cancel-preview,screen,secretaria-hub-agenda}.ts`, `components/agenda/{Drawer,CancelCard,RescheduleModal,AgendaView,AgendaScreen,CalendarViews,Modals}.tsx`, `app/agenda/page.tsx` (passes `parseAgendaParams(useSearchParams().toString())` as `initial`); contexto: `lib/contexto/{types,hub-mapping,snapshot,screen,toc}.ts`, `components/contexto/{ContextoView,PostConsultSection}.tsx`; `lib/real/secretaria-hub.ts`). Facts that shaped this revision: the events read maps every slot to `"agendado"` and carries no phone, professional, service or attendee name on `c2d3ff7` (R7 Task 14 adds them; Task 1 maps them); `cancel-preview.ts::PatientReach` already has an unused `channel?: "whatsapp" | "portal"` (Task 6 feeds it from `patient_channel`); `NewApptModal`/`BlockModal` (`components/agenda/Modals.tsx`) have no doctor field and `createAppt`/`createBlock` send none; `parseAgendaParams` returns exactly `{anchorKey, view, consultaId}` (three `toEqual` pins in `screen.test.ts`); the drawer today offers a `StatusPicker` (Confirmou / Compareceu / Faltou, no time gate), an enabled "Remarcar" and a disabled "Editar"; `SectionId` now includes `"facts"` and the order is `prof, gcal, ctx, facts, srv, disp, msg, pix, pos`; `buildConfigUpdatePayload` takes 8 arguments (`clinicFacts` is the 5th); `TOC_LABELS` is a `Record<SectionId, string>`; `PostConsultSection` already edits `post_consult_message`. **Before Task 1, `git log --oneline -1` must still print `c2d3ff7`; if not, re-read the files above and reconcile any anchor that moved.**

**Order (execution):** `C:\TECH\BRAIN\brain-api\docs\superpowers\plans\2026-10-09-hub-token-papel-profissional.md` (brain-api: `agenda_scope` on the hub-token introspection) → R7 (secretarIA backend, TASK-044, `plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md`, **merged to `main` first**) → this plan. R5 calls `POST …/edit`, `notify_outside_window` on PATCH status, `GET /calendar/viewer`, `GET /events?mine=`, `professional_id` on create/block, reads `patient_notice`/`whatsapp_link` on every action, the six event keys and the clinic keys `paid_notices_auto_approved` / `reminders_v2_enabled` / `reminder_extra_lead_minutes`. **Order (deploy, never done by this plan):** secretarIA migration (`alembic upgrade head`, R7 `d8e3a5c1f7b2`) → brain-api → `secretaria_api` + `secretaria-worker` together (`deploy_parity=match`) → this front. Backend state on 2026-10-09: R1, R2, R3, R4 and R6 are merged and deployed (R4: `origin/main` `5e66c9d`, production fingerprint `aa11e3be7642`, production-tested 2026-10-09 — `TESTES_REAIS_lembretes_r4_2026-10-09.md`, `CHECKPOINT_lembretes_r4.md`); the events read already returns `status`, `confirmation_count`, `display_state`, `attention`, `reminders`; R7 is planned, not built. Every new key is still read fail-open, so a front deployed against a backend without R7 degrades to today's behaviour instead of breaking (see Global Constraints).

## Change log 2026-10-09 (owner decisions — spec `2026-10-09-acoes-clinica-avisos-paciente-design.md`)

- **Drawer actions follow the clock, not the colour.** The R5 draft offered three actions only in the red state ("Confirmação" box) and kept today's ungated status picker. Now the drawer offers, for a live appointment before its start, Liberar horário · Confirmar · Compareceu · Editar/Remarcar · Cancelar consulta · Enviar mensagem; after the start, Compareceu · Faltou · Editar/Remarcar; for a cancelled / attended / no-show one, nothing. "Faltou" is never offered before the start and the backend's `409 no_show_before_start` is shown, not retried. "Liberar horário" is no longer red-state only (Task 3; Pix-retention card unchanged, Task 6).
- **One "Editar/Remarcar" sheet** replaces "Remarcar" and the disabled "Editar": date/time, service, doctor, convênio, attendee name and contact phone through R7's `POST …/edit`; `RescheduleModal`, the `/reschedule` client call and the reschedule helpers are removed from the UI (Tasks 8–9).
- **Paid WhatsApp notices are asked, once or forever.** Before an action that notifies, the front reads `cancel-preview` + `paid_notices_auto_approved`; outside the 24 h window and not auto-approved it asks with the cost, an "Enviar mesmo assim" choice and a "Não perguntar novamente" checkbox that saves the flag; Portal patients (no WhatsApp number on the appointment) are never asked (Tasks 4–6). A switch in Contexto turns the flag back off (Task 11).
- **Every action tells the clinic what happened to the patient**: Confirmar, Compareceu, Editar/Remarcar, Cancelar, Liberar show R7's `patient_notice` in Portuguese, with the free wa.me link when `whatsapp_outside_window` (Tasks 4–6). Confirmar / Compareceu / Faltou go through `PATCH …/status` with `notify_outside_window`, answering `AppointmentActionRead`.
- **Colour fix:** a terminal appointment takes its colour from `status`, never from `display_state` (the backend reports `"unconfirmed"` for cancelled / attended / no-show) — pinned in Tasks 1–2.
- **Compareceu's text** is the clinic's `post_consult_message`, already editable in Contexto → Pós-consulta; the section now says when it is sent and that a default "how was your visit" text is used when empty (Task 11). No new field.
- Kept from the draft: badge, legend, timeline (now also labels R7's `staff_confirm` / `staff_edit` rows), red → green live flip, `?consulta=` deep link, Pix-retention release card, message sheet, extra-reminder field. Tasks renumbered 1–11; worktree TASK-045; R7 is now a prerequisite; commit trailer is Claude Opus 5.5.

## Change log 2026-10-09, second round (owner decisions — spec §5 and the "create on his own agenda" decision; R7 "Produces for R5" as updated the same day)

- **Who is looking (new Task 7).** `GET /calendar/viewer` → `{agenda_scope, professional_id, professional_name, can_filter_own}` is read once, before the first range (and again after a clinic switch). A secretary sees everything and has no switch; a manager who is also a doctor (`can_filter_own`) gets "Todos / Só os meus", which sends `GET /events?mine=true` and is kept in the URL as `?meus=1`; a doctor restricted to his own agenda (`"own"`) never gets the switch (the server already narrows his read). A failed or missing viewer read keeps today's look (no switch, create sends no doctor): the server still applies the real rule.
- **A restricted doctor books and blocks only on his own agenda** (owner's decision 2026-10-09, replaces R7's earlier "cannot create"): "Nova consulta" and "Bloquear" stay available, show "Profissional: <his name>" locked, and send his `professional_id`; with no doctor of his own (`"own"` + `professional_id: null`) both buttons are disabled with the reason. R7's 403 `professional_not_allowed` and 422 `no_own_agenda` are shown with the backend's own sentence (Task 7); in Editar/Remarcar his doctor field is locked to himself (Tasks 8–9).
- **The six event keys** (`professional_id`, `professional_name`, `service`, `attendee_name`, `phone`, `patient_channel`) are mapped in Task 1; Editar/Remarcar now **pre-fills** doctor, service, attendee and contact phone from them (Tasks 8–9, replaces "start on keep"); the drawer shows the doctor (Task 3).
- **Portal = `patient_channel: "brain_message"`**, no longer "no WhatsApp link on the preview": `decidePaidNotice` (Task 4) and the cancel/release/edit notice block (`reachFor`, Task 6) read the channel; a Portal patient is never asked and is now told he gets the notice in the chat (R7 notifies the Portal on every action, cancel included).
- **Hidden appointments (R7 404):** a `?consulta=` deep link to an appointment that is not in the viewer's agenda says "A consulta do link não foi encontrada na sua agenda." and opens nothing; an action answered 404 says so and reloads (Task 7; the edit sheet in Task 8).
- **Configuration (Tasks 10–11):** R7 Task 16 now owns `reminders_v2_enabled` (read-only) and `reminder_extra_lead_minutes` (`1500..20160`, `null` = off), so the "hidden until a backend owns it" caveat is gone (the field still renders only when the key is present — older backend); a legacy stored lead outside `1500..20160` is shown but never re-sent unchanged (it would 422 the whole save).
- Tasks are now 1–12 (Task 7 inserted; old 7→8, 8→9, 9→10, 10→11, 11→12).

## Decisions taken where the spec is silent

1. **"Now" is the browser clock, the start is the slot's own start** (`date` + `start`, browser-local, exactly what the grid draws). Before the screen has a clock (static export, `useNow()` is `null` until mount) the drawer offers only the actions valid in both phases: Compareceu and Editar/Remarcar.
2. **Who is "a Portal patient" for the ask:** the event's `patient_channel` (R7 Task 14). Only `"whatsapp"` can be asked (outside 24 h, flag off); `"brain_message"` (Portal) is never asked — the Portal has no window and R7 tells him in the chat; `null` (no patient record: a phone-only booking) or an absent key (backend without R7) is never asked either, and the response's `patient_notice` tells what really happened. The preview's `whatsapp_link` is no longer a channel signal; it is only the free link and the "has a WhatsApp number" fact for a non-Portal patient.
3. **A failed preview or flag read never bills silently:** the action proceeds with `notify_outside_window: false`; the response's `patient_notice` tells the clinic what happened. A failed flag read counts as "not approved" (the clinic is asked rather than billed).
4. **"Não perguntar novamente" only counts together with "Enviar mesmo assim"** (it is the standing authorisation for paid notices). It is saved **before** the action; if saving fails the sheet shows the error and nothing is sent.
5. **Confirmar / Compareceu ask in a small sheet only when asking is needed;** otherwise they act at once (no sheet flash). Faltou never notifies, so it never asks.
6. **Cards that already carry a notice block (Cancelar, Liberar, Editar/Remarcar)** keep their "send the paid notice" checkbox as the "Enviar mesmo assim" choice and gain the "Não perguntar novamente" checkbox; with the flag on they say the paid notice will go and send `notify_outside_window: true`.
7. **Editar/Remarcar starts from what the agenda carries:** date, time, convênio, doctor, service, attendee and contact phone are all pre-filled from the event (R7's six keys). A field is sent only when it differs from the current value; nothing changed → the save button stays disabled. The doctor select lists the clinic's active doctors plus the current one; with no doctor yet it starts on "Sem profissional definido" (not sent). For a viewer restricted to his own agenda the doctor select is locked to himself. "A consulta é do próprio paciente" starts ticked when there is no attendee name and sends `attendee_name: null` only when there was one. The contact phone is validated (12–15 digits) only when changed; emptying it means "keep" (clearing is not offered from the agenda). Duration is not editable (R7 derives it from the service).
8. **Slot taken** (`409 slot_unavailable`) → the sheet explains and the button becomes "Salvar mesmo assim (encaixe)", which resends with `allow_overlap: true` (mirrors R4's "Liberar mesmo assim"). `appointment_changed` / `not_live` → explained, the agenda reloads, nothing is resent.
9. **Terminal means no buttons,** although R7 accepts an `attended ↔ no_show` correction: the owner's matrix wins; a correction stays possible through the API only.
10. **Confirmar stays offered on an already confirmed slot** before the start (the backend de-duplicates the card and answers `patient_notice: null`).
11. **Toasts carry the wa.me link only when it is an `https://wa.me/` address** (untrusted data into an `href`), and an unknown `patient_notice` value (including R4's `"not_attempted"`) is success-neutral: nothing is claimed.
12. **Cancel's toast** comes from `patient_notice`; a backend that does not send the key falls back to today's verdict-based toast. The cancel/release/edit notice block tells a Portal patient's case apart by `patient_channel` (Task 6): "Este paciente usa o Portal: a secretarIA deixa o aviso na conversa dele…", button "Cancelar e avisar paciente"; a patient with neither WhatsApp nor Portal keeps "não será avisado" and "Cancelar sem avisar".
13. **The paid flag is read when a card/sheet opens or an action starts**, never cached across actions, so a change made in Contexto (or in another tab) applies to the next action.
14. **Configuration:** the paid-notice switch appears when the backend exposes `paid_notices_auto_approved`; the reminders state (read-only) and the extra-reminder field appear when it exposes `reminder_extra_lead_minutes` — both are R7 (Tasks 2 and 16), so on the target backend the whole section shows; the key-presence check only keeps an older backend safe. The section is "Lembretes e avisos" and is hidden when neither key is exposed. The lead is offered only inside R7's `1500..20160`; a legacy stored value outside it is shown as stored and **omitted** from the save while untouched (sending it back would 422 every other section of the same save).
15. **"Só os meus" lives in the URL (`?meus=1`), not in browser storage.** The agenda's other state (`?data=`, `?view=`, `?consultaId=`) already survives a reload through the query string (static export rule, skill `front-brain`), so the switch follows the same rule: a reload, a shared link and the back button keep it, no `localStorage` try/catch is needed, and on a shared reception computer one doctor's choice never leaks into the next user's session. It is honoured only when the viewer `can_filter_own`; for anyone else `?meus=1` is ignored and dropped from the URL on the next write (a receptionist's `mine=true` would be a 422).
16. **The viewer is read before the first range, and after a clinic switch.** `start()` awaits `GET /viewer` then loads the range, so a manager's `?meus=1` is applied on the first read. Any failure (network, 5xx, 404 on a backend without the route) keeps `CLINIC_VIEWER` (no switch, create sends no doctor) and the range still loads — the server enforces the real rule regardless. An unknown `agenda_scope` reads as `"own"`.
17. **Creating for a restricted doctor:** the controller adds his `professional_id` to `POST /appointments` / `POST /blocks`; the sheets show "Profissional: <nome>" read-only. A clinic-wide viewer's create keeps today's form (no doctor field, nothing sent); choosing a doctor when the reception books is a later improvement (the backend already accepts it).
18. **Hidden appointments:** the server answers 404 for anything the viewer may not see, byte-identical to a nonexistent id. The front never guesses why: a deep link that is not in the loaded range says "A consulta do link não foi encontrada na sua agenda."; an action answered 404 says "Esta consulta não foi encontrada na sua agenda. A agenda foi atualizada." and reloads.
19. **The drawer shows the doctor** ("Profissional") when the event carries it — useful in "Todos" for a manager; a restricted doctor only ever sees his own name there.

## Global Constraints

- The 2026-10-09 spec is binding: action matrix, paid-notice rule, notice per action, colour from status. Wire names, codes and messages are R7's / R4's, copied under "Consumes" — never invented.
- UI text in **Portuguese** (sentence case, "paciente"/"clínica"), code, identifiers and comments in **English**.
- Hand-written CSS only; reuse the existing vocabulary (`btn`, `badge`, `agm-*`, `cal-*`, `agenda-*`, `contexto-*`) and tokens; no Tailwind, no new dependency, no inline hex outside `app/styles/tokens.css`. New colour tokens go in **both** the `[data-theme="light"]` and `[data-theme="dark"]` blocks.
- **Colour is never the only signal:** every state has a text label and an icon/tick glyph; the accessible name of a slot/badge/action says it in words (skill `custom-control-accessible-name`: custom controls take a required `label`; checkboxes get a `<label htmlFor>`; visible text is inside the accessible name).
- Motion: **no** new animation or transition; the global `@media (prefers-reduced-motion: reduce)` in `app/styles/base.css` stays the only motion rule (pinned by a CSS test, Task 2).
- Labels derived from dates come from the data's own timestamps (skill `date-derived-ui-labels`); the time gate uses the injected clock (`now`), never `new Date()` inside a component or pure function.
- Static export rules (skill `front-brain`): no new route, no `fetch` outside `lib/agenda/secretaria-hub-agenda.ts` / `lib/real/secretaria-hub.ts` (`hubFetch`: hub token, 401 re-mint, `HubApiError` with `.status`, `.code`, `.message`, `.detail`).
- Every new wire key is **optional** on read: an older backend (no `status`/`confirmation_count`/`display_state`/`attention`/`reminders`, no `patient_notice`, no config keys) must render and behave as today. Unknown `display_state`/`kind`/`answer`/`patient_notice` strings are ignored, never guessed.
- Terminal statuses keep their own look: `cancelado`, `compareceu`, `faltou` (and `bloqueio`) are coloured from the status, never from `confirmation_count`/`display_state`; Google-only events (no `appointment_id`) and blocks map exactly as before and offer no action.
- Display cap: ticks never exceed **two** (`confirmation_count` clamped to 0–2 on read).
- Never bill silently: `notify_outside_window: true` is sent only after the clinic chose "Enviar mesmo assim" / ticked the paid notice in this action, or when the clinic flag is on.
- Gates (PowerShell, from the worktree; `CLAUDE.md` of the repo): `npm run typecheck`, `npm test`, `npm run build`. **Never** `npm run lint` (ESLint is not installed; it blocks on an interactive prompt) and never `npx tsc` (wrong package on this machine). A single file: `npx vitest run <path>`.
- `git add` only the files named in each task (never `git add -A`; parallel sessions share this machine). Commit locally; **no push, no merge, no deploy** (see "Deploy" at the end). After each Write/Edit, `git diff --stat` must show only the touched lines (a whole-file diff means line endings flipped — restore and redo), and `LC_ALL=C.UTF-8 grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]' <file>` (Git Bash) must print nothing for every touched file.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Agenda visibility is the server's (spec §5.A); the front mirrors it, never widens it.** A viewer with `agenda_scope: "own"` is never offered the "Todos / Só os meus" switch, another doctor's name in a doctor field, or a create on another doctor's agenda; `mine=true` is sent only when `can_filter_own`; a 404 on an appointment is "not found in your agenda", never "it exists but you cannot see it".
- The "Só os meus" choice is URL state (`?meus=1`), like `?data=`/`?view=`/`?consultaId=`; no browser storage is added.

## Review Focus

Most likely to bite first. Each line names the task whose tests pin it.

1. **The clock gate.** Before the start: no "Faltou"; after the start: no Liberar / Confirmar / Cancelar / Enviar mensagem; closed: no button at all; unknown clock: only Compareceu and Editar/Remarcar. A backend `409 no_show_before_start` (clock skew) is shown in the drawer and nothing changes locally. Task 3 (`drawerActions`, drawer markup), Task 5 (controller).
2. **Terminal colour from status.** `{status: "no_show" | "attended" | "cancelled", display_state: "unconfirmed"}` paints faltou / compareceu / cancelado, never the neutral or red look, in the grid and the drawer. Task 1, Task 2, Task 3.
3. **Paid notice asked exactly when needed.** `patient_channel: "whatsapp"` outside 24 h + flag off → asked, with the cost; inside 24 h, a Portal patient (`"brain_message"`, even when the preview carries a WhatsApp link), no patient record / no channel key, or flag on → not asked; "Não perguntar novamente" saves the flag before the action and a failed save sends nothing; a failed preview acts without paying. Task 4 (`decidePaidNotice`), Task 5 (controller), Task 6 (sheets, `reachFor`), Task 9 (edit).
4. **Honest notice after every action.** Each `patient_notice` value maps to its sentence and tone; `whatsapp_outside_window` shows the free link (only `https://wa.me/…`); `null`/unknown claim nothing; a backend without the key keeps today's cancel toast. Task 4 (`actionToast`), Task 5, Task 6 (toast link markup).
5. **Editar/Remarcar sends only what changed** against the pre-filled values (an untouched form sends nothing and the button is disabled; a legacy contact phone without country code does not block an untouched form), and handles `slot_unavailable` (encaixe), `appointment_changed`, `not_live`, `invalid_phone`, `professional_not_allowed`, 404, 502 without losing the form. Task 8, Task 9.
6. **Pix-paid release needs the acknowledgement** (kept): disabled confirm until ticked, nothing sent without `acknowledge_retention: true`, backend `retention_ack_required` re-opens the step with its own text; `already_confirmed` needs "Liberar mesmo assim". Tasks 4, 5, 6.
7. **Red → green live after Confirmar**, before the refetch; the refetch then wins; a failure leaves it red. Task 5.
8. **Double clicks send once** (Confirmar, the paid-notice sheet, release, edit). Task 5, Task 8.
9. **Old backend graceful:** no badge/legend/timeline without the keys, no "Lembretes e avisos" section without the config keys, the paid flag and lead never sent unless exposed, no viewer route → today's look (no switch) and the agenda still loads. Tasks 1, 2, 3, 7, 10, 11.
10. **Keyboard / screen-reader names:** badge text, action buttons named by their visible text, every checkbox labelled, the timeline is a named ordered list, the "Todos / Só os meus" group is named and says its pressed state, icons `aria-hidden`; no new motion; tokens in both themes. Tasks 2, 3, 6, 7, 9.
11. **A scoped doctor never sees or acts on another doctor's appointment through the front:** no switch (even with `?meus=1` in the URL), "Nova consulta"/"Bloquear" locked to himself and sending his id, his Editar doctor locked, `403 professional_not_allowed` / `422 no_own_agenda` shown with the backend's sentence, a deep link or action to a hidden appointment answers "não foi encontrada na sua agenda" without opening anything. Task 7, Task 8, Task 9.
12. **Manager toggle:** "Só os meus" sends `mine=true`, is written as `?meus=1` and restored on reload; "Todos" sends `mine=false` and drops it; the first read already honours `?meus=1` (viewer read first). Task 7.
13. **Secretary has no toggle:** `?meus=1` in her URL never reaches the server (it would be 422 `no_own_agenda`) and `setMine` is a no-op. Task 7.
14. **Viewer per clinic:** after a clinic switch the old clinic's viewer never applies (reset, re-read for the new clinic before its range). Task 7.
15. **Legacy extra-reminder value** outside `1500..20160` is shown but not re-sent while untouched, so saving Contexto still works. Task 10.

## Consumes — backend contract (exact shapes, and what is assumed)

**From R1 (live; used verbatim).** `GET /tenants/me/calendar/events?start=&end=` items gain, all optional/nullable and `null` for events with no local `Appointment` (blocks, Google-only):

```ts
status?: "scheduled" | "confirmed" | "rescheduled" | "cancelled" | "attended" | "no_show" | null;
confirmation_count?: number | null;          // 0..2
display_state?: string | null;               // "unconfirmed" | "confirmed" | "confirmed_twice" | "attention" (terminal statuses are always "unconfirmed")
attention?: boolean | null;                  // === (display_state == "attention")
reminders?: Array<{
  kind: string;                              // "custom" | "day" | "hour" | "chat" | R7: "staff_confirm" | "staff_edit"
  status: string;                            // "pending" | "sending" | "sent" | "failed" | "skipped" | "cancelled"
  due_at: string;                            // aware UTC ISO
  sent_at?: string | null; answered_at?: string | null;
  answer?: string | null;                    // "confirm" | "cancel" | "other"
  warned_at?: string | null; warn_kind?: string | null;   // "unconfirmed" | "delivery_failed"
}> | null;                                   // only rows of the appointment's CURRENT start, ordered by due_at
```

On `origin/main` (R1) the read carries **no** phone, professional, service or attendee name (verified in `schemas/calendar.py::CalendarEventRead`); R7 Task 14 adds them (below). The appointment's WhatsApp link still comes from `cancel-preview`.

**From R7 (`2026-10-09-lembretes-r7-avisos-acoes-clinica.md`, "Produces for R5", read 2026-10-09; used verbatim).** All paths under `/tenants/me`, hub bearer token. Structured errors are `{"detail": {code, message, ...}}` (→ `HubApiError.code`, `.message` = the backend's Portuguese `message`, `.detail` = the object). A foreign or malformed appointment id is **404** `{"detail":"Appointment not found"}` on every route.

```text
patient_notice  (null = no notice was due)
  whatsapp_sent | whatsapp_queued | portal_chat | portal_chat_email      -> success
  whatsapp_outside_window (+ whatsapp_link "https://wa.me/<digits>" | null) | no_channel  -> warning (NOT notified)
  queue_unavailable | notice_failed                                      -> danger (NOT notified)
  any unknown future value                                               -> success-neutral

AppointmentActionRead (PATCH status, POST edit, POST cancel) =
  AppointmentRead {id, tenant_id, patient_id, conversation_id, google_event_id, google_event_link, appointment_type,
                   start_at, end_at, phone, status, confirmation_count, created_at, updated_at, deposit_status, deposit_outcome}
  + patient_notice: string | null + whatsapp_link: string | null (non-null only with whatsapp_outside_window)

GET  /tenants/me/config                      -> adds "paid_notices_auto_approved": bool
PUT  /tenants/me/configuration  {"tenant": {"paid_notices_auto_approved": true|false}}  -> 200 HubConfigurationRead (tenant echoes it)
     absent = unchanged; null = 422

GET  /calendar/appointments/{id}/cancel-preview -> {inside_window, professional_name, template_cost_brl, cost_is_estimate, whatsapp_link}
     ask "avisar pago?" only when inside_window is false AND paid_notices_auto_approved is false; meaningless for a Portal patient

PATCH /calendar/appointments/{id}/status  {"status": ..., "notify_outside_window": false}
  200 AppointmentActionRead; confirmed -> card code, or null (already sent for this time / already started);
      attended -> post-consult code, or null (already sent); other statuses -> null
  409 {"code":"no_show_before_start","message":"Só é possível marcar falta depois do horário da consulta.","status":"<current>","start_at":"<ISO UTC>"}
  409 {"code":"not_live","message":"Esta consulta já foi cancelada ou encerrada.","status":"<current>"}
  422 pydantic (unknown status)

POST /calendar/appointments/{id}/edit
  {"start_at": aware ISO, "service": str, "professional_id": uuid, "insurance": str|null, "attendee_name": str|null,
   "phone": str|null, "notify_outside_window": false, "allow_overlap": false}   # all optional, >= 1 editable field, extra keys 422
  200 AppointmentActionRead (status "rescheduled" + confirmation_count 0 when date/time changed; patient_notice always a code)
  409 not_live | slot_unavailable ("Este horário não está livre na agenda.") | appointment_changed | calendar_unresolved
  422 not_a_patient_appointment | nothing_changed | start_in_past | unknown_professional | service_not_offered
      | invalid_phone ("Informe o telefone com DDI e DDD, por exemplo 5511988887777.") | plain "Google Calendar not connected. …"
  502 calendar_unavailable (Google refused: nothing changed, safe to retry)
  POST …/reschedule keeps working until R5 migrates (R5 stops calling it)

POST /calendar/appointments/{id}/cancel  {"confirm": true, "justification": null|str, "notify_outside_window": false}
  200 AppointmentActionRead: WhatsApp -> whatsapp_queued | whatsapp_outside_window (+link); Portal -> portal_chat | portal_chat_email;
      no number -> no_channel; queue down -> queue_unavailable. Errors unchanged (409 plain "Appointment already cancelled").

POST /calendar/appointments/{id}/release (R4)  body {acknowledge_retention, release_confirmed, notify_outside_window, justification}
  200 AppointmentReleaseRead (+ patient_notice, + whatsapp_link with whatsapp_outside_window); notify_outside_window OR-ed with the flag
  409 retention_ack_required {message, deposit_outcome, amount_cents} | not_live {status} | already_confirmed {confirmation_count}
      | calendar_unresolved; 422 not_a_patient_appointment | plain "not connected"; 502 calendar_unavailable

POST /calendar/appointments/{id}/message (R4)  {"text": 1..1000 trimmed, "notify_outside_window": false}
  200 {delivery: whatsapp_text|whatsapp_template|portal_chat, email_nudge: null|sent|no_email|not_sent, message_id}
  409 outside_window_not_authorised {message, template_cost_brl, whatsapp_link}  (not raised when the clinic flag is on)
  422 no_channel | 502 delivery_failed | 422 pydantic blank/too long

GET /calendar/events — reminders[].kind may be "staff_confirm" ("Confirmação da clínica") or "staff_edit" ("Alteração da clínica"),
  born status "sent", never warned, carrying the patient's answer.
```

**From R7, second round (Tasks 13–16 + the owner's "create on his own agenda" decision; "Produces for R5", read 2026-10-09; used verbatim).**

```text
GET /calendar/viewer  (call once when the agenda opens; 401 as any hub route)
  200 {"agenda_scope": "clinic", "professional_id": "<uuid>", "professional_name": "Dra. Ana", "can_filter_own": true}
  agenda_scope: "clinic" (every doctor) | "own" (only professional_id's appointments; with professional_id null sees none)
  can_filter_own = agenda_scope == "clinic" and a linked professional -> show "Todos / Só os meus" only then.
  Never show other doctors' filters/labels to "own".

GET /calendar/events?start=&end=&mine=true|false   (mine optional, default false)
  mine=true with can_filter_own -> only that professional's appointments ("Só os meus"); mine=false -> everything
  "own" viewer: always only his own, whatever mine says (no 4xx)
  mine=true for a clinic viewer without a professional (a receptionist) -> 422 {"code":"no_own_agenda","message":"Seu usuário não está ligado a um profissional da clínica."}
  narrowed ("own" or mine=true): Google-only events and appointments without a doctor are OMITTED, not anonymised
  each item gains six keys (all null for an event with no local appointment) — the Editar/Remarcar pre-fill:
    professional_id    str (uuid) | null   the appointment's doctor in THIS clinic
    professional_name  str | null
    service            str | null          stored service name (a hub block reads "Bloqueado") -> edit's service
    attendee_name      str | null          who the appointment is for (null = the patient themself) -> edit's attendee_name
    phone              str | null          the appointment's CONTACT phone, digits (never the patient's identity) -> edit's phone
    patient_channel    "whatsapp" | "brain_message" | null   the patient's own channel; null = no patient record
                                           (a block or a phone-only booking: only cancel can still reach a WhatsApp number)
  insurance (already there) pre-fills edit's insurance; start pre-fills start_at.

Every route that takes an appointment id (cancel-preview, cancel, release, message, reschedule, status, edit):
  "own" viewer + another doctor's appointment / an appointment without a doctor / any appointment when he has no professional
  -> 404 {"detail":"Appointment not found"}, byte-identical to a nonexistent id; nothing touched or sent. Clinic viewers: unchanged.

POST /calendar/appointments  and  POST /calendar/blocks   body gains optional "professional_id": "<uuid>" | null; 201 AppointmentRead unchanged
  "own" viewer: absent/null -> his own agenda; his own id -> same; any other id ->
      403 {"detail":{"code":"professional_not_allowed","message":"Você só pode marcar consultas e bloqueios na sua própria agenda."}}
    "own" with professional_id null in /viewer -> 422 {"detail":{"code":"no_own_agenda","message":"Seu usuário não está ligado a um profissional da clínica."}}
  "clinic" viewer: absent/null -> no doctor (today); an id -> active professional of this clinic, else
      422 {"detail":{"code":"unknown_professional","message":"Este profissional não pertence a esta clínica."}}
  Front: for "own", preselect and lock the doctor to /viewer's professional_id/professional_name and send it;
         for "own" with professional_id null, disable both buttons.

POST /calendar/appointments/{id}/edit   "own" viewer sending a professional_id other than his own ->
  403 {"detail":{"code":"professional_not_allowed","message":"Você só pode manter a consulta na sua própria agenda. Peça à recepção para trocar o médico."}}
  nothing changed; in the form, lock "Médico" to himself for "own".

Clinic configuration — reminders (R7 Task 16)
  GET /tenants/me/config (and HubConfigurationRead.tenant) adds "reminders_v2_enabled": bool (read-only) and "reminder_extra_lead_minutes": int | null
  PUT /tenants/me/configuration {"tenant": {"reminder_extra_lead_minutes": 2880}}: integer 1500..20160; null = no extra reminder;
      absent = unchanged; out of range / not an integer -> 422 pydantic, nothing saved; reminders_v2_enabled in a PUT body is ignored
  Effect: pending extra reminders of future appointments move to the new time (or are cancelled when off / already past);
      the agenda's reminders[] shows the new due_at on the next GET /events.
```

R4's warning e-mail links to the agenda as `DOCTOR_AGENDA_URL?consulta=<appointment_id>&data=<YYYY-MM-DD>` (`workers/confirmation_warnings.py`, the day in the clinic's timezone). The agenda reads `?consultaId=` and `?data=` today, so **Task 5 adds `consulta` as an accepted alias** and pins that `?consulta=<id>&data=<day>` loads that week and opens the drawer.

**Existing lists the Editar/Remarcar sheet uses (unchanged endpoints, `lib/real/secretaria-hub.ts`):** `getProfessionals(session)` → `ProfessionalWire[]` (`id`, `name`, `is_active`, …) and `getServices(session)` → `ServiceWire[]` (`name`, `is_active`, `professional_ids` = active professionals offering it).

**Reminders configuration — owned by R7 Task 16** (shape above; `origin/main` does not expose the two keys yet, R7 adds them). Tasks 10–11 still render each half only when its key is present, so a front deployed before R7 hides it instead of writing nowhere; on the target backend both halves show.

**Not in this plan (owner's decision 2026-10-09, spec §5.D):** the conversations console visibility (doctor sees only his own patients' conversations, manager toggles, secretary sees all) is a separate future plan; R5 does not touch the staff chat (`app/chat/page.tsx` → `components/console/ChatScreen.tsx`).

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `lib/agenda/types.ts` | Modify | `Appt.confirmation`, `Appt.reminders`, `Appt.professionalId/professionalName/service/attendeeName/patientChannel`, their types |
| `lib/agenda/confirmation.ts` | Create | wire→`Appt` status/confirmation mapping, `confirmationLook`, legend, reminder timeline, `applyStaffConfirmation` (pure) |
| `lib/agenda/hub-mapping.ts` | Modify | thread status + confirmation fields and the six R7 facts (`mapAppointmentFacts`) through `mapHubEventToAppt` |
| `lib/agenda/viewer.ts` | Create | who is looking: `AgendaViewer`, `CLINIC_VIEWER`, `viewerFromWire`, `lockedProfessional`, `canCreate`, `wantsOwnOnly`, copy (Task 7) |
| `components/agenda/Modals.tsx` | Modify | `NewApptModal`/`BlockModal`: optional read-only locked doctor line (Task 7) |
| `lib/agenda/actions.ts` | Create | the clock-gated action matrix (`actionPhase`, `drawerActions`, labels) |
| `lib/agenda/notice.ts` | Create | `Toast` type, `patient_notice` → sentence/tone (`actionToast`), `safeWaLink`, `decidePaidNotice` |
| `lib/agenda/release.ts` | Create | Pix-retention gate (`buildReleaseBody`), release/message copy, message suggestion/validation |
| `lib/agenda/edit.ts` | Create | Editar/Remarcar form, changed-fields payload, issues, options, error mapping |
| `lib/agenda/secretaria-hub-agenda.ts` | Modify | new event keys (R1 + the six R7 facts); status payload; release / message / edit calls; action response type; paid flag get/approve; `getAgendaViewer`, `mine` on `listCalendarEvents`, `professional_id` on create/block payloads; drop `rescheduleAppointment` |
| `lib/agenda/modal-forms.ts` | Modify | drop `STATUS_ACTIONS` and the reschedule helpers; `reachFor` reads `patientChannel`; `cancelWillNotify("portal")` is true |
| `lib/agenda/cancel-preview.ts` | Modify | Portal copy (told in the chat), paid-notice copy, drop the reschedule warning |
| `lib/agenda/screen.ts` | Modify | modals (`paid_notice`, `release`, `message`, `edit`), `paidAuto`, live flip, controller methods, `AgendaHub`, `?consulta=` alias; `viewer`/`mine` state, `?meus=1`, `setMine`, viewer read before the range, create sends the locked doctor, 404 handling |
| `lib/real/secretaria-hub.ts` | Modify | `TenantConfigWire`/`Payload`: `paid_notices_auto_approved`, the two reminder keys |
| `app/styles/tokens.css` | Modify | `--st-neutral-*`, `--st-confirm2-*`, `--st-attn-*` in both themes |
| `components/agenda/ConfirmationBadge.tsx`, `confirmation.css` | Create | mark, badge, legend |
| `components/agenda/CalendarViews.tsx`, `calendar-views.css` | Modify | tone class, marks, accessible names, month summary |
| `components/agenda/Drawer.tsx` | Rewrite | badge, timeline, clock-gated actions |
| `components/agenda/PaidNoticeSheet.tsx`, `ReleaseCard.tsx`, `MessageModal.tsx`, `EditModal.tsx` | Create | the four new sheets |
| `components/agenda/CancelCard.tsx` | Modify | flag-aware notice block with "Não perguntar novamente"; remember flag on confirm |
| `components/agenda/RescheduleModal.tsx` + its test | Delete | replaced by `EditModal` |
| `components/agenda/agenda-modals.css`, `agenda-screen.css` | Modify | timeline, actions, sheets, toast link |
| `components/agenda/AgendaView.tsx`, `AgendaScreen.tsx` | Modify | legend, new sheets, toast link, "Todos / Só os meus" group, create buttons per viewer, HUB wiring |
| `lib/contexto/reminders.ts` | Create | lead presets: `formatLead`, `leadOptions`, `parseLead` |
| `lib/contexto/types.ts`, `hub-mapping.ts`, `snapshot.ts`, `screen.ts`, `toc.ts` | Modify | `Reminders` slice (lead + paid flag), hydration, payload, dirty detection, reducer actions, TOC label |
| `components/contexto/ReminderSection.tsx` | Create | "Lembretes e avisos" |
| `components/contexto/ContextoView.tsx`, `PostConsultSection.tsx` | Modify | `rem` section (visible only when exposed); post-consult hint |
| `docs/CHECKPOINT_brain_message_lembretes_agenda.md` | Create | state, what went where, pending items |
| Tests (listed per task) | | `lib/agenda/__tests__/*`, `components/agenda/__tests__/*`, `lib/contexto/__tests__/*`, `components/contexto/__tests__/*` |

---

## Task 1: Wire types, mapping and the pure confirmation logic

**Files:**
- Modify: `lib/agenda/types.ts` (after `ApptDeposit`; end of `Appt`)
- Modify: `lib/agenda/secretaria-hub-agenda.ts` (`CalendarEventWire`, new `CalendarReminderWire`)
- Create: `lib/agenda/confirmation.ts`
- Modify: `lib/agenda/hub-mapping.ts` (`mapHubEventToAppt`)
- Test: `lib/agenda/__tests__/confirmation.test.ts`

**Interfaces:**
- Consumes: R1 event keys, R7's `staff_confirm` / `staff_edit` reminder kinds and R7's six event keys (see "Consumes").
- Produces (later tasks rely on these exact names):
  - `type PatientChannel = "whatsapp" | "brain_message"`; `Appt.professionalId?: string`, `Appt.professionalName?: string`, `Appt.service?: string`, `Appt.attendeeName?: string`, `Appt.patientChannel?: PatientChannel`; the wire's contact `phone` lands in the existing `Appt.phone` (digits). All ABSENT (no key) when the backend sent nothing usable. `hub-mapping.ts::mapAppointmentFacts(e: CalendarEventWire): Pick<Appt, "professionalId" | "professionalName" | "service" | "attendeeName" | "phone" | "patientChannel">`. `CalendarEventWire` gains `professional_id?`, `professional_name?`, `service?`, `attendee_name?`, `phone?` (`string | null`) and `patient_channel?: string | null`.
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

  it("labels the clinic's own cards (R7 staff_confirm / staff_edit) and the patient's answer to them", () => {
    const out = reminderTimeline([
      r({ kind: "staff_confirm", sentAt: at(10), answeredAt: at(10, 5), answer: "confirm" }),
      r({ kind: "staff_edit", sentAt: at(11) }),
    ]);
    expect(out.map((e) => e.text)).toEqual(["Confirmação da clínica enviada", "Paciente confirmou presença", "Alteração da clínica enviada"]);
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

  it("a terminal appointment takes its look from STATUS, never from display_state (the backend reports 'unconfirmed' for them)", () => {
    for (const [status, local] of [["no_show", "faltou"], ["attended", "compareceu"], ["cancelled", "cancelado"]] as const) {
      const a = mapHubEventToAppt(ev({ status, confirmation_count: 0, display_state: "unconfirmed", attention: false }));
      expect(a?.status).toBe(local);
      expect(confirmationLook(a as Appt)).toBeNull();
    }
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

describe("R7's six facts (doctor, service, attendee, contact phone, channel)", () => {
  const facts = {
    professional_id: "pro-1",
    professional_name: "Dra. Ana",
    service: "Retorno",
    attendee_name: "João Pedro",
    phone: "5511988887777",
    patient_channel: "brain_message",
  };

  it("maps every fact onto the appointment", () => {
    const a = mapHubEventToAppt(ev(facts));
    expect(a).toMatchObject({
      professionalId: "pro-1",
      professionalName: "Dra. Ana",
      service: "Retorno",
      attendeeName: "João Pedro",
      phone: "5511988887777",
      patientChannel: "brain_message",
    });
  });

  it("adds NO key for absent, null, blank or unknown values (older backend keeps today's shape)", () => {
    const a = mapHubEventToAppt(ev({ professional_id: null, service: "  ", attendee_name: null, phone: "", patient_channel: "sms" }));
    for (const k of ["professionalId", "professionalName", "service", "attendeeName", "phone", "patientChannel"]) {
      expect(k in (a as object)).toBe(false);
    }
    expect(mapAppointmentFacts(ev())).toEqual({});
  });

  it("a WhatsApp patient reads as whatsapp", () => {
    expect(mapAppointmentFacts(ev({ patient_channel: "whatsapp" }))).toEqual({ patientChannel: "whatsapp" });
  });

  it("a Google-only event never carries facts, even with stray keys", () => {
    expect(mapAppointmentFacts(ev({ ...facts, appointment_id: null }))).toEqual({});
  });

  it("a block keeps its own shape (the facts are for appointments)", () => {
    const base = ev({ summary: "Bloqueado: Almoço" });
    expect(mapHubEventToAppt({ ...base, ...facts })).toEqual(mapHubEventToAppt(base));
  });
});
```

Add `mapAppointmentFacts` to the import from `"../hub-mapping"` at the top of this test file (it becomes `import { mapAppointmentFacts, mapHubEventToAppt } from "../hub-mapping";`).

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
  /**
   * R7's facts about the appointment (the Editar/Remarcar pre-fill), each ABSENT
   * when the backend sent nothing usable. `phone` (above) receives the wire's
   * contact phone: the APPOINTMENT's contact, never the patient's identity.
   */
  professionalId?: string;
  professionalName?: string;
  /** The stored service name (R7 `service`); `type` stays the summary-parsed label. */
  service?: string;
  /** Who the appointment is for; absent = the patient themself. */
  attendeeName?: string;
  /** The patient's own channel; absent = no patient record or an older backend. */
  patientChannel?: PatientChannel;
```

and, next to `ApptReminder` (after it), add:

```ts
/** The patient's own channel (secretarIA `Patient.channel`). */
export type PatientChannel = "whatsapp" | "brain_message";
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
  /** R7 Task 14 — the Editar/Remarcar pre-fill. Absent on an older backend; null without a local appointment. */
  professional_id?: string | null;
  professional_name?: string | null;
  service?: string | null;
  attendee_name?: string | null;
  /** The appointment's CONTACT phone, digits. */
  phone?: string | null;
  /** "whatsapp" | "brain_message" — a plain string on purpose (unknown values are ignored). */
  patient_channel?: string | null;
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
  // R7: the cards the CLINIC's actions put in front of the patient (born "sent", never warned).
  staff_confirm: "Confirmação da clínica",
  staff_edit: "Alteração da clínica",
};

/** Feminine nouns read "enviada"; every other kind keeps "<label> enviado". */
const SENT_TEXT: Readonly<Record<string, string>> = {
  chat: "Mensagem de abertura da conversa enviada",
  staff_confirm: "Confirmação da clínica enviada",
  staff_edit: "Alteração da clínica enviada",
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
    if (r.sentAt) raw.push({ at: r.sentAt, text: own(SENT_TEXT, r.kind) ?? `${label} enviado`, tone: "info" });
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

and after `...mapDepositField(e),` add `...mapConfirmationFields(e),` and `...mapAppointmentFacts(e),`. Do **not** touch the block branch.

Add `type PatientChannel` to the type import from `./types` in `hub-mapping.ts`, and add, right after `mapDepositField`:

```ts
// R7 Task 14: what Editar/Remarcar pre-fills and who the patient's channel is.
// Fail open: a key the backend did not send (older backend), a null, a blank
// string or an unknown channel adds NO key. Only an event backed by a local
// appointment carries facts (a Google-only event's are ignored).
const fact = (v: unknown): string | undefined => (typeof v === "string" && v.trim() !== "" ? v.trim() : undefined);
const CHANNELS: readonly PatientChannel[] = ["whatsapp", "brain_message"];

export function mapAppointmentFacts(
  e: CalendarEventWire,
): Pick<Appt, "professionalId" | "professionalName" | "service" | "attendeeName" | "phone" | "patientChannel"> {
  const out: Pick<Appt, "professionalId" | "professionalName" | "service" | "attendeeName" | "phone" | "patientChannel"> = {};
  if (typeof e.appointment_id !== "string" || e.appointment_id === "") return out;
  const professionalId = fact(e.professional_id);
  if (professionalId) out.professionalId = professionalId;
  const professionalName = fact(e.professional_name);
  if (professionalName) out.professionalName = professionalName;
  const service = fact(e.service);
  if (service) out.service = service;
  const attendeeName = fact(e.attendee_name);
  if (attendeeName) out.attendeeName = attendeeName;
  const phone = fact(e.phone);
  if (phone) out.phone = phone;
  const channel = fact(e.patient_channel);
  if (channel && (CHANNELS as readonly string[]).includes(channel)) out.patientChannel = channel as PatientChannel;
  return out;
}
```

- [ ] **Step 6: Run the tests, then the whole suite and the type check**

Run: `npx vitest run lib/agenda/__tests__/confirmation.test.ts` — Expected: PASS.
Run: `npm test` — Expected: all green (existing agenda tests still see `status: "agendado"` for events without status, and no new key for events without the R7 facts).
Run: `npm run typecheck` — Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add lib/agenda/types.ts lib/agenda/secretaria-hub-agenda.ts lib/agenda/confirmation.ts lib/agenda/hub-mapping.ts lib/agenda/__tests__/confirmation.test.ts
git commit -m "feat(agenda): map confirmation state, status and reminders from the events read (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
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

  it("a terminal status keeps its status colour although the backend's display_state says 'unconfirmed'", () => {
    for (const [status, tone] of [["faltou", "miss"], ["compareceu", "attend"], ["cancelado", "block"]] as const) {
      const html = week([appt("unconfirmed", { status })]);
      expect(html).toContain(`cal-appt--${tone}`);
      expect(html).not.toContain("cal-appt--neutral");
    }
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
Run: `npm run typecheck` — Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add app/styles/tokens.css components/agenda/confirmation.css components/agenda/ConfirmationBadge.tsx components/agenda/calendar-views.css components/agenda/CalendarViews.tsx components/agenda/AgendaView.tsx components/agenda/__tests__/confirmation-views.test.tsx components/agenda/__tests__/confirmation-css.test.ts components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): confirmation tones, ticks, marks and legend on the grid (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3: Drawer — badge, reminder timeline and the clock-gated action matrix

**Files:**
- Create: `lib/agenda/actions.ts`
- Rewrite: `components/agenda/Drawer.tsx`
- Modify: `components/agenda/agenda-modals.css` (append), `lib/agenda/modal-forms.ts` (drop `STATUS_ACTIONS`/`StatusAction`), `components/agenda/AgendaView.tsx` (the `<Drawer>` props only)
- Test: `lib/agenda/__tests__/actions.test.ts` (new), `components/agenda/__tests__/drawer.test.tsx` (rework), `lib/agenda/__tests__/modal-forms.test.ts` (drop one test)

**Interfaces:**
- Consumes: `confirmationLook`, `reminderTimeline` (Task 1); `ConfirmationBadge` (Task 2); `slotIsoRangeFromDateKey` (`hub-mapping.ts`); `StatusActionWire`, `WIRE_STATUS_LABEL` (`modal-forms.ts`, kept).
- Produces (later tasks rely on these exact names):
  - `lib/agenda/actions.ts`: `type DrawerActionId = "release" | "confirm" | "attended" | "no_show" | "edit" | "cancel" | "message"`; `type ActionPhase = "before_start" | "after_start" | "unknown_time" | "terminal" | "unavailable"`; `isTerminal(appt: Pick<Appt,"status">, knownStatus: AppointmentStatusWire | null): boolean`; `apptStartMs(appt: Pick<Appt,"date"|"start"|"dur">): number`; `actionPhase(appt: Appt, knownStatus: AppointmentStatusWire | null, now: Date | null): ActionPhase`; `drawerActions(appt, knownStatus, now): readonly DrawerActionId[]`; `ACTION_LABEL: Record<DrawerActionId, string>`; `STATUS_OF: { confirm: "confirmed"; attended: "attended"; no_show: "no_show" }`; copy `NO_SHOW_NOTE`, `AFTER_START_HINT`, `TERMINAL_HINT`, `UNKNOWN_TIME_HINT`.
  - `DrawerProps` = `{ open; appt; onClose; now?: Date | null; onCancel?: (appt) => void; onEdit?: (appt) => void; onRelease?: (appt) => void; onMessage?: (appt) => void; onStatus?: (appt, status: StatusActionWire) => void; knownStatus?; statusPending?; statusError? }` — **`onReschedule` is gone**. Buttons are named by their visible text: `Liberar horário`, `Confirmar`, `Compareceu`, `Faltou`, `Editar/Remarcar`, `Cancelar consulta`, `Enviar mensagem`, inside `role="group" aria-label="Ações da consulta"`.

- [ ] **Step 1: Write the failing tests**

Create `lib/agenda/__tests__/actions.test.ts`:

```ts
// The owner's action matrix (spec 2026-10-09 §1): what the drawer offers depends on the clock.
import { describe, expect, it } from "vitest";

import { ACTION_LABEL, actionPhase, apptStartMs, drawerActions, isTerminal } from "../actions";
import type { Appt } from "../types";

// The slot starts 2026-09-29 09:00 local.
const appt = (over: Partial<Appt> = {}): Appt => ({
  id: "hub-1", date: "2026-09-29", day: 2, start: 9 * 60, dur: 30,
  patient: "Maria Souza", type: "Consulta", status: "agendado", appointmentId: "ap-1", ...over,
});
const BEFORE = new Date(2026, 8, 29, 8, 59);
const AT_START = new Date(2026, 8, 29, 9, 0);
const AFTER = new Date(2026, 8, 29, 9, 31);

describe("actionPhase / drawerActions", () => {
  it("before the start: Liberar, Confirmar, Compareceu, Editar/Remarcar, Cancelar, Mensagem — never Faltou", () => {
    expect(actionPhase(appt(), null, BEFORE)).toBe("before_start");
    expect(drawerActions(appt(), null, BEFORE)).toEqual(["release", "confirm", "attended", "edit", "cancel", "message"]);
    expect(drawerActions(appt(), null, BEFORE)).not.toContain("no_show");
  });

  it("from the start on (start <= now): Compareceu, Faltou, Editar/Remarcar only", () => {
    expect(actionPhase(appt(), null, AT_START)).toBe("after_start");
    expect(drawerActions(appt(), null, AT_START)).toEqual(["attended", "no_show", "edit"]);
    expect(drawerActions(appt(), null, AFTER)).toEqual(["attended", "no_show", "edit"]);
  });

  it("a confirmed (or rescheduled) appointment is still live", () => {
    expect(drawerActions(appt({ status: "confirmou" }), null, BEFORE)).toContain("release");
    expect(drawerActions(appt(), "rescheduled", BEFORE)).toContain("confirm");
    expect(drawerActions(appt(), "confirmed", AFTER)).toEqual(["attended", "no_show", "edit"]);
  });

  it("terminal — from the read OR learned from a write — offers nothing, whatever the clock", () => {
    for (const status of ["cancelado", "compareceu", "faltou"] as const) {
      expect(actionPhase(appt({ status }), null, BEFORE)).toBe("terminal");
      expect(drawerActions(appt({ status }), null, AFTER)).toEqual([]);
    }
    for (const known of ["cancelled", "attended", "no_show"] as const) {
      expect(drawerActions(appt(), known, BEFORE)).toEqual([]);
    }
  });

  it("blocks and Google-only events offer nothing", () => {
    expect(actionPhase(appt({ status: "bloqueio" }), null, BEFORE)).toBe("unavailable");
    expect(drawerActions(appt({ appointmentId: null }), null, BEFORE)).toEqual([]);
  });

  it("without a clock yet (before mount) only what is valid in both phases", () => {
    expect(actionPhase(appt(), null, null)).toBe("unknown_time");
    expect(drawerActions(appt(), null, null)).toEqual(["attended", "edit"]);
  });

  it("isTerminal reads both sources; apptStartMs is the slot's own local start", () => {
    expect(isTerminal(appt(), null)).toBe(false);
    expect(isTerminal(appt(), "no_show")).toBe(true);
    expect(isTerminal(appt({ status: "faltou" }), "confirmed")).toBe(true);
    expect(apptStartMs(appt())).toBe(new Date(2026, 8, 29, 9, 0).getTime());
  });

  it("labels every action with the owner's words", () => {
    expect(ACTION_LABEL).toEqual({
      release: "Liberar horário",
      confirm: "Confirmar",
      attended: "Compareceu",
      no_show: "Faltou",
      edit: "Editar/Remarcar",
      cancel: "Cancelar consulta",
      message: "Enviar mensagem",
    });
  });
});
```

In `components/agenda/__tests__/drawer.test.tsx`:

(a) Replace the import of `"../Drawer"` and the `render` helper with:

```tsx
import { BLOCK_HINT, CANCELLED_HINT, CANCEL_UNAVAILABLE_HINT, Drawer, type DrawerProps } from "../Drawer";
import { AFTER_START_HINT, NO_SHOW_NOTE, TERMINAL_HINT } from "../../../lib/agenda/actions";
```

```tsx
// The default slot starts 2026-09-29 09:00 local.
const BEFORE = new Date(2026, 8, 29, 8, 0);
const AFTER = new Date(2026, 8, 29, 9, 30);
const render = (a: Appt | null, over: Partial<DrawerProps> = {}) =>
  renderToStaticMarkup(
    <Drawer
      open appt={a} now={BEFORE} onClose={noop}
      onCancel={noop} onEdit={noop} onRelease={noop} onMessage={noop} onStatus={noop}
      {...over}
    />,
  );
```

(b) Delete the `describe` blocks `"Drawer — appointment"`, `"Drawer — cancelled"`, `"Drawer — block"` and `"Drawer — a11y"`, and the last `it` of `"Drawer — convênio e sinal (TASK-014)"` (`"does not disturb the actions: Remarcar and Cancelar stay enabled …"`). Put these blocks in their place (before the `infoRows` helper):

```tsx
const ACTION_NAMES = ["Liberar horário", "Confirmar", "Compareceu", "Faltou", "Editar/Remarcar", "Cancelar consulta", "Enviar mensagem"];
const offered = (html: string) =>
  ACTION_NAMES.filter((n) => html.includes(`<span>${n}</span>`));

describe("Drawer — appointment", () => {
  it("shows identity and info rows", () => {
    const html = render(appt({ phone: "+55 11 99999-8888" }));
    expect(html).toContain("Detalhes da consulta");
    expect(html).toContain("Maria Souza");
    expect(html).toContain("Terça, 29/09 · 09:00–09:30");
    expect(html).toContain("+55 11 99999-8888");
  });

  it("hides the phone row when the slot carries no contact phone", () => {
    const html = render(appt());
    expect(html).not.toContain("Telefone");
  });

  it("names the doctor when the read carried it (R7), and hides the row otherwise", () => {
    expect(render(appt({ professionalName: "Dra. Ana" }))).toMatch(/Profissional<\/div><div class="agm-info-value">Dra\. Ana/);
    expect(render(appt())).not.toContain("Profissional");
  });

  it("before the start: the six actions, in the owner's order, all enabled — and no Faltou", () => {
    const html = render(appt());
    expect(offered(html)).toEqual(["Liberar horário", "Confirmar", "Compareceu", "Editar/Remarcar", "Cancelar consulta", "Enviar mensagem"]);
    for (const n of offered(html)) expect(button(html, n)).not.toContain("disabled");
    expect(html).toContain('role="group" aria-label="Ações da consulta"');
  });

  it("after the start: Compareceu, Faltou, Editar/Remarcar only, with the hint and the deposit note on Faltou", () => {
    const html = render(appt(), { now: AFTER });
    expect(offered(html)).toEqual(["Compareceu", "Faltou", "Editar/Remarcar"]);
    expect(html).toContain(AFTER_START_HINT);
    const noteId = new RegExp(`<p id="([^"]+)" class="agm-hint agm-status-note">${NO_SHOW_NOTE}`).exec(html)?.[1];
    expect(noteId).toBeTruthy();
    expect(button(html, "Faltou")).toContain(`aria-describedby="${noteId}"`);
    expect(button(html, "Compareceu")).not.toContain("aria-describedby");
  });

  it("without a clock yet: Compareceu and Editar/Remarcar only", () => {
    expect(offered(render(appt(), { now: null }))).toEqual(["Compareceu", "Editar/Remarcar"]);
  });

  it("a status write in flight disables the three status buttons and marks the pending one busy", () => {
    const html = render(appt(), { now: AFTER, statusPending: "no_show", statusError: "Falhou" });
    for (const n of ["Compareceu", "Faltou"]) expect(button(html, n)).toContain("disabled");
    expect(button(html, "Faltou")).toContain('aria-busy="true"');
    expect(button(html, "Editar/Remarcar")).not.toContain("disabled");
    expect(html).toMatch(/role="alert"[^>]*>(?:(?!<\/p>).)*Falhou/);
  });

  it("a missing callback disables its button, never a silent no-op", () => {
    const html = render(appt(), { onRelease: undefined, onEdit: undefined, onStatus: undefined, onMessage: undefined, onCancel: undefined });
    for (const n of offered(html)) expect(button(html, n)).toContain("disabled");
  });

  it("without a local appointment id: no action, the Google hint", () => {
    const html = render(appt({ appointmentId: null }));
    expect(offered(html)).toEqual([]);
    expect(html).toContain(CANCEL_UNAVAILABLE_HINT);
  });
});

describe("Drawer — closed appointments", () => {
  it("a cancel learned from a write: no action, the cancelled hint", () => {
    const html = render(appt(), { knownStatus: "cancelled" });
    expect(offered(html)).toEqual([]);
    expect(html).toContain(CANCELLED_HINT);
  });

  it("attended / no-show (read or learned): no action, the closed hint and the status badge", () => {
    for (const status of ["compareceu", "faltou"] as const) {
      const html = render(appt({ status }), { now: AFTER });
      expect(offered(html)).toEqual([]);
      expect(html).toContain(TERMINAL_HINT);
      expect(html).toContain(status === "compareceu" ? "Compareceu" : "Faltou");
    }
    expect(offered(render(appt(), { now: AFTER, knownStatus: "attended" }))).toEqual([]);
  });
});

describe("Drawer — block", () => {
  it("gates on status, not appointmentId: a block with an id offers no action", () => {
    const html = render(appt({ status: "bloqueio", reason: "Almoço", patient: undefined, appointmentId: "ap-9" }));
    expect(html).toContain("Bloqueio");
    expect(html).toContain("Almoço");
    expect(html).toContain(BLOCK_HINT);
    expect(offered(html)).toEqual([]);
  });
});

describe("Drawer — a11y", () => {
  it("names the close button and the actions group", () => {
    const html = render(appt());
    expect(html).toContain('aria-label="Fechar"');
    expect(html).toContain('role="group" aria-label="Ações da consulta"');
  });

  it("renders an empty closed sheet with no appointment", () => {
    expect(render(null, { open: false })).toContain("Detalhes da consulta");
  });
});

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

  it("red: explains the state above the same actions (no separate red-only buttons)", () => {
    const html = render(appt({ confirmation: red }));
    expect(html).toContain("Um lembrete saiu, o prazo passou e o paciente não confirmou.");
    expect(offered(html)).toContain("Liberar horário");
  });

  it("green: the same before-start actions — Liberar is no longer a red-state action", () => {
    expect(offered(render(appt({ confirmation: { state: "confirmed", count: 1 } })))).toContain("Liberar horário");
  });

  it("terminal statuses keep their own look: no confirmation badge, whatever display_state said", () => {
    for (const status of ["cancelado", "compareceu", "faltou"] as const) {
      const html = render(appt({ status, confirmation: { state: "unconfirmed", count: 0 } }));
      expect(html).not.toContain("conf-badge");
      expect(html).not.toContain("Sem confirmação");
    }
    const learned = render(appt({ confirmation: red }), { knownStatus: "attended" });
    expect(learned).not.toContain("Sem confirmação");
    expect(learned).toContain("Compareceu");
  });

  it("a block is untouched by stray confirmation data", () => {
    const html = render(appt({ status: "bloqueio", reason: "Almoço", confirmation: red, reminders }));
    expect(html).toContain(BLOCK_HINT);
    expect(html).not.toContain("Sem confirmação");
    expect(html).not.toContain("Lembretes");
  });

  it("older backend (no confirmation, no reminders): no badge, no timeline", () => {
    const html = render(appt());
    expect(html).not.toContain("Lembretes");
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

(c) In the `"Drawer — convênio e sinal (TASK-014)"` block add, in place of the deleted last `it`:

```tsx
  it("does not disturb the actions: everything stays enabled with convênio and sinal present", () => {
    const html = render(appt({ insurance: "Unimed", deposit: { status: "confirmado_pago", amountCents: 3000 } }));
    expect(button(html, "Editar/Remarcar")).not.toContain("disabled");
    expect(button(html, "Cancelar consulta")).not.toContain("disabled");
  });
```

In `lib/agenda/__tests__/modal-forms.test.ts`: remove `STATUS_ACTIONS,` from the import list and delete the `it("never offers cancelled; no_show carries the deposit note", …)` test (the matrix test above replaces it).

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/actions.test.ts components/agenda/__tests__/drawer.test.tsx`
Expected: FAIL — `Cannot find module '../actions'`; the drawer still renders "Remarcar"/"Confirmou".

- [ ] **Step 3: `lib/agenda/actions.ts`**

```ts
// lib/agenda/actions.ts — which actions the appointment drawer offers, decided by
// the CLOCK (owner's decision 2026-10-09, spec §1), not by the confirmation colour:
//
//   live, before the start   Liberar horário · Confirmar · Compareceu · Editar/Remarcar · Cancelar consulta · Enviar mensagem
//   live, start <= now        Compareceu · Faltou · Editar/Remarcar
//   cancelled/attended/no-show nothing
//
// "Faltou" before the start does not exist (the backend refuses it too: 409
// no_show_before_start). Pure: the clock is a parameter, never read here.

import { slotIsoRangeFromDateKey } from "./hub-mapping";
import type { StatusActionWire } from "./modal-forms";
import type { AppointmentStatusWire } from "./secretaria-hub-agenda";
import type { Appt, ApptStatus } from "./types";

export type DrawerActionId = "release" | "confirm" | "attended" | "no_show" | "edit" | "cancel" | "message";

export type ActionPhase =
  /** A live appointment whose start is still ahead. */
  | "before_start"
  /** A live appointment that has started (start <= now). */
  | "after_start"
  /** No clock yet (static export, before mount): only what is valid in both phases. */
  | "unknown_time"
  /** Cancelled, attended or no-show — from the read or learned from a write. */
  | "terminal"
  /** A block or a Google-only event: nothing can be written. */
  | "unavailable";

const TERMINAL_LOCAL: readonly ApptStatus[] = ["cancelado", "compareceu", "faltou"];
const TERMINAL_WIRE: readonly AppointmentStatusWire[] = ["cancelled", "attended", "no_show"];

export function isTerminal(appt: Pick<Appt, "status">, knownStatus: AppointmentStatusWire | null): boolean {
  return TERMINAL_LOCAL.includes(appt.status) || (knownStatus !== null && TERMINAL_WIRE.includes(knownStatus));
}

/** The slot's start instant: the same browser-local date + minutes the grid draws it at. */
export function apptStartMs(appt: Pick<Appt, "date" | "start" | "dur">): number {
  return Date.parse(slotIsoRangeFromDateKey(appt.date, appt.start, appt.dur).startIso);
}

export function actionPhase(appt: Appt, knownStatus: AppointmentStatusWire | null, now: Date | null): ActionPhase {
  if (appt.status === "bloqueio" || !appt.appointmentId) return "unavailable";
  if (isTerminal(appt, knownStatus)) return "terminal";
  if (!now) return "unknown_time";
  return apptStartMs(appt) <= now.getTime() ? "after_start" : "before_start";
}

const ACTIONS: Readonly<Record<ActionPhase, readonly DrawerActionId[]>> = {
  before_start: ["release", "confirm", "attended", "edit", "cancel", "message"],
  after_start: ["attended", "no_show", "edit"],
  unknown_time: ["attended", "edit"],
  terminal: [],
  unavailable: [],
};

/** The actions to draw, in display order. */
export function drawerActions(
  appt: Appt,
  knownStatus: AppointmentStatusWire | null,
  now: Date | null,
): readonly DrawerActionId[] {
  return ACTIONS[actionPhase(appt, knownStatus, now)];
}

export const ACTION_LABEL: Readonly<Record<DrawerActionId, string>> = {
  release: "Liberar horário",
  confirm: "Confirmar",
  attended: "Compareceu",
  no_show: "Faltou",
  edit: "Editar/Remarcar",
  cancel: "Cancelar consulta",
  message: "Enviar mensagem",
};

/** The three actions that are a PATCH status, and the status each sends. */
export const STATUS_OF = {
  confirm: "confirmed",
  attended: "attended",
  no_show: "no_show",
} as const satisfies Record<"confirm" | "attended" | "no_show", StatusActionWire>;

export const NO_SHOW_NOTE = "Marcar falta pode reter o sinal pago pelo paciente, se houver.";
export const AFTER_START_HINT = "A consulta já começou: registre se o paciente compareceu ou faltou, ou corrija os dados.";
export const TERMINAL_HINT = "Esta consulta está encerrada: não há ações disponíveis.";
export const UNKNOWN_TIME_HINT = "Carregando o horário atual…";
```

- [ ] **Step 4: Drop the old status picker data from `lib/agenda/modal-forms.ts`**

Delete the `StatusAction` type and the `STATUS_ACTIONS` constant (with their comments). Keep `StatusActionWire` and `WIRE_STATUS_LABEL` exactly as they are, and replace the section title comment `// Drawer (R4/R5/R6)` with `// Drawer (the action matrix lives in ./actions.ts)`.

- [ ] **Step 5: CSS**

Append to `components/agenda/agenda-modals.css`:

```css
/* Reminder timeline + clock-gated actions (TASK-032 R5). No motion on purpose. */
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
.agm-actions-grid .agm-action--wide { grid-column: 1 / -1; }
```

- [ ] **Step 6: Rewrite `components/agenda/Drawer.tsx`**

Replace the whole file with:

```tsx
"use client";

// Appointment / block detail — logic ported from secretarIA-frontend
// agenda/drawer.tsx, shell swapped for this repo's Sheet (the only modal
// surface). Presentational: props in, callbacks out; no fetching, no session.
//
// TASK-032 R5 (owner's decisions 2026-10-09):
// - The actions follow the CLOCK (lib/agenda/actions.ts): before the start
//   Liberar horário · Confirmar · Compareceu · Editar/Remarcar · Cancelar ·
//   Enviar mensagem; from the start on Compareceu · Faltou · Editar/Remarcar;
//   a cancelled / attended / no-show appointment offers nothing. "Faltou"
//   never exists before the start.
// - The confirmation state is a badge with TEXT (never colour alone), and the
//   red state explains itself above the same actions.
// - The reminder rows of the current start are a short, named timeline.
// - Terminal statuses keep their own badge; `display_state` never recolours them.
// - TASK-014: "Convênio" and "Sinal" rows come from the events read and are
//   HIDDEN when the field is absent — never a dash or an invented value.

import { useId, type ReactNode } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { Avatar } from "../primitives/Avatar";
import { Badge, type BadgeTone } from "../primitives/Badge";
import { Icon, type IconName } from "../icons";
import { fmtRange } from "../../lib/agenda/types";
import type { Appt, ApptStatus } from "../../lib/agenda/types";
import { dayLabelFromKey } from "../../lib/agenda/calendar-dates";
import { DEPOSIT_ROW_LABEL, INSURANCE_ROW_LABEL, apptDepositView, apptInsuranceView } from "../../lib/agenda/insurance";
import type { ApptDepositView } from "../../lib/agenda/insurance";
import { WIRE_STATUS_LABEL } from "../../lib/agenda/modal-forms";
import type { StatusActionWire } from "../../lib/agenda/modal-forms";
import {
  ACTION_LABEL,
  AFTER_START_HINT,
  NO_SHOW_NOTE,
  STATUS_OF,
  TERMINAL_HINT,
  UNKNOWN_TIME_HINT,
  actionPhase,
  drawerActions,
  isTerminal,
  type DrawerActionId,
} from "../../lib/agenda/actions";
import { confirmationLook, reminderTimeline } from "../../lib/agenda/confirmation";
import type { AppointmentStatusWire } from "../../lib/agenda/secretaria-hub-agenda";
import { initialsOf } from "../../lib/text";
import { ConfirmationBadge } from "./ConfirmationBadge";
import "./agenda-modals.css";

// Verbatim from the source drawer.
export const CANCEL_UNAVAILABLE_HINT =
  "Este evento veio direto do Google Calendar e não tem consulta vinculada — cancele por lá.";
export const BLOCK_HINT = "Gerencie pelo Google Calendar por enquanto.";
export const CANCELLED_HINT = "Esta consulta está cancelada.";

export type DrawerProps = {
  open: boolean;
  appt: Appt | null;
  onClose: () => void;
  /** The screen's clock (lib/useNow.ts): null until mount. Decides the action matrix. */
  now?: Date | null;
  /** Opens the cancel card. Absent → "Cancelar consulta" disabled. */
  onCancel?: (appt: Appt) => void;
  /** Opens the Editar/Remarcar sheet. Absent → disabled. */
  onEdit?: (appt: Appt) => void;
  /** Opens the "Liberar horário" card. Absent → disabled. */
  onRelease?: (appt: Appt) => void;
  /** Opens the "Enviar mensagem" sheet. Absent → disabled. */
  onMessage?: (appt: Appt) => void;
  /** Confirmar / Compareceu / Faltou (PATCH status). Absent → the three are disabled. */
  onStatus?: (appt: Appt, status: StatusActionWire) => void;
  /** Status learned from a write response (AppointmentWire.status); null = unknown. */
  knownStatus?: AppointmentStatusWire | null;
  /** The status currently being saved (disables the three status actions). */
  statusPending?: AppointmentStatusWire | null;
  statusError?: string | null;
};

const ACTION_ICON: Readonly<Record<DrawerActionId, IconName>> = {
  release: "calendar",
  confirm: "check",
  attended: "check-double",
  no_show: "x",
  edit: "sliders",
  cancel: "x",
  message: "send",
};

// Icon only: the words carry the meaning. "clock" = waiting, "check" = paid, "x" = already resolved,
// "alert" = paid and waiting for the clinic's decision (spec F; only when the backend says so).
const DEPOSIT_ICON: Record<ApptDepositView["kind"], IconName> = {
  pending: "clock",
  paid: "check",
  closed: "x",
  attention: "alert",
};

const STATUS_BADGE: Partial<Record<ApptStatus, { tone: BadgeTone; label: string }>> = {
  cancelado: { tone: "cancelada", label: "Cancelada" },
  compareceu: { tone: "compareceu", label: "Compareceu" },
  faltou: { tone: "faltou", label: "Faltou" },
};

function badgeTone(s: AppointmentStatusWire): BadgeTone {
  if (s === "no_show") return "faltou";
  if (s === "cancelled") return "cancelada";
  if (s === "attended" || s === "confirmed") return "compareceu";
  return "neutral";
}

function InfoRow({ icon, label, children }: { icon: IconName; label: string; children: ReactNode }) {
  return (
    <div className="agm-info-row">
      <span className="agm-info-icon" aria-hidden="true"><Icon name={icon} size={16} /></span>
      <div className="agm-info-text">
        <div className="agm-info-label">{label}</div>
        <div className="agm-info-value">{children}</div>
      </div>
    </div>
  );
}

function BlockBody({ appt }: { appt: Appt }) {
  return (
    <>
      <div>
        <div className="agm-title">{appt.reason || "Bloqueio"}</div>
        <div className="agm-sub">{dayLabelFromKey(appt.date)} · {fmtRange(appt.start, appt.dur)}</div>
      </div>
      <p className="agm-hint">{BLOCK_HINT}</p>
    </>
  );
}

/** The click handler of one action, or undefined when it cannot run (→ disabled, never a silent no-op). */
function handlerFor(id: DrawerActionId, p: DrawerProps, appt: Appt): (() => void) | undefined {
  switch (id) {
    case "release":
      return p.onRelease ? () => p.onRelease!(appt) : undefined;
    case "message":
      return p.onMessage ? () => p.onMessage!(appt) : undefined;
    case "edit":
      return p.onEdit ? () => p.onEdit!(appt) : undefined;
    case "cancel":
      return p.onCancel ? () => p.onCancel!(appt) : undefined;
    case "confirm":
    case "attended":
    case "no_show": {
      if (!p.onStatus || p.statusPending) return undefined;
      const status = STATUS_OF[id];
      return () => p.onStatus!(appt, status);
    }
  }
}

function AppointmentBody(props: DrawerProps & { appt: Appt }) {
  const { appt, statusError } = props;
  const noteId = useId();
  const knownStatus = props.knownStatus ?? null;
  const now = props.now ?? null;
  const phase = actionPhase(appt, knownStatus, now);
  const actions = drawerActions(appt, knownStatus, now);
  // A status learned from a write that ended the appointment keeps ITS look; so does a stored terminal status.
  const look = isTerminal(appt, knownStatus) ? null : confirmationLook(appt);
  const timeline = reminderTimeline(appt.reminders);
  const cancelled = appt.status === "cancelado" || knownStatus === "cancelled";
  const statusBadge = STATUS_BADGE[appt.status];
  const name = appt.patient || "Paciente";
  const insurance = apptInsuranceView(appt);
  const deposit = apptDepositView(appt);
  const hint =
    phase === "unavailable" ? (appt.appointmentId ? null : CANCEL_UNAVAILABLE_HINT)
    : phase === "terminal" ? (cancelled ? CANCELLED_HINT : TERMINAL_HINT)
    : phase === "after_start" ? AFTER_START_HINT
    : phase === "unknown_time" ? UNKNOWN_TIME_HINT
    : null;

  return (
    <>
      <div className="agm-identity">
        <Avatar initials={initialsOf(name)} label={name} size={48} decorative />
        <div className="agm-identity-text">
          <div className="agm-title">{name}</div>
          {look && <ConfirmationBadge look={look} />}
          {!look && knownStatus && <Badge tone={badgeTone(knownStatus)}>{WIRE_STATUS_LABEL[knownStatus]}</Badge>}
          {!look && !knownStatus && statusBadge && <Badge tone={statusBadge.tone}>{statusBadge.label}</Badge>}
        </div>
      </div>

      <div className="agm-info">
        <InfoRow icon="calendar" label="Data e horário">{dayLabelFromKey(appt.date)} · {fmtRange(appt.start, appt.dur)}</InfoRow>
        <InfoRow icon="file" label="Tipo de consulta">{appt.type || "Não informado"}</InfoRow>
        {/* R7: the doctor, when the read carried it (a restricted doctor only ever sees his own name). */}
        {appt.professionalName ? <InfoRow icon="user" label="Profissional">{appt.professionalName}</InfoRow> : null}
        {insurance ? <InfoRow icon="clipboard" label={INSURANCE_ROW_LABEL}>{insurance.convenio}</InfoRow> : null}
        {deposit ? <InfoRow icon={DEPOSIT_ICON[deposit.kind]} label={DEPOSIT_ROW_LABEL}>{deposit.text}</InfoRow> : null}
        {/* R7: the APPOINTMENT's contact phone (never the patient's WhatsApp identity); hidden when absent. */}
        {appt.phone ? <InfoRow icon="user" label="Telefone">{appt.phone}</InfoRow> : null}
        {appt.notes ? <InfoRow icon="list" label="Observações">{appt.notes}</InfoRow> : null}
      </div>

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

      {look?.state === "attention" && actions.length > 0 && (
        <p className="agm-hint agm-confirm">{look.hint} Escolha o que fazer abaixo.</p>
      )}

      <section className="agm-section" aria-label="Ações">
        <h3 className="agm-section-title">Ações</h3>
        {actions.length > 0 && (
          <div className="agm-actions-grid" role="group" aria-label="Ações da consulta">
            {actions.map((id) => {
              const onClick = handlerFor(id, props, appt);
              const isStatus = id === "confirm" || id === "attended" || id === "no_show";
              return (
                <Button
                  key={id}
                  icon={ACTION_ICON[id]}
                  variant={id === "cancel" || id === "release" ? "danger" : "outline"}
                  className={id === "cancel" || id === "message" ? "agm-action--wide" : undefined}
                  disabled={!onClick}
                  aria-busy={(isStatus && props.statusPending === STATUS_OF[id]) || undefined}
                  aria-describedby={id === "no_show" ? noteId : undefined}
                  onClick={onClick}
                >
                  {ACTION_LABEL[id]}
                </Button>
              );
            })}
          </div>
        )}
        {actions.includes("no_show") && (
          <p id={noteId} className="agm-hint agm-status-note">{NO_SHOW_NOTE}</p>
        )}
        {statusError && (
          <p className="agm-note agm-note--danger" role="alert"><Icon name="alert" size={16} /><span>{statusError}</span></p>
        )}
        {hint && <p className="agm-hint">{hint}</p>}
      </section>
    </>
  );
}

export function Drawer(props: DrawerProps) {
  const { open, appt, onClose } = props;
  const isBlock = appt?.status === "bloqueio";
  return (
    <Sheet open={open} onClose={onClose} title={isBlock ? "Bloqueio" : "Detalhes da consulta"} className="agm-card">
      {appt && (isBlock ? <BlockBody appt={appt} /> : <AppointmentBody {...props} appt={appt} />)}
    </Sheet>
  );
}
```

- [ ] **Step 7: `components/agenda/AgendaView.tsx` — the drawer props only**

On `<Drawer …>` delete `onReschedule={writable ? p.onOpenReschedule : undefined}` and add `now={p.now}`. (Task 6 wires `onRelease`/`onMessage`, Task 9 wires `onEdit`; until then those buttons are disabled.)

- [ ] **Step 8: Run tests and type check**

Run: `npx vitest run lib/agenda components/agenda` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors (`STATUS_ACTIONS` had no other importer; if `tsc` names one, it is a test import to delete).

- [ ] **Step 9: Commit**

```bash
git add lib/agenda/actions.ts lib/agenda/modal-forms.ts components/agenda/Drawer.tsx components/agenda/agenda-modals.css components/agenda/AgendaView.tsx lib/agenda/__tests__/actions.test.ts lib/agenda/__tests__/modal-forms.test.ts components/agenda/__tests__/drawer.test.tsx
git commit -m "feat(agenda): drawer badge, reminder timeline and clock-gated actions (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 4: Hub client (R4 + R7 calls), notice vocabulary, paid-notice decision, release/message rules

**Files:**
- Modify: `lib/agenda/secretaria-hub-agenda.ts`, `lib/real/secretaria-hub.ts` (`TenantConfigWire`, `TenantConfigUpdatePayload`)
- Create: `lib/agenda/notice.ts`, `lib/agenda/release.ts`
- Modify: `lib/agenda/screen.ts` (the `Toast` types move to `notice.ts`; re-exported)
- Test: `lib/agenda/__tests__/notice.test.ts`, `lib/agenda/__tests__/release.test.ts`, `lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts`

**Interfaces:**
- Consumes: `hubFetch`, `HubApiError`, `getTenantConfig`, `updateHubConfiguration` (`lib/real/secretaria-hub.ts`); `AppointmentWire`, `CancelPreviewWire`; `patientFirstName` (`modal-forms.ts`); R4/R7 routes exactly as copied under "Consumes".
- Produces (names Tasks 5–9 use):
  - `secretaria-hub-agenda.ts` codes: `NOT_LIVE_CODE = "not_live"`, `STATUS_NO_SHOW_BEFORE_START_CODE = "no_show_before_start"`, `RELEASE_ACK_REQUIRED_CODE = "retention_ack_required"`, `RELEASE_ALREADY_CONFIRMED_CODE = "already_confirmed"`, `MESSAGE_OUTSIDE_WINDOW_CODE = "outside_window_not_authorised"`, `MESSAGE_NO_CHANNEL_CODE = "no_channel"`, `EDIT_SLOT_UNAVAILABLE_CODE = "slot_unavailable"`, `EDIT_APPOINTMENT_CHANGED_CODE = "appointment_changed"`.
  - types: `AppointmentActionWire = AppointmentWire & { confirmation_count?: number; patient_notice?: string | null; whatsapp_link?: string | null }`; `AppointmentStatusPayload = { status: AppointmentStatusWire; notify_outside_window: boolean }`; `AppointmentReleasePayload = { acknowledge_retention: boolean; release_confirmed: boolean; notify_outside_window: boolean }`; `AppointmentReleaseWire = AppointmentActionWire`; `AppointmentMessagePayload = { text: string; notify_outside_window: boolean }`; `AppointmentMessageWire = { delivery?: string | null; email_nudge?: string | null; message_id?: string | null }`; `AppointmentEditPayload = { start_at?: string; service?: string; professional_id?: string; insurance?: string | null; attendee_name?: string | null; phone?: string | null; notify_outside_window: boolean; allow_overlap: boolean }`.
  - calls: `updateAppointmentStatus(session, appointmentId, payload: AppointmentStatusPayload): Promise<AppointmentActionWire>` (**signature change**: was `(session, id, status)`); `cancelAppointment(...)` now resolves `AppointmentActionWire`; `releaseAppointment(session, id, payload)`, `messageAppointment(session, id, payload)`, `editAppointment(session, id, payload): Promise<AppointmentActionWire>`, `getPaidNoticesAutoApproved(session): Promise<boolean>`, `approvePaidNotices(session): Promise<boolean>`.
  - `notice.ts`: `type ToastTone = "success" | "danger"`; `type Toast = { tone: ToastTone; message: string; link?: { href: string; label: string } }`; `safeWaLink(v: unknown): string | null`; `NOTICE_LINK_LABEL = "Abrir conversa no WhatsApp"`; `actionToast(base: string, res: { patient_notice?: string | null; whatsapp_link?: string | null }): Toast`; `paidCostLabel(preview: CancelPreviewWire): string`; `type PaidNoticeAsk = { costLabel: string; waLink: string | null }`; `type PaidNoticeDecision = { kind: "send"; notify: boolean } | ({ kind: "ask" } & PaidNoticeAsk)`; `decidePaidNotice(preview: CancelPreviewWire | null, autoApproved: boolean, channel: PatientChannel | undefined): PaidNoticeDecision` (only `"whatsapp"` can be asked).
  - `release.ts`: `RELEASE_COPY`, `RELEASE_FAILED`, `RELEASE_UNAVAILABLE`, `RELEASE_ALREADY_DONE`, `RELEASE_ALREADY_CONFIRMED`, `RELEASE_ACK_REQUIRED`, `MESSAGE_COPY`, `MESSAGE_FAILED`, `MESSAGE_NO_CHANNEL`, `MESSAGE_MAX = 1000`, `PAID_APPROVE_FAILED`, `type ReleaseConfirm = { acknowledged: boolean; optIn: boolean; remember: boolean }`, `type ReleaseOptions`, `releaseNeedsAck(appt, forced?)`, `buildReleaseBody(appt, options) => AppointmentReleasePayload | null`, `releaseConfirmLabel(...)`, `messageSuggestion(appt)`, `validateMessage(text)`.
  - `screen.ts` keeps exporting `Toast` and `ToastTone` (now re-exported from `notice.ts`).

- [ ] **Step 1: Write the failing tests**

Create `lib/agenda/__tests__/notice.test.ts`:

```ts
// The patient_notice vocabulary (R4 + R7) and the paid-notice decision (spec 2026-10-09 §2-3).
import { describe, expect, it } from "vitest";

import { NOTICE_LINK_LABEL, actionToast, decidePaidNotice, paidCostLabel, safeWaLink } from "../notice";
import type { CancelPreviewWire } from "../secretaria-hub-agenda";

const preview = (over: Partial<CancelPreviewWire> = {}): CancelPreviewWire => ({
  inside_window: false, professional_name: "Dra. Ana", template_cost_brl: "R$ 0,35",
  cost_is_estimate: false, whatsapp_link: "https://wa.me/5511999990000", ...over,
});

describe("actionToast", () => {
  it.each([
    ["whatsapp_sent", "success", "O paciente foi avisado pelo WhatsApp."],
    ["whatsapp_queued", "success", "A secretarIA está avisando o paciente pelo WhatsApp."],
    ["portal_chat", "success", "O aviso ficou na conversa do paciente no Portal."],
    ["portal_chat_email", "success", "O aviso ficou na conversa do paciente no Portal, e um e-mail avisou que há mensagem."],
    ["no_channel", "danger", "O paciente NÃO foi avisado: não há um canal para falar com ele."],
    ["queue_unavailable", "danger", "O aviso ao paciente não pôde ser enviado agora. Fale com ele por outro meio."],
    ["notice_failed", "danger", "O aviso ao paciente não pôde ser enviado. Fale com ele por outro meio."],
  ] as const)("%s -> %s", (notice, tone, sentence) => {
    expect(actionToast("Consulta confirmada.", { patient_notice: notice })).toEqual({ tone, message: `Consulta confirmada. ${sentence}` });
  });

  it("outside the 24 h window: danger, says so, and carries ONLY a wa.me link", () => {
    const t = actionToast("Consulta alterada.", { patient_notice: "whatsapp_outside_window", whatsapp_link: "https://wa.me/5511999990000" });
    expect(t.tone).toBe("danger");
    expect(t.message).toBe(
      "Consulta alterada. O paciente NÃO foi avisado: está fora da janela de 24 h do WhatsApp. Escreva pelo seu próprio WhatsApp, sem custo.",
    );
    expect(t.link).toEqual({ href: "https://wa.me/5511999990000", label: NOTICE_LINK_LABEL });
    expect(actionToast("x", { patient_notice: "whatsapp_outside_window", whatsapp_link: "javascript:alert(1)" }).link).toBeUndefined();
    expect(actionToast("x", { patient_notice: "whatsapp_outside_window", whatsapp_link: null }).link).toBeUndefined();
  });

  it("null, absent or unknown claims nothing (success, the base sentence only)", () => {
    expect(actionToast("Presença registrada.", { patient_notice: null })).toEqual({ tone: "success", message: "Presença registrada." });
    expect(actionToast("Presença registrada.", {})).toEqual({ tone: "success", message: "Presença registrada." });
    expect(actionToast("Horário liberado.", { patient_notice: "not_attempted" })).toEqual({ tone: "success", message: "Horário liberado." });
    expect(actionToast("x", { patient_notice: "toString" }).message).toBe("x");
  });

  it("a link is never attached to another notice", () => {
    expect(actionToast("x", { patient_notice: "whatsapp_sent", whatsapp_link: "https://wa.me/1" }).link).toBeUndefined();
  });

  it("safeWaLink only passes https://wa.me/<digits>", () => {
    expect(safeWaLink("https://wa.me/5511999990000")).toBe("https://wa.me/5511999990000");
    expect(safeWaLink("https://wa.me/55a")).toBeNull();
    expect(safeWaLink("http://wa.me/55")).toBeNull();
    expect(safeWaLink(42)).toBeNull();
  });
});

describe("decidePaidNotice", () => {
  it("WhatsApp patient outside 24 h, clinic flag off -> ask, with the cost and the free link", () => {
    expect(decidePaidNotice(preview(), false, "whatsapp")).toEqual({ kind: "ask", costLabel: "R$ 0,35", waLink: "https://wa.me/5511999990000" });
  });

  it("a WhatsApp patient whose appointment has no number is still asked (the backend notifies his own wa_id); no link then", () => {
    expect(decidePaidNotice(preview({ whatsapp_link: null }), false, "whatsapp")).toEqual({ kind: "ask", costLabel: "R$ 0,35", waLink: null });
  });

  it("an estimated cost says so; an unconfigured cost is empty (never a made-up price)", () => {
    expect(paidCostLabel(preview({ cost_is_estimate: true }))).toBe("~R$ 0,35 (estimado)");
    expect(paidCostLabel(preview({ template_cost_brl: "  " }))).toBe("");
  });

  it("clinic flag on -> no question, notify_outside_window true", () => {
    expect(decidePaidNotice(preview(), true, "whatsapp")).toEqual({ kind: "send", notify: true });
  });

  it("inside 24 h -> no question, nothing paid", () => {
    expect(decidePaidNotice(preview({ inside_window: true }), false, "whatsapp")).toEqual({ kind: "send", notify: false });
    expect(decidePaidNotice(preview({ inside_window: true }), true, "whatsapp")).toEqual({ kind: "send", notify: false });
  });

  it("a Portal patient (patient_channel brain_message) -> never asked, nothing paid, even when the preview carries a WhatsApp link", () => {
    expect(decidePaidNotice(preview(), false, "brain_message")).toEqual({ kind: "send", notify: false });
    expect(decidePaidNotice(preview(), true, "brain_message")).toEqual({ kind: "send", notify: false });
  });

  it("no patient record or no channel key (older backend) -> never asked; the response tells what happened", () => {
    expect(decidePaidNotice(preview(), false, undefined)).toEqual({ kind: "send", notify: false });
    expect(decidePaidNotice(preview(), true, undefined)).toEqual({ kind: "send", notify: false });
  });

  it("a failed preview never bills silently", () => {
    expect(decidePaidNotice(null, false, "whatsapp")).toEqual({ kind: "send", notify: false });
    expect(decidePaidNotice(null, true, "whatsapp")).toEqual({ kind: "send", notify: false });
  });
});
```

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

Create `lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts`:

```ts
// The R4/R7 calls through the real hubFetch (fetch stubbed, brain-session mocked),
// same idiom as lib/real/__tests__/secretaria-hub.test.ts.
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HubApiError, SECRETARIA_HUB_BASE } from "../../real/secretaria-hub";
import type { Session } from "../../real/brain-session";
import {
  MESSAGE_OUTSIDE_WINDOW_CODE,
  RELEASE_ACK_REQUIRED_CODE,
  STATUS_NO_SHOW_BEFORE_START_CODE,
  approvePaidNotices,
  editAppointment,
  getPaidNoticesAutoApproved,
  messageAppointment,
  releaseAppointment,
  updateAppointmentStatus,
} from "../secretaria-hub-agenda";

vi.mock("../../real/brain-session", () => ({
  mintHubToken: vi.fn(async () => ({ hubToken: "fake-hub-token", expiresIn: 3600 })),
}));

const session: Session = { token: "t", tenantId: "tenant-1", email: "d@x.com", role: "doctor", userId: "u1" };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const lastCall = () => vi.mocked(fetch).mock.calls.at(-1)!;

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal("fetch", vi.fn());
});

describe("updateAppointmentStatus (R7)", () => {
  it("PATCHes status + notify_outside_window and returns the notice", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { id: "ap 1", status: "confirmed", patient_notice: "whatsapp_sent", whatsapp_link: null }));
    const res = await updateAppointmentStatus(session, "ap 1", { status: "confirmed", notify_outside_window: true });
    expect(res.patient_notice).toBe("whatsapp_sent");
    const [url, init] = lastCall();
    expect(url).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/calendar/appointments/ap%201/status`);
    expect((init as RequestInit).method).toBe("PATCH");
    expect((init as RequestInit).body).toBe('{"status":"confirmed","notify_outside_window":true}');
  });

  it("exposes the no-show-before-start refusal with the backend's own sentence", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json(409, { detail: { code: STATUS_NO_SHOW_BEFORE_START_CODE, message: "Só é possível marcar falta depois do horário da consulta.", status: "scheduled", start_at: "2026-10-20T13:30:00Z" } }),
    );
    const err = await updateAppointmentStatus(session, "ap-1", { status: "no_show", notify_outside_window: false }).catch((e) => e);
    expect(err).toBeInstanceOf(HubApiError);
    expect(err.code).toBe(STATUS_NO_SHOW_BEFORE_START_CODE);
    expect(err.message).toBe("Só é possível marcar falta depois do horário da consulta.");
  });
});

describe("editAppointment (R7)", () => {
  it("POSTs only the given keys to …/edit", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { id: "ap-1", status: "rescheduled", patient_notice: "portal_chat" }));
    const res = await editAppointment(session, "ap-1", { start_at: "2026-10-20T13:30:00.000Z", notify_outside_window: false, allow_overlap: false });
    expect(res.status).toBe("rescheduled");
    const [url, init] = lastCall();
    expect(url).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/calendar/appointments/ap-1/edit`);
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBe('{"start_at":"2026-10-20T13:30:00.000Z","notify_outside_window":false,"allow_overlap":false}');
  });
});

describe("releaseAppointment / messageAppointment (R4)", () => {
  it("POSTs the three release flags and returns the notice and the link", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { id: "ap-1", status: "cancelled", patient_notice: "whatsapp_outside_window", whatsapp_link: "https://wa.me/55" }));
    const res = await releaseAppointment(session, "ap-1", { acknowledge_retention: true, release_confirmed: false, notify_outside_window: false });
    expect(res.whatsapp_link).toBe("https://wa.me/55");
    expect((lastCall()[1] as RequestInit).body).toBe('{"acknowledge_retention":true,"release_confirmed":false,"notify_outside_window":false}');
  });

  it("keeps the retention refusal's code and text", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json(409, { detail: { code: RELEASE_ACK_REQUIRED_CODE, message: "O sinal pago será retido.", deposit_outcome: "retained", amount_cents: 4500 } }),
    );
    const err = await releaseAppointment(session, "ap-1", { acknowledge_retention: false, release_confirmed: false, notify_outside_window: false }).catch((e) => e);
    expect(err.status).toBe(409);
    expect(err.code).toBe(RELEASE_ACK_REQUIRED_CODE);
    expect(err.message).toBe("O sinal pago será retido.");
  });

  it("message: POSTs text + flag; an outside-window refusal keeps cost and link on error.detail", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(json(200, { delivery: "portal_chat", email_nudge: "sent", message_id: null }));
    expect((await messageAppointment(session, "ap-1", { text: "Olá", notify_outside_window: false })).delivery).toBe("portal_chat");
    expect((lastCall()[1] as RequestInit).body).toBe('{"text":"Olá","notify_outside_window":false}');
    vi.mocked(fetch).mockResolvedValueOnce(
      json(409, { detail: { code: MESSAGE_OUTSIDE_WINDOW_CODE, message: "fora da janela", template_cost_brl: "R$ 0,35", whatsapp_link: "https://wa.me/55" } }),
    );
    const err = await messageAppointment(session, "ap-1", { text: "Olá", notify_outside_window: false }).catch((e) => e);
    expect((err.detail as { template_cost_brl: string }).template_cost_brl).toBe("R$ 0,35");
  });
});

describe("the clinic's paid-notice flag (R7)", () => {
  it("reads it from GET /config; absent or not literally true reads as false", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(json(200, { clinic_name: "C", paid_notices_auto_approved: true }));
    expect(await getPaidNoticesAutoApproved(session)).toBe(true);
    expect(lastCall()[0]).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/config`);
    vi.mocked(fetch).mockResolvedValueOnce(json(200, { clinic_name: "C" }));
    expect(await getPaidNoticesAutoApproved(session)).toBe(false);
  });

  it("'Não perguntar novamente' PUTs the tenant flag through /configuration and returns what was stored", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(json(200, { tenant: { paid_notices_auto_approved: true }, professional: null }));
    expect(await approvePaidNotices(session)).toBe(true);
    const [url, init] = lastCall();
    expect(url).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/configuration`);
    expect((init as RequestInit).method).toBe("PUT");
    expect((init as RequestInit).body).toBe('{"tenant":{"paid_notices_auto_approved":true}}');
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/notice.test.ts lib/agenda/__tests__/release.test.ts lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts`
Expected: FAIL (modules/exports missing).

- [ ] **Step 3: The clinic flag on the config wire (`lib/real/secretaria-hub.ts`)**

In `TenantConfigWire`, after `asaas_connected: boolean;` add:

```ts
  // TASK-032 R7: the clinic's standing authorisation for PAID WhatsApp notices
  // ("Não perguntar novamente" in the agenda; switch in Contexto). ABSENT on a
  // backend that predates R7: read as "not approved" and never written then.
  paid_notices_auto_approved?: boolean;
```

and in `TenantConfigUpdatePayload` (the `Partial<{ … }>`), after `google_calendar_mode: GoogleCalendarMode;` add `paid_notices_auto_approved: boolean;`.

- [ ] **Step 4: The calls (`lib/agenda/secretaria-hub-agenda.ts`)**

(a) Extend the header route list with:

```ts
//   POST  /tenants/me/calendar/appointments/{id}/release       -> AppointmentReleaseRead (R4)
//   POST  /tenants/me/calendar/appointments/{id}/message       -> StaffMessageRead (R4)
//   POST  /tenants/me/calendar/appointments/{id}/edit          -> AppointmentActionRead (R7)
//   PATCH status / POST cancel answer AppointmentActionRead since R7 (+ patient_notice, whatsapp_link)
```

(b) Add the import `import { getTenantConfig, updateHubConfiguration } from "../real/secretaria-hub";` (next to the existing `hubFetch` import, same module — merge into one import line).

(c) After `AppointmentReschedulePayload` add:

```ts
// Refusal codes (`detail.code`) the agenda reacts to (R4 + R7).
export const NOT_LIVE_CODE = "not_live"; // already cancelled / attended / no-show (409)
export const STATUS_NO_SHOW_BEFORE_START_CODE = "no_show_before_start"; // "Faltou" before the start (409)
export const RELEASE_ACK_REQUIRED_CODE = "retention_ack_required"; // paid Pix deposit, no acknowledgement (409)
export const RELEASE_ALREADY_CONFIRMED_CODE = "already_confirmed"; // the patient confirmed meanwhile (409)
export const MESSAGE_OUTSIDE_WINDOW_CODE = "outside_window_not_authorised"; // WhatsApp, outside 24 h, not authorised (409)
export const MESSAGE_NO_CHANNEL_CODE = "no_channel"; // nothing to deliver on (422)
export const EDIT_SLOT_UNAVAILABLE_CODE = "slot_unavailable"; // the new time is taken / outside hours (409)
export const EDIT_APPOINTMENT_CHANGED_CODE = "appointment_changed"; // another edit won meanwhile (409)

/**
 * AppointmentActionRead (R7): what PATCH status, POST edit and POST cancel answer.
 * `patient_notice` is kept a plain string on purpose (lib/agenda/notice.ts maps the
 * known values; an unknown one claims nothing). Both keys are ABSENT on an older backend.
 */
export type AppointmentActionWire = AppointmentWire & {
  confirmation_count?: number;
  patient_notice?: string | null;
  /** https://wa.me/... — only with patient_notice "whatsapp_outside_window". */
  whatsapp_link?: string | null;
};

export type AppointmentStatusPayload = {
  status: AppointmentStatusWire;
  /** Authorises the PAID template outside the 24 h window for this action (OR-ed with the clinic flag). */
  notify_outside_window: boolean;
};

export type AppointmentReleasePayload = {
  /** true only after the clinic ticked the Pix-retention warning. */
  acknowledge_retention: boolean;
  /** true only after the clinic saw "the patient just confirmed" and chose to release anyway. */
  release_confirmed: boolean;
  /** Authorises the PAID WhatsApp template for the patient notice outside the 24 h window. */
  notify_outside_window: boolean;
};

/** AppointmentReleaseRead (R4 + R7's whatsapp_link): the same notice keys as every action. */
export type AppointmentReleaseWire = AppointmentActionWire;

export type AppointmentMessagePayload = { text: string; notify_outside_window: boolean };

/** `delivery`: "whatsapp_text" | "whatsapp_template" | "portal_chat"; `email_nudge` only for the Portal: "sent" | "no_email" | "not_sent". */
export type AppointmentMessageWire = {
  delivery?: string | null;
  email_nudge?: string | null;
  message_id?: string | null;
};

/**
 * POST …/edit (R7, "Editar/Remarcar"). Every editable key is optional and sent ONLY
 * when the clinic changed it (lib/agenda/edit.ts::buildEditPayload); `null` clears
 * insurance / attendee name / contact phone. Extra keys are refused (422).
 */
export type AppointmentEditPayload = {
  /** Timezone-aware ISO instant; future only. */
  start_at?: string;
  /** A service name the (new or current) professional offers. */
  service?: string;
  /** An active professional of this clinic. */
  professional_id?: string;
  insurance?: string | null;
  attendee_name?: string | null;
  /** The appointment's contact phone, with country code; never the patient's WhatsApp or login. */
  phone?: string | null;
  notify_outside_window: boolean;
  /** true = "encaixe": skip the free-slot check. */
  allow_overlap: boolean;
};
```

(d) Change `cancelAppointment`'s return type to `Promise<AppointmentActionWire>` (and its `hubFetch<…>` generic), and replace `updateAppointmentStatus` with:

```ts
/** PATCH status (R7). CANCELLED via PATCH skips the patient notice — the UI routes cancels to cancelAppointment. */
export function updateAppointmentStatus(
  session: Session,
  appointmentId: string,
  payload: AppointmentStatusPayload,
): Promise<AppointmentActionWire> {
  return hubFetch<AppointmentActionWire>(session, apptPath(appointmentId, "status"), {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
```

(e) Append at the end of the file:

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

/** "Editar/Remarcar" (R7): changes what the clinic changed and tells the patient. */
export function editAppointment(
  session: Session,
  appointmentId: string,
  payload: AppointmentEditPayload,
): Promise<AppointmentActionWire> {
  return hubFetch<AppointmentActionWire>(session, apptPath(appointmentId, "edit"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** The clinic's standing authorisation for paid notices (R7). Absent / not literally true = false. */
export async function getPaidNoticesAutoApproved(session: Session): Promise<boolean> {
  const cfg = await getTenantConfig(session);
  return cfg.paid_notices_auto_approved === true;
}

/** "Não perguntar novamente": stores the authorisation; resolves to what the backend stored. */
export async function approvePaidNotices(session: Session): Promise<boolean> {
  const res = await updateHubConfiguration(session, { tenant: { paid_notices_auto_approved: true } });
  return res.tenant.paid_notices_auto_approved === true;
}
```

- [ ] **Step 5: `lib/agenda/notice.ts`**

```ts
// lib/agenda/notice.ts — what the clinic is told about the PATIENT after an action
// (spec 2026-10-09 §2-3), and whether a paid WhatsApp notice must be asked first.
// Pure: no fetch, no React.
//
// Vocabulary (R4 + R7 `patient_notice`): success = the patient was or will be told;
// danger = the patient was NOT told and the clinic must act. An unknown value (a
// newer backend, or R4's "not_attempted") claims nothing. The wa.me link is
// untrusted data going into an href: only https://wa.me/<digits> passes.

import type { CancelPreviewWire } from "./secretaria-hub-agenda";
import type { PatientChannel } from "./types";

export type ToastTone = "success" | "danger";
export type Toast = {
  tone: ToastTone;
  message: string;
  /** A free alternative for the clinic (the wa.me conversation), rendered as a link. */
  link?: { href: string; label: string };
};

export const NOTICE_LINK_LABEL = "Abrir conversa no WhatsApp";

const NOTICE_OUTCOME: Readonly<Record<string, { tone: ToastTone; text: string }>> = {
  whatsapp_sent: { tone: "success", text: "O paciente foi avisado pelo WhatsApp." },
  whatsapp_queued: { tone: "success", text: "A secretarIA está avisando o paciente pelo WhatsApp." },
  portal_chat: { tone: "success", text: "O aviso ficou na conversa do paciente no Portal." },
  portal_chat_email: {
    tone: "success",
    text: "O aviso ficou na conversa do paciente no Portal, e um e-mail avisou que há mensagem.",
  },
  whatsapp_outside_window: {
    tone: "danger",
    text: "O paciente NÃO foi avisado: está fora da janela de 24 h do WhatsApp. Escreva pelo seu próprio WhatsApp, sem custo.",
  },
  no_channel: { tone: "danger", text: "O paciente NÃO foi avisado: não há um canal para falar com ele." },
  queue_unavailable: { tone: "danger", text: "O aviso ao paciente não pôde ser enviado agora. Fale com ele por outro meio." },
  notice_failed: { tone: "danger", text: "O aviso ao paciente não pôde ser enviado. Fale com ele por outro meio." },
};

const WA_LINK = /^https:\/\/wa\.me\/\d+$/;

export function safeWaLink(v: unknown): string | null {
  return typeof v === "string" && WA_LINK.test(v) ? v : null;
}

/** `base` (what happened to the appointment) + what happened to the patient notice. */
export function actionToast(base: string, res: { patient_notice?: string | null; whatsapp_link?: string | null }): Toast {
  const notice = typeof res.patient_notice === "string" ? res.patient_notice : null;
  const outcome = notice !== null && Object.prototype.hasOwnProperty.call(NOTICE_OUTCOME, notice) ? NOTICE_OUTCOME[notice] : null;
  if (!outcome) return { tone: "success", message: base };
  const link = notice === "whatsapp_outside_window" ? safeWaLink(res.whatsapp_link) : null;
  return {
    tone: outcome.tone,
    message: `${base} ${outcome.text}`,
    ...(link ? { link: { href: link, label: NOTICE_LINK_LABEL } } : {}),
  };
}

/** "R$ 0,35", "~R$ 0,35 (estimado)", or "" when the clinic configured no price (never invent one). */
export function paidCostLabel(preview: CancelPreviewWire): string {
  const cost = (preview.template_cost_brl ?? "").trim();
  if (!cost) return "";
  return preview.cost_is_estimate === true ? `~${cost} (estimado)` : cost;
}

export type PaidNoticeAsk = { costLabel: string; waLink: string | null };

export type PaidNoticeDecision = { kind: "send"; notify: boolean } | ({ kind: "ask" } & PaidNoticeAsk);

/**
 * Before an action that notifies (spec §2-3): ask only for a WhatsApp patient
 * outside the 24 h window when the clinic has no standing authorisation. The
 * channel is the event's `patient_channel` (R7), never guessed from the link.
 *  - not a WhatsApp patient (Portal, no patient record, older backend) -> never ask
 *  - no preview (lookup failed)      -> act without paying; the response tells what happened
 *  - inside the window               -> free, nothing to ask
 *  - clinic flag on                  -> pay without asking
 */
export function decidePaidNotice(
  preview: CancelPreviewWire | null,
  autoApproved: boolean,
  channel: PatientChannel | undefined,
): PaidNoticeDecision {
  if (channel !== "whatsapp" || !preview || preview.inside_window) return { kind: "send", notify: false };
  if (autoApproved) return { kind: "send", notify: true };
  return { kind: "ask", costLabel: paidCostLabel(preview), waLink: safeWaLink(preview.whatsapp_link) };
}
```

- [ ] **Step 6: `screen.ts` takes its toast types from `notice.ts`**

In `lib/agenda/screen.ts` replace

```ts
export type ToastTone = "success" | "danger";
export type Toast = { tone: ToastTone; message: string };
```

with

```ts
import type { Toast, ToastTone } from "./notice";
export type { Toast, ToastTone };
```

(move the `import type` line up into the import block at the top of the file; keep the `export type { … }` where the old declarations were).

- [ ] **Step 7: `lib/agenda/release.ts`**

```ts
// lib/agenda/release.ts — the rules behind "Liberar horário" and "Enviar mensagem"
// (TASK-032 R5). Pure, so the Pix-retention gate is testable without a DOM.
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
/** "Não perguntar novamente" could not be saved: nothing else was attempted. */
export const PAID_APPROVE_FAILED =
  "Não foi possível salvar a autorização para avisos pagos. Nada foi enviado. Tente de novo ou desmarque “Não perguntar novamente”.";

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
  outsideLabel: "Enviar mesmo assim, como mensagem oficial (cobrada)",
  openWhatsapp: "Abrir o WhatsApp",
} as const;

export const MESSAGE_FAILED = "Não foi possível enviar a mensagem. Nada foi enviado. Tente novamente.";
export const MESSAGE_NO_CHANNEL = "Este paciente não tem um canal para receber mensagens pela secretarIA.";
export const MESSAGE_MAX = 1000;

/** What the release card hands the controller: the ticks the clinic made. */
export type ReleaseConfirm = {
  acknowledged: boolean;
  /** "Enviar mesmo assim": the paid notice for a patient outside the 24 h window. */
  optIn: boolean;
  /** "Não perguntar novamente": save the clinic flag first (only with optIn). */
  remember: boolean;
};

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

- [ ] **Step 8: Keep the existing callers compiling**

`updateAppointmentStatus` changed its third parameter. In `lib/agenda/screen.ts`, change the `AgendaHub` member to

```ts
  updateAppointmentStatus: (s: Session, appointmentId: string, payload: AppointmentStatusPayload) => Promise<AppointmentActionWire>;
```

(add `AppointmentActionWire, AppointmentStatusPayload` to the type import from `./secretaria-hub-agenda`) and, inside `setStatus`, `await hub.updateAppointmentStatus(s, appointmentId, status)` becomes `await hub.updateAppointmentStatus(s, appointmentId, { status, notify_outside_window: false })` (Task 5 replaces this method). In `lib/agenda/__tests__/screen.test.ts`, the `fakeHub` line becomes `updateAppointmentStatus: vi.fn(async (_s, id, body) => wire(id, body.status)),` and the assertion in `"PATCHes, reflects the returned status and refetches"` becomes `toHaveBeenCalledWith(expect.anything(), "appt-e1", { status: "no_show", notify_outside_window: false })`.

- [ ] **Step 9: Run tests and type check**

Run: `npx vitest run lib/agenda` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add lib/real/secretaria-hub.ts lib/agenda/secretaria-hub-agenda.ts lib/agenda/notice.ts lib/agenda/release.ts lib/agenda/screen.ts lib/agenda/__tests__/notice.test.ts lib/agenda/__tests__/release.test.ts lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts lib/agenda/__tests__/screen.test.ts
git commit -m "feat(agenda): R4/R7 hub calls, patient-notice vocabulary and paid-notice decision (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Controller — status actions with the paid-notice question, guards, live flip, release, message, cancel notice, deep link

**Files:**
- Modify: `lib/agenda/confirmation.ts` (add `applyStaffConfirmation`), `lib/agenda/screen.ts`
- Test: `lib/agenda/__tests__/confirmation.test.ts` (append), `lib/agenda/__tests__/screen.test.ts` (modify `fakeHub`, append)

**Interfaces:**
- Consumes: Task 4 (`decidePaidNotice`, `actionToast`, `PaidNoticeAsk`, release rules, codes, call types); `Appt.patientChannel` (Task 1); `canActOn`, `cancelNoticeState` (`modal-forms.ts`); the controller's `loadCheck`, `beginModalWrite`, `fresh`, `refreshAfter`.
- Produces (Task 6 wires these; Tasks 7–8 extend them):
  - `applyStaffConfirmation(a: Appt): Appt` in `confirmation.ts`.
  - `screen.ts`: copy `STATUS_NOT_LIVE`, `NO_SHOW_TOO_EARLY`, `RELEASED_BASE`, `CANCELLED_BASE`; `statusToast(res: AppointmentActionWire): Toast`; `ReleaseModalError = { kind: "already_done" } | { kind: "ack_required"; message: string } | { kind: "already_confirmed" } | { kind: "failed"; message: string }`; `MessageModalError = { kind: "failed"; message: string } | { kind: "no_channel" } | { kind: "outside_window"; costBrl: string; waLink: string | null }`; `PaidNoticeModalError = { kind: "failed"; message: string }`; `AgendaModal` gains `{ type: "paid_notice"; appt: Appt; status: "confirmed" | "attended"; ask: PaidNoticeAsk; pending: boolean; error: PaidNoticeModalError | null }`, `{ type: "release"; appt; pending; error: ReleaseModalError | null; forceAck: boolean; confirmedSeen: boolean }`, `{ type: "message"; appt; pending; error: MessageModalError | null }`; `AgendaState.paidAuto: boolean` (the clinic flag, read with every window check); scoped actions `{ type: "confirmation_recorded"; appointmentId }`, `{ type: "paid_auto_loaded"; value: boolean }`; `AgendaHub` gains `releaseAppointment`, `messageAppointment`, `getPaidNoticesAutoApproved`, `approvePaidNotices`.
  - Controller: `setStatus(appt, status)` (confirmed/attended ask first when needed; no_show never asks), `choosePaidNotice(choice: { send: boolean; remember: boolean }): Promise<void>`, `openRelease(appt): Promise<void>`, `confirmRelease(c: ReleaseConfirm): Promise<void>`, `openMessage(appt): void`, `sendMessage(text: string, notifyOutsideWindow: boolean, remember?: boolean): Promise<void>`, `confirmCancel(body, remember?: boolean)`.
  - `parseAgendaParams` also accepts `?consulta=` (R4's warning e-mail) as an alias of `?consultaId=`.
  - Behaviour contract: nothing is sent while a Pix gate is open or a 24 h verdict is loading; "Não perguntar novamente" is saved before the action and a failed save sends nothing; every failure leaves its sheet open (or the drawer error set), `pending` false and no success toast.

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

(a) Add to the import from `"../screen"`: `NO_SHOW_TOO_EARLY`, `STATUS_NOT_LIVE`. Add `import { MESSAGE_FAILED, PAID_APPROVE_FAILED, RELEASE_FAILED, RELEASE_UNAVAILABLE } from "../release";` and `import { MESSAGE_NO_CHANNEL_CODE, MESSAGE_OUTSIDE_WINDOW_CODE, NOT_LIVE_CODE, RELEASE_ACK_REQUIRED_CODE, RELEASE_ALREADY_CONFIRMED_CODE, STATUS_NO_SHOW_BEFORE_START_CODE } from "../secretaria-hub-agenda";`.

(b) The `event()` helper's appointments are WhatsApp patients from now on (R7 `patient_channel`; only they can be asked about a paid notice). Replace its `return` line with:

```ts
  return {
    id, summary: `Consulta — Paciente ${id}`, start: start.toISOString(), end: end.toISOString(), appointment_id: appointmentId,
    ...(appointmentId ? { patient_channel: "whatsapp" } : {}),
  };
```

In `fakeHub`, after the `updateAppointmentStatus` line, add:

```ts
    releaseAppointment: vi.fn(async (_s, id) => ({ ...wire(id, "cancelled"), patient_notice: "whatsapp_queued" })),
    messageAppointment: vi.fn(async () => ({ delivery: "whatsapp_text", email_nudge: null, message_id: null })),
    getPaidNoticesAutoApproved: vi.fn(async () => false),
    approvePaidNotices: vi.fn(async () => true),
```

(c) After the existing test `it("parses ?data=, ?view= and ?consultaId=, ignoring junk", …)` add:

```ts
  it("accepts ?consulta= (the id the clinic's warning e-mail links with); ?consultaId= wins when both are present", () => {
    expect(parseAgendaParams("?consulta=ap-9").consultaId).toBe("ap-9");
    expect(parseAgendaParams("?consultaId=a&consulta=b").consultaId).toBe("a");
    expect(parseAgendaParams("?consulta=%20").consultaId).toBeNull();
  });
```

(d) Append at the end of the file:

```ts
// ---------------------------------------------------------------------------
// TASK-032 R5 — status with the paid-notice question, guards, flip, release, message, cancel notice
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
const gate = { acknowledged: false, optIn: false, remember: false };
const outsidePreview = () => vi.fn(async () => preview({ inside_window: false }));
const patchCalls = (s: { hub: AgendaHub }) => (s.hub.updateAppointmentStatus as ReturnType<typeof vi.fn>).mock.calls;

describe("deep link from the clinic's warning e-mail", () => {
  it("?consulta=<id>&data=<day> loads that week and opens the drawer", async () => {
    const s = setup({ initial: parseAgendaParams("?consulta=appt-e1&data=2026-09-29") });
    await s.controller.start();
    expect(s.state().anchorKey).toBe("2026-09-29");
    expect(s.state().selectedId).toBe("hub-e1");
  });

  it("?consulta=<id> without data still opens it when it is in the loaded range", async () => {
    const s = setup({ initial: parseAgendaParams("?consulta=appt-e1") });
    await s.controller.start();
    expect(s.state().selectedId).toBe("hub-e1");
  });
});

describe("confirmation — red turns green live", () => {
  it("flips right after 'Confirmar', before the refetch lands; the refetch then wins", async () => {
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

  it("registering attended does not touch the confirmation", async () => {
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

describe("status actions — the paid-notice question (spec 2026-10-09 §2)", () => {
  it("inside 24 h: no question, PATCH with notify false, and the toast says how the patient was told", async () => {
    const s = setup({
      hub: fakeHub({ updateAppointmentStatus: vi.fn(async (_s, id) => ({ ...wire(id, "confirmed"), patient_notice: "whatsapp_sent" })) }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(patchCalls(s)[0][2]).toEqual({ status: "confirmed", notify_outside_window: false });
    expect(s.state().modal).toBeNull();
    expect(s.state().toast).toEqual({ tone: "success", message: "Consulta confirmada. O paciente foi avisado pelo WhatsApp." });
  });

  it("WhatsApp outside 24 h, flag off: nothing is sent; the sheet opens with the cost and the free link; the drawer is free again", async () => {
    const s = setup({ hub: fakeHub({ getCancelPreview: outsidePreview() }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(s.hub.updateAppointmentStatus).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({
      type: "paid_notice", status: "attended", pending: false, error: null,
      ask: { costLabel: "R$ 0,35", waLink: "https://wa.me/5511999990000" },
    });
    expect(s.state().statusPending).toBeNull();
  });

  it("'Enviar mesmo assim' sends notify true; 'Não avisar' sends notify false; the sheet closes", async () => {
    for (const send of [true, false]) {
      const s = setup({ hub: fakeHub({ getCancelPreview: outsidePreview() }) });
      await s.controller.start();
      await s.controller.setStatus(appt(s.state()), "confirmed");
      await s.controller.choosePaidNotice({ send, remember: false });
      expect(patchCalls(s)[0][2]).toEqual({ status: "confirmed", notify_outside_window: send });
      expect(s.state().modal).toBeNull();
      expect(s.hub.approvePaidNotices).not.toHaveBeenCalled();
    }
  });

  it("'Não perguntar novamente' saves the flag BEFORE sending, then the next action asks nothing", async () => {
    const auto = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
    const s = setup({ hub: fakeHub({ getCancelPreview: outsidePreview(), getPaidNoticesAutoApproved: auto }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    await s.controller.choosePaidNotice({ send: true, remember: true });
    const approve = (s.hub.approvePaidNotices as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    const patch = (s.hub.updateAppointmentStatus as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(approve).toBeLessThan(patch);
    expect(s.state().paidAuto).toBe(true);
    await flush();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(patchCalls(s)[1][2]).toEqual({ status: "attended", notify_outside_window: true });
    expect(s.state().modal).toBeNull();
  });

  it("a failed save of the flag sends nothing and keeps the sheet with the reason", async () => {
    const s = setup({
      hub: fakeHub({ getCancelPreview: outsidePreview(), approvePaidNotices: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))) }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    await s.controller.choosePaidNotice({ send: true, remember: true });
    expect(s.hub.updateAppointmentStatus).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({ type: "paid_notice", pending: false, error: { kind: "failed", message: PAID_APPROVE_FAILED } });
  });

  it("flag on: no question, notify true", async () => {
    const s = setup({ hub: fakeHub({ getCancelPreview: outsidePreview(), getPaidNoticesAutoApproved: vi.fn(async () => true) }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(patchCalls(s)[0][2]).toEqual({ status: "confirmed", notify_outside_window: true });
    expect(s.state().modal).toBeNull();
  });

  it("a Portal patient (patient_channel brain_message): never asked, nothing paid — even with a WhatsApp link on the preview", async () => {
    const s = setup({
      hub: fakeHub({
        listCalendarEvents: vi.fn(async () => [{ ...event("e1", 29, 9), patient_channel: "brain_message" }]),
        getCancelPreview: outsidePreview(),
      }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(patchCalls(s)[0][2]).toEqual({ status: "attended", notify_outside_window: false });
    expect(s.state().modal).toBeNull();
  });

  it("no channel on the event (phone-only booking, or a backend without R7): never asked", async () => {
    const noChannel: CalendarEventWire = { ...event("e1", 29, 9), patient_channel: undefined };
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [noChannel]), getCancelPreview: outsidePreview() }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(patchCalls(s)[0][2]).toEqual({ status: "confirmed", notify_outside_window: false });
    expect(s.state().modal).toBeNull();
  });

  it("a failed preview acts without paying; the response tells what happened", async () => {
    const s = setup({
      hub: fakeHub({
        getCancelPreview: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))),
        updateAppointmentStatus: vi.fn(async (_s, id) => ({ ...wire(id, "confirmed"), patient_notice: "whatsapp_outside_window", whatsapp_link: "https://wa.me/5511999990000" })),
      }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "confirmed");
    expect(patchCalls(s)[0][2]).toEqual({ status: "confirmed", notify_outside_window: false });
    expect(s.state().toast).toMatchObject({ tone: "danger", link: { href: "https://wa.me/5511999990000" } });
  });

  it("Faltou never notifies, so it never reads the preview", async () => {
    const s = setup();
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "no_show");
    expect(s.hub.getCancelPreview).not.toHaveBeenCalled();
    expect(patchCalls(s)[0][2]).toEqual({ status: "no_show", notify_outside_window: false });
  });

  it("a double click on Confirmar sends once", async () => {
    const d = deferred<AppointmentWire>();
    const s = setup({ hub: fakeHub({ updateAppointmentStatus: vi.fn(() => d.promise) }) });
    await s.controller.start();
    const first = s.controller.setStatus(appt(s.state()), "confirmed");
    await s.controller.setStatus(appt(s.state()), "confirmed");
    d.resolve(wire("appt-e1", "confirmed"));
    await first;
    expect(s.hub.updateAppointmentStatus).toHaveBeenCalledTimes(1);
  });
});

describe("status actions — backend guards (R7)", () => {
  it("409 no_show_before_start: the backend's sentence in the drawer, nothing learned, no refetch", async () => {
    const refuse = vi.fn(async () =>
      Promise.reject(new HubApiError(409, "Só é possível marcar falta depois do horário da consulta.", STATUS_NO_SHOW_BEFORE_START_CODE, { status: "scheduled" })),
    );
    const s = setup({ hub: fakeHub({ updateAppointmentStatus: refuse }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "no_show");
    expect(s.state().statusError).toBe("Só é possível marcar falta depois do horário da consulta.");
    expect(s.state().knownStatus).toEqual({});
    expect(s.state().toast).toBeNull();
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(1);
  });

  it("an empty refusal text falls back to the local sentence", async () => {
    const s = setup({ hub: fakeHub({ updateAppointmentStatus: vi.fn(async () => Promise.reject(new HubApiError(409, "", STATUS_NO_SHOW_BEFORE_START_CODE))) }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "no_show");
    expect(s.state().statusError).toBe(NO_SHOW_TOO_EARLY);
  });

  it("409 not_live: learns the current status from the refusal, explains, refetches", async () => {
    const s = setup({
      hub: fakeHub({ updateAppointmentStatus: vi.fn(async () => Promise.reject(new HubApiError(409, "", NOT_LIVE_CODE, { code: NOT_LIVE_CODE, status: "cancelled" }))) }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(s.state().knownStatus["appt-e1"]).toBe("cancelled");
    expect(s.state().statusError).toBe(STATUS_NOT_LIVE);
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("a not_live refusal with a status this build does not know learns nothing", async () => {
    const s = setup({
      hub: fakeHub({ updateAppointmentStatus: vi.fn(async () => Promise.reject(new HubApiError(409, "x", NOT_LIVE_CODE, { status: "paused" }))) }),
    });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "attended");
    expect(s.state().knownStatus).toEqual({});
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

  it("opening reads the 24 h window AND the clinic flag", async () => {
    const s = await opened({ getPaidNoticesAutoApproved: vi.fn(async () => true) });
    expect(s.hub.getCancelPreview).toHaveBeenCalledWith(expect.anything(), "appt-e1");
    expect(s.state().check.status).toBe("loaded");
    expect(s.state().paidAuto).toBe(true);
  });

  it("unpaid: sends the three flags false, closes card and drawer, toasts the notice and refetches", async () => {
    const s = await opened();
    await s.controller.confirmRelease(gate);
    await flush();
    expect(call(s)[2]).toEqual({ acknowledge_retention: false, release_confirmed: false, notify_outside_window: false });
    expect(s.state().modal).toBeNull();
    expect(s.state().selectedId).toBeNull();
    expect(s.state().knownStatus["appt-e1"]).toBe("cancelled");
    expect(s.state().toast).toEqual({
      tone: "success",
      message: "Horário liberado e consulta cancelada. A secretarIA está avisando o paciente pelo WhatsApp.",
    });
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("outside the 24 h window the paid notice goes only with the opt-in — or the clinic flag", async () => {
    const off = await opened({ getCancelPreview: outsidePreview() });
    await off.controller.confirmRelease(gate);
    expect(call(off)[2].notify_outside_window).toBe(false);
    const on = await opened({ getCancelPreview: outsidePreview() });
    await on.controller.confirmRelease({ ...gate, optIn: true });
    expect(call(on)[2].notify_outside_window).toBe(true);
    const auto = await opened({ getCancelPreview: outsidePreview(), getPaidNoticesAutoApproved: vi.fn(async () => true) });
    await auto.controller.confirmRelease(gate);
    expect(call(auto)[2].notify_outside_window).toBe(true);
  });

  it("'Não perguntar novamente' with the opt-in saves the flag first; without the opt-in it is ignored", async () => {
    const s = await opened({ getCancelPreview: outsidePreview() });
    await s.controller.confirmRelease({ acknowledged: false, optIn: true, remember: true });
    expect(s.hub.approvePaidNotices).toHaveBeenCalledTimes(1);
    const n = await opened({ getCancelPreview: outsidePreview() });
    await n.controller.confirmRelease({ acknowledged: false, optIn: false, remember: true });
    expect(n.hub.approvePaidNotices).not.toHaveBeenCalled();
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
    await s.controller.confirmRelease({ ...gate, acknowledged: true });
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

  it("'the patient just confirmed': explained, and only a second click sends release_confirmed", async () => {
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
    const s = await opened({ releaseAppointment: vi.fn(async () => Promise.reject(new HubApiError(409, "gone", NOT_LIVE_CODE))) });
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

  it("a notice that did not go out is a danger toast with the free link", async () => {
    const s = await opened({
      releaseAppointment: vi.fn(async () => ({ ...wire("appt-e1", "cancelled"), patient_notice: "whatsapp_outside_window", whatsapp_link: "https://wa.me/5511999990000" })),
    });
    await s.controller.confirmRelease(gate);
    expect(s.state().toast).toMatchObject({ tone: "danger", link: { href: "https://wa.me/5511999990000", label: "Abrir conversa no WhatsApp" } });
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

  it("'Não perguntar novamente' with the paid send saves the flag first", async () => {
    const s = await opened();
    await s.controller.sendMessage("Oi", true, true);
    const approve = (s.hub.approvePaidNotices as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    const send = (s.hub.messageAppointment as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(approve).toBeLessThan(send);
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

describe("cancel — the patient notice from the response (R7)", () => {
  it("toasts patient_notice when the backend sends it (a Portal patient is now told)", async () => {
    const s = setup({
      hub: fakeHub({
        getCancelPreview: vi.fn(async () => preview({ whatsapp_link: null })),
        cancelAppointment: vi.fn(async (_s, id) => ({ ...wire(id, "cancelled"), patient_notice: "portal_chat_email" })),
      }),
    });
    await s.controller.start();
    await s.controller.openCancel(appt(s.state()));
    await s.controller.confirmCancel({ confirm: true, justification: null, notify_outside_window: false });
    expect(s.state().toast).toEqual({
      tone: "success",
      message: "Consulta cancelada. O aviso ficou na conversa do paciente no Portal, e um e-mail avisou que há mensagem.",
    });
  });

  it("'Não perguntar novamente' with the paid opt-in saves the flag before cancelling", async () => {
    const s = setup({ hub: fakeHub({ getCancelPreview: outsidePreview() }) });
    await s.controller.start();
    await s.controller.openCancel(appt(s.state()));
    await s.controller.confirmCancel({ confirm: true, justification: null, notify_outside_window: true }, true);
    const approve = (s.hub.approvePaidNotices as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    const cancel = (s.hub.cancelAppointment as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(approve).toBeLessThan(cancel);
  });
});
```

Also add `messageSentToast` to the import from `"../screen"`.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run lib/agenda/__tests__/confirmation.test.ts lib/agenda/__tests__/screen.test.ts`
Expected: FAIL (`applyStaffConfirmation`, `openRelease`, `choosePaidNotice`, `paidAuto`… missing; `tsc` errors on `fakeHub`).

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

(a) Imports — add:

```ts
import { isTerminal } from "./actions";
import { applyStaffConfirmation } from "./confirmation";
import { actionToast, decidePaidNotice, safeWaLink, type PaidNoticeAsk, type PaidNoticeDecision } from "./notice";
import { MESSAGE_FAILED, PAID_APPROVE_FAILED, RELEASE_FAILED, RELEASE_UNAVAILABLE, buildReleaseBody, validateMessage } from "./release";
import type { ReleaseConfirm } from "./release";
import {
  MESSAGE_NO_CHANNEL_CODE,
  MESSAGE_OUTSIDE_WINDOW_CODE,
  NOT_LIVE_CODE,
  RELEASE_ACK_REQUIRED_CODE,
  RELEASE_ALREADY_CONFIRMED_CODE,
  STATUS_NO_SHOW_BEFORE_START_CODE,
} from "./secretaria-hub-agenda";
```

add `canActOn` to the existing `./modal-forms` import list, and add `AppointmentMessagePayload, AppointmentMessageWire, AppointmentReleasePayload, AppointmentReleaseWire` to the existing type import from `./secretaria-hub-agenda` (Task 4 already added `AppointmentActionWire, AppointmentStatusPayload`).

(b) Copy — after `STATUS_FAILED` add:

```ts
export const STATUS_NOT_LIVE = "Esta consulta já foi cancelada ou encerrada. A agenda foi atualizada.";
export const NO_SHOW_TOO_EARLY = "Só é possível marcar falta depois do horário da consulta.";
export const RELEASED_BASE = "Horário liberado e consulta cancelada.";
export const CANCELLED_BASE = "Consulta cancelada.";
```

(c) Parse the alias. In `parseAgendaParams` replace `const consultaId = q.get("consultaId");` with:

```ts
  // `consulta` is the id R4's warning e-mail links with (…/agenda?consulta=<appointment_id>&data=<day>).
  const consultaId = q.get("consultaId") ?? q.get("consulta");
```

(d) After `export type CancelModalError = …;` add:

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

export type PaidNoticeModalError = { kind: "failed"; message: string };
```

and in the `AgendaModal` union, before the final `| null;`, add:

```ts
  /** "Enviar mesmo assim?" before Confirmar / Compareceu (spec 2026-10-09 §2): only when asking is needed. */
  | { type: "paid_notice"; appt: Appt; status: "confirmed" | "attended"; ask: PaidNoticeAsk; pending: boolean; error: PaidNoticeModalError | null }
  | { type: "release"; appt: Appt; pending: boolean; error: ReleaseModalError | null; forceAck: boolean; confirmedSeen: boolean }
  | { type: "message"; appt: Appt; pending: boolean; error: MessageModalError | null }
```

(e) State. In `AgendaState`, after `checkGeneration: number;` add:

```ts
  /** The clinic's standing authorisation for paid notices (R7), read with every window check. */
  paidAuto: boolean;
```

and `paidAuto: false,` in `INITIAL_AGENDA_STATE` (after `checkGeneration: 0,`).

(f) `TenantScopedAction`: change the `modal_error` member to `| { type: "modal_error"; error: string | CancelModalError | ReleaseModalError | MessageModalError | PaidNoticeModalError | null }` and add after the `known_status` member:

```ts
  /** The clinic just confirmed this appointment (PATCH confirmed answered): flip it live, before the refetch. */
  | { type: "confirmation_recorded"; appointmentId: string }
  /** The clinic flag as last read or stored. */
  | { type: "paid_auto_loaded"; value: boolean }
```

(g) In `applyScoped`, replace

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
    case "paid_auto_loaded":
      return { ...state, paidAuto: action.value };
```

(h) Toasts — after `rescheduledToast` add:

```ts
const STATUS_TOAST_BASE: Partial<Record<AppointmentStatusWire, string>> = {
  confirmed: "Consulta confirmada.",
  attended: "Presença registrada.",
  no_show: "Falta registrada.",
};

/** After Confirmar / Compareceu / Faltou: what changed + how the patient was told (R7 patient_notice). */
export function statusToast(res: AppointmentActionWire): Toast {
  const base = STATUS_TOAST_BASE[res.status] ?? `Status registrado: ${WIRE_STATUS_LABEL[res.status]}.`;
  return actionToast(base, res);
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

/** The wa.me link and cost of an outside-window refusal. The link is untrusted: only wa.me passes. */
function outsideWindowFacts(e: HubApiError): { costBrl: string; waLink: string | null } {
  const d = e.detail && typeof e.detail === "object" ? (e.detail as Record<string, unknown>) : {};
  return {
    costBrl: typeof d.template_cost_brl === "string" ? d.template_cost_brl : "",
    waLink: safeWaLink(d.whatsapp_link),
  };
}

/** The `status` a not_live refusal reports, when it is one this build knows. */
function refusedStatus(e: HubApiError): AppointmentStatusWire | null {
  const d = e.detail && typeof e.detail === "object" ? (e.detail as Record<string, unknown>) : {};
  const s = d.status;
  return typeof s === "string" && Object.prototype.hasOwnProperty.call(WIRE_STATUS_LABEL, s) ? (s as AppointmentStatusWire) : null;
}
```

(i) `AgendaHub`: change `cancelAppointment`'s return to `Promise<AppointmentActionWire>` and add after `updateAppointmentStatus`:

```ts
  releaseAppointment: (s: Session, appointmentId: string, payload: AppointmentReleasePayload) => Promise<AppointmentReleaseWire>;
  messageAppointment: (s: Session, appointmentId: string, payload: AppointmentMessagePayload) => Promise<AppointmentMessageWire>;
  getPaidNoticesAutoApproved: (s: Session) => Promise<boolean>;
  approvePaidNotices: (s: Session) => Promise<boolean>;
```

(j) `loadCheck` reads the clinic flag with the preview. Replace its `try { … }` body with:

```ts
    try {
      // The flag travels with the window: a card decides "ask or not" from both.
      // A failed flag read counts as "not approved" (the clinic is asked, never billed silently).
      const [preview, auto] = await Promise.all([
        hub.getCancelPreview(s, appointmentId),
        hub.getPaidNoticesAutoApproved(s).catch(() => false),
      ]);
      dispatch(scoped(s.tenantId, { type: "paid_auto_loaded", value: auto }));
      dispatch(scoped(s.tenantId, { type: "check_loaded", generation, preview }));
    }
```

(k) After `onWriteError`, add the shared "Não perguntar novamente" step:

```ts
  /**
   * "Não perguntar novamente": stores the clinic's standing authorisation BEFORE the
   * action. false = it could not be stored; the open sheet shows why and nothing
   * else is attempted (spec: never bill on an authorisation that did not land).
   */
  async function rememberPaidNotices(s: Session): Promise<boolean> {
    try {
      const stored = await hub.approvePaidNotices(s);
      dispatch(scoped(s.tenantId, { type: "paid_auto_loaded", value: stored }));
      return true;
    } catch (e) {
      console.error("agenda: failed to save the paid-notice authorisation", errorFacts(e));
      if (isTenantSwitched(e)) {
        await fresh();
        return false;
      }
      dispatch(scoped(s.tenantId, { type: "modal_error", error: { kind: "failed", message: PAID_APPROVE_FAILED } }));
      return false;
    }
  }

  /** A live appointment with a local id: not a block, not Google-only, not cancelled/attended/no-show (read or learned). */
  function actionable(appt: Appt): boolean {
    if (!canActOn(appt) || !appt.appointmentId) return false;
    return !isTerminal(appt, getState().knownStatus[appt.appointmentId] ?? null);
  }

  /**
   * Window + flag for a one-click action. Only a WhatsApp patient (R7 `patient_channel`)
   * can be asked, so nobody else costs a lookup. Failures never bill: no preview -> act unpaid.
   */
  async function paidNoticeDecision(s: Session, appt: Appt): Promise<PaidNoticeDecision> {
    if (appt.patientChannel !== "whatsapp" || !appt.appointmentId) return { kind: "send", notify: false };
    const appointmentId = appt.appointmentId;
    const [preview, auto] = await Promise.all([
      hub.getCancelPreview(s, appointmentId).catch((e: unknown) => {
        console.error("agenda: failed to read cancel preview", errorFacts(e));
        return null;
      }),
      hub.getPaidNoticesAutoApproved(s).catch(() => false),
    ]);
    return decidePaidNotice(preview, auto, appt.patientChannel);
  }
```

(l) `confirmCancel`: change its signature to `async function confirmCancel(body: AppointmentCancelPayload, remember = false): Promise<void>`. Inside, directly before `try { const res = await hub.cancelAppointment(s, appointmentId, body);` insert:

```ts
      if (body.notify_outside_window && remember && !getState().paidAuto && !(await rememberPaidNotices(s))) return;
```

and replace `own({ type: "toast", toast: cancelledToast(after, body) });` with:

```ts
        // R7 reports what really happened to the patient (the Portal is now told too); an older
        // backend without the key keeps the verdict-based toast.
        own({ type: "toast", toast: res.patient_notice === undefined ? cancelledToast(after, body) : actionToast(CANCELLED_BASE, res) });
```

(m) Replace the whole `setStatus` function with:

```ts
  /**
   * Confirmar / Compareceu / Faltou (PATCH status, R7). Confirmar and Compareceu tell the
   * patient, so they first decide whether a PAID WhatsApp notice must be asked
   * (lib/agenda/notice.ts::decidePaidNotice): when it must, nothing is sent and the
   * "Enviar mesmo assim?" sheet opens; otherwise the PATCH goes at once. Faltou never
   * notifies. The drawer only offers what the clock allows (lib/agenda/actions.ts);
   * the backend's 409s are the backstop and are explained, never retried.
   */
  async function setStatus(appt: Appt, status: StatusActionWire): Promise<void> {
    const st = getState();
    if (!canWrite(st) || !appt.appointmentId || st.statusPending) return;
    const appointmentId = appt.appointmentId;
    dispatch({ type: "status_pending", status });
    const s = await fresh();
    if (!s) {
      dispatch({ type: "status_pending", status: null });
      return;
    }
    let notify = false;
    if (status === "confirmed" || status === "attended") {
      const decision = await paidNoticeDecision(s, appt);
      if (getState().tenantId !== s.tenantId) return; // the clinic changed meanwhile: nothing is sent
      if (decision.kind === "ask") {
        dispatch(scoped(s.tenantId, { type: "status_done", error: null }));
        dispatch({
          type: "open_modal",
          modal: { type: "paid_notice", appt, status, ask: { costLabel: decision.costLabel, waLink: decision.waLink }, pending: false, error: null },
        });
        return;
      }
      notify = decision.notify;
    }
    await patchStatus(s, appointmentId, status, notify);
  }

  async function patchStatus(s: Session, appointmentId: string, status: StatusActionWire, notify: boolean): Promise<void> {
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      const res = await hub.updateAppointmentStatus(s, appointmentId, { status, notify_outside_window: notify });
      own({ type: "known_status", appointmentId, status: res.status });
      if (status === "confirmed") own({ type: "confirmation_recorded", appointmentId });
      own({ type: "status_done", error: null });
      own({ type: "toast", toast: statusToast(res) });
      refreshAfter(s.tenantId);
    } catch (e) {
      console.error("agenda: failed to update appointment status", errorFacts(e));
      if (isTenantSwitched(e)) {
        await fresh();
        return;
      }
      if (e instanceof HubApiError && e.status === 409 && e.code === STATUS_NO_SHOW_BEFORE_START_CODE) {
        own({ type: "status_done", error: e.message || NO_SHOW_TOO_EARLY }); // nothing changed: no refetch
        return;
      }
      if (e instanceof HubApiError && e.status === 409 && e.code === NOT_LIVE_CODE) {
        const current = refusedStatus(e);
        if (current) own({ type: "known_status", appointmentId, status: current });
        own({ type: "status_done", error: e.message || STATUS_NOT_LIVE });
        refreshAfter(s.tenantId);
        return;
      }
      own({ type: "status_done", error: isCalendarNotConnected(e) ? CALENDAR_NOT_CONNECTED_WRITE : STATUS_FAILED });
    }
  }

  /** The "Enviar mesmo assim?" sheet's answer. "Não perguntar novamente" only counts with send. */
  async function choosePaidNotice(choice: { send: boolean; remember: boolean }): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "paid_notice" || m.pending || !m.appt.appointmentId) return;
    const { status } = m;
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("paid_notice");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      if (choice.send && choice.remember && !(await rememberPaidNotices(s))) return;
      own({ type: "modal_done" });
      dispatch({ type: "status_pending", status });
      await patchStatus(s, appointmentId, status, choice.send);
    } finally {
      own({ type: "modal_pending", pending: false });
    }
  }
```

(Old test: the expected toast of `"PATCHes, reflects the returned status and refetches"` is still a success toast — "Falta registrada.")

(n) Add the four release/message methods after `choosePaidNotice`:

```ts
  /** Opens the release card and reads the 24 h window + clinic flag (same lookup as the cancel card). */
  function openRelease(appt: Appt): Promise<void> {
    if (!canWrite(getState()) || !appt.appointmentId || !actionable(appt)) return Promise.resolve();
    dispatch({
      type: "open_modal",
      modal: { type: "release", appt, pending: false, error: null, forceAck: false, confirmedSeen: false },
    });
    return loadCheck(appt.appointmentId);
  }

  /**
   * "Liberar horário". Nothing is sent while the Pix-retention gate is open (a PAID
   * deposit, or the backend asked for it) or while the 24 h verdict is still being
   * read. The paid notice goes with the opt-in ("Enviar mesmo assim") or the clinic
   * flag. Any failure keeps the card open with its error and leaves the appointment
   * exactly as it was.
   */
  async function confirmRelease(c: ReleaseConfirm): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "release" || m.pending || !m.appt.appointmentId) return;
    const st = getState();
    const notice = cancelNoticeState(m.appt, st.check);
    if (notice.kind === "loading") return;
    const outside = notice.kind === "whatsapp_outside_window";
    const body = buildReleaseBody(m.appt, {
      acknowledged: c.acknowledged,
      forceAck: m.forceAck,
      confirmedSeen: m.confirmedSeen,
      notifyOutsideWindow: outside && (c.optIn || st.paidAuto),
    });
    if (!body) return; // the acknowledgement step is not done: nothing is sent
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("release");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      if (outside && c.optIn && c.remember && !st.paidAuto && !(await rememberPaidNotices(s))) return;
      const res = await hub.releaseAppointment(s, appointmentId, body);
      own({ type: "known_status", appointmentId, status: "cancelled" });
      own({ type: "cancel_done" });
      own({ type: "toast", toast: actionToast(RELEASED_BASE, res ?? {}) });
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
      } else if (e instanceof HubApiError && e.status === 409 && e.code === NOT_LIVE_CODE) {
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
    if (!canWrite(getState()) || !appt.appointmentId || !actionable(appt)) return;
    dispatch({ type: "open_modal", modal: { type: "message", appt, pending: false, error: null } });
  }

  async function sendMessage(text: string, notifyOutsideWindow: boolean, remember = false): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "message" || m.pending || !m.appt.appointmentId) return;
    const checked = validateMessage(text);
    if (!checked.ok) return; // the sheet keeps its send button disabled; this is the backstop
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("message");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      if (notifyOutsideWindow && remember && !(await rememberPaidNotices(s))) return;
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

and add `choosePaidNotice, openRelease, confirmRelease, openMessage, sendMessage,` to the controller's returned object, after `setStatus,`.

- [ ] **Step 5: Run tests and type check**

Run: `npx vitest run lib/agenda` — Expected: PASS (the existing cancel/reschedule/status tests too: the cancel "success" test's fake answers no `patient_notice`, so its verdict toast still says "NÃO foi avisado").
Run: `npm run typecheck` — Expected: errors only in `components/agenda/AgendaScreen.tsx` (`HUB` lacks the four new members); fix them now by importing `approvePaidNotices`, `getPaidNoticesAutoApproved`, `messageAppointment`, `releaseAppointment` from `@/lib/agenda/secretaria-hub-agenda` and adding the four to the `HUB` object. Re-run: no errors.

- [ ] **Step 6: Commit**

```bash
git add lib/agenda/confirmation.ts lib/agenda/screen.ts components/agenda/AgendaScreen.tsx lib/agenda/__tests__/confirmation.test.ts lib/agenda/__tests__/screen.test.ts
git commit -m "feat(agenda): status actions ask before paid notices; release, message, guards, deep link and live flip (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 6: Sheets — "Enviar mesmo assim?", ReleaseCard, MessageModal, the flag-aware cancel card, the toast link, and the wiring

**Files:**
- Create: `components/agenda/PaidNoticeSheet.tsx`, `components/agenda/ReleaseCard.tsx`, `components/agenda/MessageModal.tsx`
- Modify: `lib/agenda/cancel-preview.ts` (copy, header comment), `lib/agenda/modal-forms.ts` (`reachFor` reads `Appt.patientChannel`; `cancelWillNotify`), `components/agenda/CancelCard.tsx`, `components/agenda/agenda-modals.css`, `components/agenda/agenda-screen.css`, `components/agenda/AgendaView.tsx`, `components/agenda/AgendaScreen.tsx`
- Test: `components/agenda/__tests__/paid-notice-sheet.test.tsx`, `components/agenda/__tests__/release-card.test.tsx`, `components/agenda/__tests__/message-modal.test.tsx` (new); `components/agenda/__tests__/cancel-card.test.tsx`, `lib/agenda/__tests__/modal-forms.test.ts`, `components/agenda/__tests__/agenda-view.test.tsx` (modify/append)

**Interfaces:**
- Consumes: Task 4 copy/rules (`RELEASE_*`, `MESSAGE_*`, `ReleaseConfirm`, `safeWaLink`, `PaidNoticeAsk`), Task 5 modal types, `paidAuto` and controller methods; `Appt.patientChannel` (Task 1); `PatientReach.channel` (already in `cancel-preview.ts`); existing `Sheet`, `Button`, `TextArea`, `Icon`, `cancelNoticeState`, `WindowCheck`.
- Produces:
  - `CancelNoticeBlock` gains optional `autoApproved?: boolean`, `remember?: boolean`, `onRemember?: (next: boolean) => void` (the "Não perguntar novamente" checkbox renders only when `onRemember` is given, and is enabled only while the paid opt-in is ticked). `CANCEL_COPY.autoApproved`, `CANCEL_COPY.rememberLabel`.
  - `CancelCardViewProps` gain `autoApproved?: boolean`, `remember: boolean`, `onRemember`; `onConfirm(body: CancelBody, remember: boolean)`. `CancelCardProps` gains `autoApproved?: boolean`.
  - `PaidNoticeSheetView(props: PaidNoticeSheetViewProps)` / `PaidNoticeSheet(props)`; `PAID_NOTICE_COPY`.
  - `ReleaseCardView` / `ReleaseCard` (`autoApproved`, `remember`); `MessageModalView` / `MessageModal` (`remember`, `onSend(text, notify, remember)`).
  - New **optional** `AgendaViewProps`: `onOpenRelease?`, `onConfirmRelease?: (c: ReleaseConfirm) => void`, `onOpenMessage?`, `onSendMessage?: (text: string, notify: boolean, remember: boolean) => void`, `onChoosePaidNotice?: (choice: { send: boolean; remember: boolean }) => void`; `onConfirmCancel: (body: CancelBody, remember: boolean) => void`.

```ts
export type PaidNoticeSheetViewProps = {
  open: boolean; appt: Appt | null; status: "confirmed" | "attended" | null; ask: PaidNoticeAsk | null;
  pending: boolean; error: PaidNoticeModalError | null;
  remember: boolean; onRemember: (next: boolean) => void;
  onClose: () => void; onChoose: (choice: { send: boolean; remember: boolean }) => void;
};
export type ReleaseCardViewProps = {
  open: boolean; appt: Appt | null; check: WindowCheck; autoApproved: boolean; forceAck: boolean; confirmedSeen: boolean;
  pending: boolean; error: ReleaseModalError | null;
  ack: boolean; onAck: (next: boolean) => void; optIn: boolean; onOptIn: (next: boolean) => void;
  remember: boolean; onRemember: (next: boolean) => void;
  onClose: () => void; onConfirm: (c: ReleaseConfirm) => void;
};
export type MessageModalViewProps = {
  open: boolean; appt: Appt | null; pending: boolean; error: MessageModalError | null;
  text: string; onText: (next: string) => void; optIn: boolean; onOptIn: (next: boolean) => void;
  remember: boolean; onRemember: (next: boolean) => void;
  onClose: () => void; onSend: (text: string, notifyOutsideWindow: boolean, remember: boolean) => void;
};
```

- [ ] **Step 1: Write the failing tests**

Create `components/agenda/__tests__/paid-notice-sheet.test.tsx`:

```tsx
// "Enviar mesmo assim?" before Confirmar / Compareceu (spec 2026-10-09 §2).
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { PAID_NOTICE_COPY, PaidNoticeSheetView, type PaidNoticeSheetViewProps } from "../PaidNoticeSheet";
import type { Appt } from "../../../lib/agenda/types";

const noop = () => {};
const appt: Appt = { id: "hub-1", date: "2026-09-29", day: 2, start: 540, dur: 30, patient: "Maria Souza", status: "agendado", appointmentId: "ap-1" };
const render = (over: Partial<PaidNoticeSheetViewProps> = {}) =>
  renderToStaticMarkup(
    <PaidNoticeSheetView
      open appt={appt} status="confirmed" ask={{ costLabel: "R$ 0,35", waLink: "https://wa.me/5511999990000" }}
      pending={false} error={null} remember={false} onRemember={noop} onClose={noop} onChoose={noop} {...over}
    />,
  );
const tag = (html: string, text: string) => {
  const m = new RegExp(`<button[^>]*>(?:(?!</button>).)*${text}(?:(?!</button>).)*</button>`).exec(html);
  if (!m) throw new Error(`no button "${text}"`);
  return m[0];
};

describe("PaidNoticeSheet", () => {
  it("names the patient, the 24 h window and the cost", () => {
    const html = render();
    expect(html).toContain(PAID_NOTICE_COPY.titleConfirmed);
    expect(html).toContain("Maria Souza está fora da janela de 24 h do WhatsApp");
    expect(html).toContain("(R$ 0,35)");
  });

  it("titles Compareceu as 'Registrar presença'", () => {
    expect(render({ status: "attended" })).toContain(PAID_NOTICE_COPY.titleAttended);
  });

  it("offers Enviar mesmo assim, Não avisar and Voltar, plus the free wa.me link", () => {
    const html = render();
    expect(tag(html, PAID_NOTICE_COPY.send)).not.toContain("disabled");
    expect(tag(html, PAID_NOTICE_COPY.skip)).not.toContain("disabled");
    expect(tag(html, PAID_NOTICE_COPY.back)).not.toContain("disabled");
    expect(html).toContain('href="https://wa.me/5511999990000"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("'Não perguntar novamente' is a labelled checkbox", () => {
    const html = render({ remember: true });
    const id = /<input id="([^"]+)" type="checkbox"/.exec(html)![1];
    expect(html).toContain(`<label for="${id}">${PAID_NOTICE_COPY.remember}</label>`);
    expect(html).toContain('checked=""');
  });

  it("no cost configured: no price quoted; no link: none drawn", () => {
    const html = render({ ask: { costLabel: "", waLink: null } });
    expect(html).not.toContain("(R$");
    expect(html).not.toContain("href=");
  });

  it("pending locks every control; an error is announced", () => {
    const pending = render({ pending: true });
    expect(tag(pending, PAID_NOTICE_COPY.pending)).toContain('aria-busy="true"');
    expect(tag(pending, PAID_NOTICE_COPY.skip)).toContain("disabled");
    const failed = render({ error: { kind: "failed", message: "Não salvou." } });
    expect(failed).toMatch(/role="alert"[^>]*>(?:(?!<\/p>).)*Não salvou\./);
  });
});
```

Create `components/agenda/__tests__/release-card.test.tsx`:

```tsx
// ReleaseCard — the Pix-retention acknowledgement gate, the 24 h notice with the clinic flag, and the failure states.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { ReleaseCardView, type ReleaseCardViewProps } from "../ReleaseCard";
import {
  RELEASE_ACK_REQUIRED, RELEASE_ALREADY_CONFIRMED, RELEASE_ALREADY_DONE, RELEASE_COPY, RELEASE_FAILED,
} from "../../../lib/agenda/release";
import { CANCEL_COPY } from "../../../lib/agenda/cancel-preview";
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
      open appt={base} check={inside} autoApproved={false} forceAck={false} confirmedSeen={false} pending={false} error={null}
      ack={false} onAck={noop} optIn={false} onOptIn={noop} remember={false} onRemember={noop} onClose={noop} onConfirm={noop} {...over}
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
    expect(html).toContain(`>${RELEASE_COPY.ackLabel}</label>`);
    expect(tag(html, RELEASE_COPY.confirm)).toContain('disabled=""');
    expect(tag(render({ appt: paid, ack: true }), RELEASE_COPY.confirm)).not.toContain("disabled");
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

  it("outside the 24 h window: the paid opt-in and 'Não perguntar novamente' are offered; the button says what it will do", () => {
    const off = render({ check: outside });
    expect(off).toContain(CANCEL_COPY.rememberLabel);
    expect(tag(off, RELEASE_COPY.confirm)).not.toContain("disabled");
    expect(tag(render({ check: outside, optIn: true }), RELEASE_COPY.confirmPaidNotice)).not.toContain("disabled");
  });

  it("with the clinic flag on: no question, the paid notice is announced", () => {
    const html = render({ check: outside, autoApproved: true });
    expect(html).toContain(CANCEL_COPY.autoApproved);
    expect(html).not.toContain(CANCEL_COPY.rememberLabel);
    expect(tag(html, RELEASE_COPY.confirmPaidNotice)).not.toContain("disabled");
  });

  it("while the 24 h verdict loads the confirm button is disabled and says so", () => {
    expect(tag(render({ check: { status: "loading" } }), RELEASE_COPY.checking)).toContain("disabled");
  });

  it("'already confirmed': explained, and the button becomes 'Liberar mesmo assim'", () => {
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
import { CANCEL_COPY } from "../../../lib/agenda/cancel-preview";
import type { Appt } from "../../../lib/agenda/types";

const noop = () => {};
const appt: Appt = { id: "hub-1", date: "2026-09-29", day: 2, start: 540, dur: 30, patient: "Maria Souza", status: "agendado", appointmentId: "ap-1" };
const render = (over: Partial<MessageModalViewProps> = {}) =>
  renderToStaticMarkup(
    <MessageModalView
      open appt={appt} pending={false} error={null} text="Olá" onText={noop} optIn={false} onOptIn={noop}
      remember={false} onRemember={noop} onClose={noop} onSend={noop} {...over}
    />,
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

  it("outside the 24 h window: explains, offers the paid message with its cost, 'Não perguntar novamente' and the free wa.me link; send waits for the opt-in", () => {
    const error = { kind: "outside_window", costBrl: "R$ 0,35", waLink: "https://wa.me/5511999990000" } as const;
    const html = render({ error });
    expect(html).toContain(MESSAGE_COPY.outsideTitle);
    expect(html).toContain("R$ 0,35");
    expect(html).toContain(CANCEL_COPY.rememberLabel);
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

In `components/agenda/__tests__/cancel-card.test.tsx`:
- add `remember={false} onRemember={noop}` to the `CancelCardView` props of the `view` helper;
- keep the test `it("no phone: says the patient will NOT be notified", …)` as it is (a patient with neither WhatsApp nor Portal) and add after it:

```tsx
  it("a Portal patient (patient_channel brain_message): told in the chat, never asked to pay — whatever the preview says", () => {
    const portal = { ...appt, patientChannel: "brain_message" as const };
    for (const check of [loaded({ whatsapp_link: null }), loaded({ inside_window: false })]) {
      const html = view({ appt: portal, check });
      expect(html).toContain(CANCEL_COPY.portal);
      expect(html).not.toContain('type="checkbox"');
      expect(confirmText(html)).toBe("Cancelar e avisar paciente");
    }
  });
```

- append inside `describe("CancelCard notice per kind (exact copy)", …)`:

```tsx
  it("outside window: 'Não perguntar novamente' is labelled and only usable once the paid opt-in is ticked", () => {
    const off = view({ check: loaded({ inside_window: false }) });
    expect(off).toContain(CANCEL_COPY.rememberLabel);
    const ids = [...off.matchAll(/<input id="([^"]+)" type="checkbox"([^>]*)>/g)];
    expect(ids).toHaveLength(2);
    expect(ids[1][2]).toContain("disabled");
    const on = view({ check: loaded({ inside_window: false }), optIn: true });
    expect([...on.matchAll(/<input id="([^"]+)" type="checkbox"([^>]*)>/g)][1][2]).not.toContain("disabled");
  });

  it("clinic flag on: no question; the paid notice is announced and the button says so", () => {
    const html = view({ check: loaded({ inside_window: false }), autoApproved: true });
    expect(html).toContain(CANCEL_COPY.autoApproved);
    expect(html).not.toContain('type="checkbox"');
    expect(confirmText(html)).toBe("Cancelar e enviar aviso pago");
  });
```

In `lib/agenda/__tests__/modal-forms.test.ts`, in `it("labels the confirm button by outcome", …)` change the line
`expect(cancelConfirmLabel(describeCancelNotice(null, { hasPhone: true, channel: "portal" }), true)).toBe("Cancelar sem avisar");`
to
`expect(cancelConfirmLabel(describeCancelNotice(null, { hasPhone: true, channel: "portal" }), true)).toBe("Cancelar e avisar paciente");`
(R7 tells a Portal patient in the chat; the no-contact case keeps "Cancelar sem avisar"), and append to the same file:

```ts
describe("reachFor reads the patient's channel (R7 patient_channel)", () => {
  it("a Portal patient is 'portal' even when the appointment carries a contact phone or the preview a link", () => {
    expect(reachFor(appt({ patientChannel: "brain_message", phone: "5511988887777" }), loaded(preview()))).toEqual({
      hasPhone: false,
      channel: "portal",
    });
  });

  it("a WhatsApp patient or an absent channel keeps the phone/link rule", () => {
    expect(reachFor(appt({ patientChannel: "whatsapp" }), loaded(preview()))).toEqual({ hasPhone: true });
    expect(reachFor(appt(), loaded(preview({ whatsapp_link: null })))).toEqual({ hasPhone: false });
  });

  it("cancelWillNotify: the Portal is notified (in the chat), no contact is not", () => {
    expect(cancelWillNotify(describeCancelNotice(null, { hasPhone: false, channel: "portal" }), false)).toBe(true);
    expect(cancelWillNotify(describeCancelNotice(null, { hasPhone: false }), false)).toBe(false);
  });
});
```

(The file's existing `appt()`, `loaded()` and `preview()` helpers are reused; add `cancelWillNotify` to its import list from `"../modal-forms"` — `reachFor` is already there.)

Append to `components/agenda/__tests__/agenda-view.test.tsx` (add `type AgendaState` is already imported; add `import type { Appt } …` is already there):

```tsx
describe("AgendaView — R5 sheets and the notice toast", () => {
  const red: Appt = { ...ITEM, start: 11 * 60, confirmation: { state: "attention", count: 0 } }; // 11:00, after NOW (10:00)
  const withModal = (modal: NonNullable<AgendaState["modal"]>): AgendaState => ({ ...loaded([red]), selectedId: red.id, modal });

  it("renders the release card for an open release modal", () => {
    const html = render(withModal({ type: "release", appt: red, pending: false, error: null, forceAck: false, confirmedSeen: false }));
    expect(html).toContain("O horário volta a ficar livre na agenda");
  });

  it("renders the message sheet for an open message modal", () => {
    expect(render(withModal({ type: "message", appt: red, pending: false, error: null }))).toContain("Mensagem para o paciente");
  });

  it("renders the paid-notice sheet for an open paid_notice modal", () => {
    const html = render(withModal({ type: "paid_notice", appt: red, status: "confirmed", ask: { costLabel: "R$ 0,35", waLink: null }, pending: false, error: null }));
    // The sheet's footer is always in the markup (Sheet keeps it mounted); the body only when open with an appt.
    expect(html).toContain("está fora da janela de 24 h do WhatsApp");
  });

  it("the drawer's before-start actions are enabled when the screen is writable and wired", () => {
    const html = render({ ...loaded([red]), selectedId: red.id }, { onOpenRelease: noop, onOpenMessage: noop });
    expect(buttonTag(html, "Liberar horário")).not.toContain("disabled");
    expect(buttonTag(html, "Enviar mensagem")).not.toContain("disabled");
    expect(buttonTag(html, "Confirmar")).not.toContain("disabled");
  });

  it("a caller that does not pass the new callbacks still renders (those actions disabled)", () => {
    expect(buttonTag(render({ ...loaded([red]), selectedId: red.id }), "Liberar horário")).toContain("disabled");
  });

  it("a toast with a free WhatsApp alternative carries the link; anything else is not rendered as a link", () => {
    const link = { href: "https://wa.me/5511999990000", label: "Abrir conversa no WhatsApp" };
    const ok = render(reduce([{ type: "toast", toast: { tone: "danger", message: "O paciente NÃO foi avisado", link } }], loaded()));
    expect(ok).toContain('href="https://wa.me/5511999990000"');
    expect(ok).toContain('rel="noopener noreferrer"');
    const bad = render(reduce([{ type: "toast", toast: { tone: "danger", message: "x", link: { href: "javascript:alert(1)", label: "x" } } }], loaded()));
    expect(bad).not.toContain("javascript:");
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run components/agenda lib/agenda/__tests__/modal-forms.test.ts`
Expected: FAIL (new modules missing; copy constants missing; labels differ).

- [ ] **Step 3: Copy and the channel rule (R7 `patient_channel`)**

In `lib/agenda/cancel-preview.ts`, inside `CANCEL_COPY`, replace the `portal` entry (it said the Portal is never told — R7 tells it now) and add two entries after `openWhatsapp`:

```ts
  portal:
    "Este paciente usa o Portal: a secretarIA deixa o aviso na conversa dele e um e-mail avisa que há mensagem. Sem custo.",
```

```ts
  autoApproved: "Sua clínica autorizou os avisos pagos: o aviso oficial do WhatsApp será enviado ao paciente",
  rememberLabel: "Não perguntar novamente (autorizar sempre os avisos pagos)",
```

In the header comment of `cancel-preview.ts`, replace the bullet that starts "`workers/tasks.py::send_cancellation_notice` sends ONLY through WhatsAppClient" (through "there is no channel field.") with:

```ts
// - Since R7 the events read carries the patient's own channel (`patient_channel`),
//   mapped to Appt.patientChannel; modal-forms.ts::reachFor turns a Portal patient
//   into `channel: "portal"` (told in the chat + an e-mail nudge, never billed).
//   Anyone else is reached by WhatsApp through the appointment's number.
```

In `lib/agenda/modal-forms.ts`, replace the whole `reachFor` function (with its doc comment) with:

```ts
/**
 * Who can the secretarIA reach? A Portal patient (R7 `patient_channel`
 * "brain_message") is told in the chat — never through WhatsApp, whatever number
 * the appointment or the preview carries. Anyone else: a phone on the Appt
 * settles it; otherwise the loaded preview's link does. A failed lookup with no
 * phone on hand is treated as "maybe reachable", so describeCancelNotice answers
 * preview_unavailable instead of a false "no phone".
 */
export function reachFor(appt: Appt, check: WindowCheck): PatientReach {
  if (appt.patientChannel === "brain_message") return { hasPhone: false, channel: "portal" };
  if (appt.phone && appt.phone.trim() !== "") return { hasPhone: true };
  if (check.status === "loaded") return { hasPhone: Boolean(check.preview.whatsapp_link) };
  return { hasPhone: true };
}
```

and in `cancelWillNotify` replace

```ts
    case "portal":
    case "no_contact": return false;
```

with

```ts
    case "portal": return true; // R7: told in the chat (+ e-mail nudge), free
    case "no_contact": return false;
```

- [ ] **Step 4: Flag-aware notice block and cancel card (`components/agenda/CancelCard.tsx`)**

Replace the `CancelNoticeBlock` signature and its `whatsapp_outside_window` branch:

```tsx
export function CancelNoticeBlock({
  state,
  optIn,
  onOptIn,
  disabled = false,
  autoApproved = false,
  remember = false,
  onRemember,
}: {
  state: CancelNoticeState;
  optIn: boolean;
  onOptIn: (next: boolean) => void;
  disabled?: boolean;
  /** The clinic flag (R7): the paid notice goes without asking. */
  autoApproved?: boolean;
  remember?: boolean;
  /** Renders "Não perguntar novamente" (usable only while the opt-in is ticked). */
  onRemember?: (next: boolean) => void;
}) {
  const optInId = useId();
  const rememberId = useId();
```

and replace the block `{state.kind === "whatsapp_outside_window" && ( <> … </> )}` with:

```tsx
      {state.kind === "whatsapp_outside_window" && (
        <>
          {autoApproved ? (
            <p>
              {CANCEL_COPY.autoApproved} ({state.costLabel}).
            </p>
          ) : (
            <>
              <div className="agm-optin">
                <input
                  id={optInId}
                  type="checkbox"
                  checked={optIn}
                  disabled={disabled}
                  onChange={(e) => onOptIn(e.target.checked)}
                />
                <label htmlFor={optInId}>
                  {state.optInLabel} ({state.costLabel})
                </label>
              </div>
              {onRemember && (
                <div className="agm-optin">
                  <input
                    id={rememberId}
                    type="checkbox"
                    checked={remember && optIn}
                    disabled={disabled || !optIn}
                    onChange={(e) => onRemember(e.target.checked)}
                  />
                  <label htmlFor={rememberId}>{CANCEL_COPY.rememberLabel}</label>
                </div>
              )}
              <p>{state.optOutNote}</p>
            </>
          )}
          {state.whatsappLink && (
            <a className="agm-link" href={state.whatsappLink} target="_blank" rel="noopener noreferrer">
              {CANCEL_COPY.openWhatsapp}
            </a>
          )}
        </>
      )}
```

In `CancelCardViewProps` add after `optIn: boolean;`:

```tsx
  /** The clinic flag (R7): outside 24 h the paid notice goes without asking. */
  autoApproved?: boolean;
  remember: boolean;
  onRemember: (next: boolean) => void;
```

and change `onConfirm: (body: CancelBody) => void;` to `onConfirm: (body: CancelBody, remember: boolean) => void;`.

In `CancelCardView`, replace the first lines of the body

```tsx
  const body = cancelSubmission(state, p.justification, p.optIn);
```

with

```tsx
  const paid = p.optIn || p.autoApproved === true;
  const body = cancelSubmission(state, p.justification, paid);
```

replace `const notSent = cancelWillNotify(state, p.optIn) === false;` with `const notSent = cancelWillNotify(state, paid) === false;`, the confirm button's `onClick={!blocked && body ? () => p.onConfirm(body) : undefined}` with `onClick={!blocked && body ? () => p.onConfirm(body, p.remember && p.optIn) : undefined}`, its label `cancelConfirmLabel(state, p.optIn)` with `cancelConfirmLabel(state, paid)`, and the notice block with:

```tsx
            <CancelNoticeBlock
              state={state}
              optIn={p.optIn}
              onOptIn={p.onOptIn}
              disabled={p.pending}
              autoApproved={p.autoApproved === true}
              remember={p.remember}
              onRemember={p.onRemember}
            />
```

In the stateful shell: change `CancelCardProps` to `Omit<CancelCardViewProps, "reason" | "justification" | "optIn" | "remember" | "onReason" | "onJustification" | "onOptIn" | "onRemember">`; add `const [remember, setRemember] = useState(false);`, `setRemember(false);` inside the reset effect, and pass `remember={remember} onRemember={setRemember}` to `CancelCardView`.

- [ ] **Step 5: CSS**

Append to `components/agenda/agenda-modals.css`:

```css
/* Release card, message sheet, paid-notice sheet (TASK-032 R5). No motion on purpose. */
.agm-release-ack {
  display: flex; flex-direction: column; gap: var(--sp-2);
  padding: var(--sp-3); border: 2px solid var(--st-attn-bd); background: var(--st-attn-bg); border-radius: var(--radius-control);
}
.agm-release-ack-title { margin: 0; font-size: var(--fs-md); font-weight: 700; color: var(--st-attn-ink); }
.agm-release-ack p { margin: 0; color: var(--ink); }
.agm-release-outside { display: flex; flex-direction: column; gap: var(--sp-2); }
.agm-paid-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); justify-content: flex-end; }
```

Append to `components/agenda/agenda-screen.css`:

```css
/* The toast's free alternative (wa.me), TASK-032 R5. */
.agenda-toast-link { color: inherit; font-weight: 600; text-decoration: underline; white-space: nowrap; }
```

- [ ] **Step 6: `components/agenda/PaidNoticeSheet.tsx`**

```tsx
"use client";

// PaidNoticeSheet — "Enviar mesmo assim?" (spec 2026-10-09 §2). Opens ONLY when
// Confirmar or Compareceu would notify a WhatsApp patient outside the 24 h window
// and the clinic has no standing authorisation (lib/agenda/notice.ts::decidePaidNotice).
// Three answers: send the paid notice, do the action without notifying, or go back
// (nothing happens). "Não perguntar novamente" stores the authorisation and only
// counts together with "Enviar mesmo assim". Stateless view + stateful shell (the
// checkbox resets on every open).

import { useEffect, useId, useState } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { Icon } from "../icons";
import type { Appt } from "../../lib/agenda/types";
import type { PaidNoticeAsk } from "../../lib/agenda/notice";
import type { PaidNoticeModalError } from "../../lib/agenda/screen";
import "./agenda-modals.css";

export const PAID_NOTICE_COPY = {
  titleConfirmed: "Confirmar consulta",
  titleAttended: "Registrar presença",
  body: "está fora da janela de 24 h do WhatsApp. Para avisá-lo agora, a secretarIA precisa enviar a mensagem oficial do WhatsApp, que é cobrada",
  skipNote: "Sem o aviso pago, a ação é registrada e o paciente não recebe mensagem.",
  send: "Enviar mesmo assim",
  skip: "Não avisar o paciente",
  back: "Voltar",
  pending: "Salvando…",
  remember: "Não perguntar novamente (autorizar sempre os avisos pagos)",
  free: "Escrever pelo meu WhatsApp, sem custo",
} as const;

export type PaidNoticeSheetViewProps = {
  open: boolean;
  appt: Appt | null;
  status: "confirmed" | "attended" | null;
  ask: PaidNoticeAsk | null;
  pending: boolean;
  error: PaidNoticeModalError | null;
  remember: boolean;
  onRemember: (next: boolean) => void;
  onClose: () => void;
  onChoose: (choice: { send: boolean; remember: boolean }) => void;
};

export function PaidNoticeSheetView(p: PaidNoticeSheetViewProps) {
  const rememberId = useId();
  const title = p.status === "attended" ? PAID_NOTICE_COPY.titleAttended : PAID_NOTICE_COPY.titleConfirmed;
  const footer = (
    <div className="agm-paid-actions">
      <Button variant="ghost" onClick={p.onClose} disabled={p.pending}>
        {PAID_NOTICE_COPY.back}
      </Button>
      <Button variant="outline" disabled={p.pending} onClick={p.pending ? undefined : () => p.onChoose({ send: false, remember: false })}>
        {PAID_NOTICE_COPY.skip}
      </Button>
      <Button
        variant="primary"
        icon="send"
        disabled={p.pending}
        aria-busy={p.pending || undefined}
        onClick={p.pending ? undefined : () => p.onChoose({ send: true, remember: p.remember })}
      >
        {p.pending ? PAID_NOTICE_COPY.pending : PAID_NOTICE_COPY.send}
      </Button>
    </div>
  );

  return (
    <Sheet open={p.open} onClose={p.onClose} title={title} footer={footer} className="agm-card" dismissible={!p.pending}>
      {p.appt && p.ask && (
        <div className="agm-notice agm-notice--warn">
          <p className="agm-notice-head">
            {p.appt.patient || "O paciente"} {PAID_NOTICE_COPY.body}
            {p.ask.costLabel ? ` (${p.ask.costLabel})` : ""}.
          </p>
          <p>{PAID_NOTICE_COPY.skipNote}</p>
          <div className="agm-optin">
            <input
              id={rememberId}
              type="checkbox"
              checked={p.remember}
              disabled={p.pending}
              onChange={(e) => p.onRemember(e.target.checked)}
            />
            <label htmlFor={rememberId}>{PAID_NOTICE_COPY.remember}</label>
          </div>
          {p.ask.waLink && (
            <a className="agm-link" href={p.ask.waLink} target="_blank" rel="noopener noreferrer">
              {PAID_NOTICE_COPY.free}
            </a>
          )}
        </div>
      )}
      {p.error && (
        <p className="agm-note agm-note--danger" role="alert">
          <Icon name="alert" size={16} />
          <span>{p.error.message}</span>
        </p>
      )}
    </Sheet>
  );
}

export type PaidNoticeSheetProps = Omit<PaidNoticeSheetViewProps, "remember" | "onRemember">;

export function PaidNoticeSheet(props: PaidNoticeSheetProps) {
  const [remember, setRemember] = useState(false);
  const apptId = props.appt?.id;
  // A standing authorisation is always a deliberate act: never pre-ticked.
  useEffect(() => {
    if (props.open) setRemember(false);
  }, [props.open, apptId]);
  return <PaidNoticeSheetView {...props} remember={remember} onRemember={setRemember} />;
}
```

- [ ] **Step 7: `components/agenda/ReleaseCard.tsx`**

```tsx
"use client";

// ReleaseCard — "Liberar horário" (TASK-032 R5; offered before the start of any
// live appointment, spec 2026-10-09 §1). Frees the slot: the secretarIA deletes the
// Google event, cancels the appointment and tells the patient (with the option to
// book again).
//
// THE GATE: when the appointment's Pix deposit is PAID, releasing inside the
// refund window can make the clinic RETAIN the money. The card then shows the
// warning and a labelled checkbox, and the confirm button stays disabled until
// it is ticked — and the controller sends nothing without it either
// (lib/agenda/release.ts::buildReleaseBody). `forceAck` makes the same step
// mandatory when the backend asked for it although the read showed no deposit.
//
// The patient notice reuses the cancel card's 24 h block (CancelNoticeBlock): a
// WhatsApp patient outside the window is told only with "Enviar mesmo assim" or
// the clinic flag; "Não perguntar novamente" stores the flag. "The patient just
// confirmed" needs a second, deliberate click ("Liberar mesmo assim").
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
  autoApproved: boolean;
  forceAck: boolean;
  confirmedSeen: boolean;
  pending: boolean;
  error: ReleaseModalError | null;
  ack: boolean;
  onAck: (next: boolean) => void;
  optIn: boolean;
  onOptIn: (next: boolean) => void;
  remember: boolean;
  onRemember: (next: boolean) => void;
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
    paidNotice: notice.kind === "whatsapp_outside_window" && (p.optIn || p.autoApproved),
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
        onClick={!blocked ? () => p.onConfirm({ acknowledged: p.ack, optIn: p.optIn, remember: p.remember && p.optIn }) : undefined}
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
            <CancelNoticeBlock
              state={notice}
              optIn={p.optIn}
              onOptIn={p.onOptIn}
              disabled={p.pending}
              autoApproved={p.autoApproved}
              remember={p.remember}
              onRemember={p.onRemember}
            />
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

export type ReleaseCardProps = Omit<ReleaseCardViewProps, "ack" | "onAck" | "optIn" | "onOptIn" | "remember" | "onRemember">;

export function ReleaseCard(props: ReleaseCardProps) {
  const [ack, setAck] = useState(false);
  const [optIn, setOptIn] = useState(false);
  const [remember, setRemember] = useState(false);
  const apptId = props.appt?.id;
  // A fresh card never starts acknowledged, and a billed notice is always a deliberate act.
  useEffect(() => {
    if (!props.open) return;
    setAck(false);
    setOptIn(false);
    setRemember(false);
  }, [props.open, apptId]);
  return (
    <ReleaseCardView
      {...props}
      ack={ack}
      onAck={setAck}
      optIn={optIn}
      onOptIn={setOptIn}
      remember={remember}
      onRemember={setRemember}
    />
  );
}
```

- [ ] **Step 8: `components/agenda/MessageModal.tsx`**

```tsx
"use client";

// MessageModal — "Enviar mensagem" (TASK-032 R5, spec 4.4): free text on the
// patient's own channel. The text is pre-filled with an opt-in suggestion built
// from the slot's own date/time (lib/agenda/release.ts::messageSuggestion) and is
// reset on every open. Send is disabled for a blank or over-long text; a failed
// send keeps the sheet open with the text intact so the clinic can retry.
//
// WhatsApp outside the 24 h window: the backend refuses a free message unless the
// clinic authorises the paid template (or its flag is on, R7 — then it just sends).
// The sheet then explains it, shows the cost, offers "Enviar mesmo assim",
// "Não perguntar novamente" and the free wa.me link, and waits for the opt-in.

import { useEffect, useId, useState } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { TextArea } from "../primitives/Input";
import { Icon } from "../icons";
import type { Appt } from "../../lib/agenda/types";
import { CANCEL_COPY } from "../../lib/agenda/cancel-preview";
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
  remember: boolean;
  onRemember: (next: boolean) => void;
  onClose: () => void;
  onSend: (text: string, notifyOutsideWindow: boolean, remember: boolean) => void;
};

export function MessageModalView(p: MessageModalViewProps) {
  const hintId = useId();
  const optInId = useId();
  const rememberId = useId();
  const check = validateMessage(p.text);
  const outside = p.error?.kind === "outside_window" ? p.error : null;
  const blocked = p.pending || !check.ok || (outside !== null && !p.optIn);
  const paid = outside !== null && p.optIn;

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
        onClick={!blocked ? () => p.onSend(p.text, paid, paid && p.remember) : undefined}
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
              <div className="agm-optin">
                <input
                  id={rememberId}
                  type="checkbox"
                  checked={p.remember && p.optIn}
                  disabled={p.pending || !p.optIn}
                  onChange={(e) => p.onRemember(e.target.checked)}
                />
                <label htmlFor={rememberId}>{CANCEL_COPY.rememberLabel}</label>
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

export type MessageModalProps = Omit<MessageModalViewProps, "text" | "onText" | "optIn" | "onOptIn" | "remember" | "onRemember">;

export function MessageModal(props: MessageModalProps) {
  const [text, setText] = useState("");
  const [optIn, setOptIn] = useState(false);
  const [remember, setRemember] = useState(false);
  const apptId = props.appt?.id;
  // Re-seed on every open (not on every appt field change): the text and the billed choices never carry over.
  useEffect(() => {
    if (!props.open || !props.appt) return;
    setText(messageSuggestion(props.appt));
    setOptIn(false);
    setRemember(false);
  }, [props.open, apptId]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <MessageModalView
      {...props}
      text={text}
      onText={setText}
      optIn={optIn}
      onOptIn={setOptIn}
      remember={remember}
      onRemember={setRemember}
    />
  );
}
```

(There is no ESLint here, so the disable comment only documents the intent.)

- [ ] **Step 9: Wire `AgendaView.tsx`**

Imports: `import { PaidNoticeSheet } from "./PaidNoticeSheet";`, `import { ReleaseCard } from "./ReleaseCard";`, `import { MessageModal } from "./MessageModal";`, `import type { ReleaseConfirm } from "@/lib/agenda/release";`, `import { safeWaLink } from "@/lib/agenda/notice";`.

In `AgendaViewProps` change `onConfirmCancel: (body: CancelBody) => void;` to `onConfirmCancel: (body: CancelBody, remember: boolean) => void;` and add (all optional so older callers and tests compile):

```ts
  onOpenRelease?: (appt: Appt) => void;
  onConfirmRelease?: (c: ReleaseConfirm) => void;
  onOpenMessage?: (appt: Appt) => void;
  onSendMessage?: (text: string, notifyOutsideWindow: boolean, remember: boolean) => void;
  onChoosePaidNotice?: (choice: { send: boolean; remember: boolean }) => void;
```

Next to `const resched = …` add:

```ts
  const release = modal?.type === "release" ? modal : null;
  const message = modal?.type === "message" ? modal : null;
  const paidNotice = modal?.type === "paid_notice" ? modal : null;
  // Toast links are untrusted until proved: only https://wa.me/<digits> becomes an href.
  const toastLink = state.toast?.link && safeWaLink(state.toast.link.href) ? state.toast.link : null;
```

On the `<Drawer …>` add `onRelease={writable ? p.onOpenRelease : undefined}` and `onMessage={writable ? p.onOpenMessage : undefined}`. On `<CancelCard …>` add `autoApproved={state.paidAuto}` (its `onConfirm={p.onConfirmCancel}` keeps working: same two parameters). Inside the toast, after `<span className="agenda-toast-text">{state.toast.message}</span>` add:

```tsx
          {toastLink && (
            <a className="agenda-toast-link" href={toastLink.href} target="_blank" rel="noopener noreferrer">
              {toastLink.label}
            </a>
          )}
```

After `<RescheduleModal … />` add:

```tsx
      <PaidNoticeSheet
        open={paidNotice !== null}
        appt={paidNotice?.appt ?? null}
        status={paidNotice?.status ?? null}
        ask={paidNotice?.ask ?? null}
        pending={paidNotice?.pending ?? false}
        error={paidNotice?.error ?? null}
        onClose={p.onCloseModal}
        onChoose={(choice) => p.onChoosePaidNotice?.(choice)}
      />
      <ReleaseCard
        open={release !== null}
        appt={release?.appt ?? null}
        check={state.check}
        autoApproved={state.paidAuto}
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
        onSend={(text, notify, remember) => p.onSendMessage?.(text, notify, remember)}
      />
```

- [ ] **Step 10: Wire `AgendaScreen.tsx`**

In the `<AgendaView …>` props replace `onConfirmCancel={(body) => void controller.confirmCancel(body)}` with `onConfirmCancel={(body, remember) => void controller.confirmCancel(body, remember)}` and add:

```tsx
      onOpenRelease={(appt) => void controller.openRelease(appt)}
      onConfirmRelease={(c) => void controller.confirmRelease(c)}
      onOpenMessage={controller.openMessage}
      onSendMessage={(text, notify, remember) => void controller.sendMessage(text, notify, remember)}
      onChoosePaidNotice={(choice) => void controller.choosePaidNotice(choice)}
```

- [ ] **Step 11: Run tests, type check, build**

Run: `npx vitest run components/agenda lib/agenda` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors.
Run: `npm run build` — Expected: success (static export; no new route).

- [ ] **Step 12: Commit**

```bash
git add lib/agenda/cancel-preview.ts lib/agenda/modal-forms.ts components/agenda/CancelCard.tsx components/agenda/PaidNoticeSheet.tsx components/agenda/ReleaseCard.tsx components/agenda/MessageModal.tsx components/agenda/agenda-modals.css components/agenda/agenda-screen.css components/agenda/AgendaView.tsx components/agenda/AgendaScreen.tsx components/agenda/__tests__/paid-notice-sheet.test.tsx components/agenda/__tests__/release-card.test.tsx components/agenda/__tests__/message-modal.test.tsx components/agenda/__tests__/cancel-card.test.tsx components/agenda/__tests__/agenda-view.test.tsx lib/agenda/__tests__/modal-forms.test.ts
git commit -m "feat(agenda): paid-notice question, release card, patient message sheet and notice toast link (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Who is looking — viewer, "Todos / Só os meus", a restricted doctor's create, hidden appointments

**Files:**
- Create: `lib/agenda/viewer.ts`
- Modify: `lib/agenda/secretaria-hub-agenda.ts` (`AgendaViewerWire`, `getAgendaViewer`, `mine` on `listCalendarEvents`, `professional_id` on the create/block payloads, two codes), `lib/agenda/screen.ts`, `components/agenda/Modals.tsx`, `components/agenda/AgendaView.tsx`, `components/agenda/AgendaScreen.tsx`
- Test: `lib/agenda/__tests__/viewer.test.ts` (new), `lib/agenda/__tests__/screen.test.ts` (modify/append), `lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts` (append), `components/agenda/__tests__/agenda-view.test.tsx` (append)

**Interfaces:**
- Consumes: R7 `GET /calendar/viewer`, `GET /events?mine=`, `professional_id` on `POST /appointments` / `POST /blocks`, 403 `professional_not_allowed`, 422 `no_own_agenda`, the 404 rule (all under "Consumes"); Task 4 (`hubFetch` calls, `HubApiError`); Task 5 (`patchStatus`, `confirmRelease`, `sendMessage`, `confirmCancel`, `refreshAfter`, `errorFacts`, `isSignedOut`, `noticeSwitch`, `onWriteError`).
- Produces (Tasks 8–9 rely on these exact names):
  - `lib/agenda/viewer.ts`: `type AgendaViewer = { scope: "clinic" | "own"; professionalId: string | null; professionalName: string | null; canFilterOwn: boolean }`; `CLINIC_VIEWER: AgendaViewer`; `viewerFromWire(w: AgendaViewerWire | null | undefined): AgendaViewer`; `lockedProfessional(v: AgendaViewer): { id: string; name: string } | null`; `canCreate(v: AgendaViewer): boolean`; `wantsOwnOnly(v: AgendaViewer, mine: boolean): boolean`; `VIEWER_COPY`; `APPT_NOT_FOUND`; `CONSULTA_LINK_NOT_FOUND`; `CREATE_OTHER_AGENDA`.
  - `secretaria-hub-agenda.ts`: `type AgendaViewerWire = { agenda_scope: string; professional_id: string | null; professional_name: string | null; can_filter_own: boolean }`; `getAgendaViewer(session): Promise<AgendaViewerWire>`; `listCalendarEvents(session, startIso, endIso, mine = false)`; `AppointmentCreatePayload.professional_id?` / `BlockCreatePayload.professional_id?: string | null`; `PROFESSIONAL_NOT_ALLOWED_CODE = "professional_not_allowed"`, `NO_OWN_AGENDA_CODE = "no_own_agenda"`.
  - `screen.ts`: `AgendaParams.mine: boolean` (`?meus=1`); `AgendaState.viewer: AgendaViewer`, `AgendaState.mine: boolean`; actions `{ type: "set_mine"; mine: boolean }` (unscoped), scoped `{ type: "viewer_loaded"; viewer: AgendaViewer }`; `init` gains optional `mine?: boolean`; `AgendaHub.getAgendaViewer`, `AgendaHub.listCalendarEvents(s, startIso, endIso, mine?: boolean)`; controller `setMine(mine: boolean): Promise<void>`; `isApptNotFound(e: unknown): boolean` (exported).
  - `NewApptModalProps.lockedProfessional?` / `BlockModalProps.lockedProfessional?: { id: string; name: string } | null`; `AgendaViewProps.onSetMine?: (mine: boolean) => void`.

- [ ] **Step 1: Write the failing tests**

Create `lib/agenda/__tests__/viewer.test.ts`:

```ts
// Who is looking at the agenda (R7 GET /calendar/viewer, spec 2026-10-09 §5.A). Pure.
import { describe, expect, it } from "vitest";

import { CLINIC_VIEWER, canCreate, lockedProfessional, viewerFromWire, wantsOwnOnly } from "../viewer";
import type { AgendaViewerWire } from "../secretaria-hub-agenda";

const wire = (over: Partial<AgendaViewerWire> = {}): AgendaViewerWire => ({
  agenda_scope: "clinic", professional_id: null, professional_name: null, can_filter_own: false, ...over,
});

describe("viewerFromWire", () => {
  it("a secretary: clinic-wide, no switch", () => {
    expect(viewerFromWire(wire())).toEqual({ scope: "clinic", professionalId: null, professionalName: null, canFilterOwn: false });
  });

  it("a manager who is also a doctor: clinic-wide with the switch", () => {
    expect(viewerFromWire(wire({ professional_id: "p1", professional_name: "Dra. Ana", can_filter_own: true }))).toEqual({
      scope: "clinic", professionalId: "p1", professionalName: "Dra. Ana", canFilterOwn: true,
    });
  });

  it("a restricted doctor: own, and never a switch even if the flag said so", () => {
    expect(viewerFromWire(wire({ agenda_scope: "own", professional_id: "p2", professional_name: "Dr. Beto", can_filter_own: true }))).toEqual({
      scope: "own", professionalId: "p2", professionalName: "Dr. Beto", canFilterOwn: false,
    });
  });

  it("an unknown scope reads as own (never show more); no wire is the clinic-wide fallback", () => {
    expect(viewerFromWire(wire({ agenda_scope: "weird" })).scope).toBe("own");
    expect(viewerFromWire(null)).toEqual(CLINIC_VIEWER);
    expect(viewerFromWire(undefined)).toEqual(CLINIC_VIEWER);
  });

  it("a switch flag without a professional, or a blank id, is ignored", () => {
    expect(viewerFromWire(wire({ can_filter_own: true })).canFilterOwn).toBe(false);
    expect(viewerFromWire(wire({ professional_id: "  ", can_filter_own: true }))).toMatchObject({ professionalId: null, canFilterOwn: false });
  });
});

describe("helpers", () => {
  const own = viewerFromWire(wire({ agenda_scope: "own", professional_id: "p2", professional_name: "Dr. Beto" }));
  const manager = viewerFromWire(wire({ professional_id: "p1", professional_name: "Dra. Ana", can_filter_own: true }));

  it("locks a new appointment/block to the restricted doctor only", () => {
    expect(lockedProfessional(own)).toEqual({ id: "p2", name: "Dr. Beto" });
    expect(lockedProfessional(viewerFromWire(wire({ agenda_scope: "own", professional_id: "p2" })))).toEqual({ id: "p2", name: "Você" });
    expect(lockedProfessional(manager)).toBeNull();
    expect(lockedProfessional(CLINIC_VIEWER)).toBeNull();
  });

  it("a restricted viewer with no doctor of his own cannot create; everyone else can", () => {
    expect(canCreate(own)).toBe(true);
    expect(canCreate(viewerFromWire(wire({ agenda_scope: "own" })))).toBe(false);
    expect(canCreate(manager)).toBe(true);
    expect(canCreate(CLINIC_VIEWER)).toBe(true);
  });

  it("only a viewer with the switch narrows the read", () => {
    expect(wantsOwnOnly(manager, true)).toBe(true);
    expect(wantsOwnOnly(manager, false)).toBe(false);
    expect(wantsOwnOnly(CLINIC_VIEWER, true)).toBe(false); // a receptionist's ?meus=1 never reaches the server (422)
    expect(wantsOwnOnly(own, true)).toBe(false); // the server already narrows him
  });
});
```

In `lib/agenda/__tests__/screen.test.ts`:

(a) The three `parseAgendaParams(...)).toEqual({ … })` pins of `it("parses ?data=, ?view= and ?consultaId=, ignoring junk", …)` gain `mine: false` (e.g. `{ anchorKey: "2026-10-02", view: "mes", consultaId: "abc", mine: false }`), and add to that test:

```ts
    expect(parseAgendaParams("?meus=1").mine).toBe(true);
    expect(parseAgendaParams("?meus=true").mine).toBe(false);
```

(b) The `setup` helper's `initial` option type becomes `initial?: Partial<AgendaParams>` (add `type AgendaParams` to the import from `"../screen"`).

(c) Right above `function fakeHub`, add:

```ts
// R7 GET /calendar/viewer answers for the four kinds of people (spec 2026-10-09 §5.A).
const VIEWERS = {
  secretary: { agenda_scope: "clinic", professional_id: null, professional_name: null, can_filter_own: false },
  manager: { agenda_scope: "clinic", professional_id: "pro-1", professional_name: "Dra. Ana", can_filter_own: true },
  doctor: { agenda_scope: "own", professional_id: "pro-2", professional_name: "Dr. Beto", can_filter_own: false },
  orphan: { agenda_scope: "own", professional_id: null, professional_name: null, can_filter_own: false },
} as const satisfies Record<string, AgendaViewerWire>;
```

(add `AgendaViewerWire` to the type import from `"../secretaria-hub-agenda"`), and in `fakeHub` add the default `getAgendaViewer: vi.fn(async () => ({ ...VIEWERS.secretary })),`.

(d) Add to the imports: `import { APPT_NOT_FOUND, CONSULTA_LINK_NOT_FOUND, VIEWER_COPY } from "../viewer";` and `NO_OWN_AGENDA_CODE, PROFESSIONAL_NOT_ALLOWED_CODE` to the value import from `"../secretaria-hub-agenda"`.

(e) Append at the end of the file:

```ts
// ---------------------------------------------------------------------------
// TASK-032 R5 — who is looking (R7 GET /viewer), "Só os meus", a restricted doctor's create, 404s
// ---------------------------------------------------------------------------

type ViewerWire = (typeof VIEWERS)[keyof typeof VIEWERS];
const asViewer = (viewer: ViewerWire, hubOver: Partial<AgendaHub> = {}, initial?: Partial<AgendaParams>) =>
  setup({ hub: fakeHub({ getAgendaViewer: vi.fn(async () => ({ ...viewer })), ...hubOver }), initial });
const listCalls = (s: { hub: AgendaHub }) => (s.hub.listCalendarEvents as ReturnType<typeof vi.fn>).mock.calls;
const NEW_APPT: Omit<Appt, "id"> = {
  patient: "Maria Souza", phone: "+55 11 99999-9999", date: "2026-09-30", day: 3, start: 600, dur: 30,
  type: "Consulta", notes: "", status: "agendado", anamnese: "pendente",
};
const BLOCK = { date: "2026-09-30", start: 720, dur: 60, reason: "Almoço" };

describe("who is looking (R7 GET /viewer, spec 2026-10-09 §5.A)", () => {
  it("reads the viewer BEFORE the first range; a secretary reads everything (mine false)", async () => {
    const s = asViewer(VIEWERS.secretary);
    await s.controller.start();
    const viewerAt = (s.hub.getAgendaViewer as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(viewerAt).toBeLessThan((s.hub.listCalendarEvents as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]);
    expect(s.state().viewer).toMatchObject({ scope: "clinic", canFilterOwn: false });
    expect(listCalls(s)[0][3]).toBe(false);
  });

  it("a secretary's ?meus=1 never reaches the server (it would be 422 no_own_agenda) and setMine is a no-op", async () => {
    const s = asViewer(VIEWERS.secretary, {}, parseAgendaParams("?meus=1"));
    await s.controller.start();
    expect(listCalls(s)[0][3]).toBe(false);
    await s.controller.setMine(true);
    expect(listCalls(s)).toHaveLength(1);
    expect(s.urls.join(" ")).not.toContain("meus=");
  });

  it("a manager-doctor: 'Só os meus' narrows the read and is kept in ?meus=1; 'Todos' widens it and drops it", async () => {
    const s = asViewer(VIEWERS.manager);
    await s.controller.start();
    await s.controller.setMine(true);
    expect(listCalls(s)[1][3]).toBe(true);
    expect(s.urls.at(-1)).toContain("meus=1");
    await s.controller.setMine(false);
    expect(listCalls(s)[2][3]).toBe(false);
    expect(s.urls.at(-1)).not.toContain("meus=");
  });

  it("a manager's reload with ?meus=1 is narrowed on the FIRST read", async () => {
    const s = asViewer(VIEWERS.manager, {}, parseAgendaParams("?data=2026-09-29&meus=1"));
    await s.controller.start();
    expect(listCalls(s)).toHaveLength(1);
    expect(listCalls(s)[0][3]).toBe(true);
  });

  it("a restricted doctor never gets the switch, whatever the URL says; the server narrows his read", async () => {
    const s = asViewer(VIEWERS.doctor, {}, parseAgendaParams("?meus=1"));
    await s.controller.start();
    expect(s.state().viewer).toMatchObject({ scope: "own", professionalId: "pro-2", canFilterOwn: false });
    expect(listCalls(s)[0][3]).toBe(false);
    await s.controller.setMine(true);
    expect(listCalls(s)).toHaveLength(1);
  });

  it("a failed viewer read (older backend, outage) keeps the clinic-wide look and still loads the agenda", async () => {
    const s = asViewer(VIEWERS.manager, { getAgendaViewer: vi.fn(async () => Promise.reject(new HubApiError(404, "Not Found"))) });
    await s.controller.start();
    expect(s.state().viewer.canFilterOwn).toBe(false);
    expect(s.state().load).toEqual({ phase: "loaded" });
  });

  it("after a clinic switch the viewer is read again for the new clinic (the old one never applies)", async () => {
    let current = session(TENANT_A);
    const viewer = vi.fn().mockResolvedValueOnce({ ...VIEWERS.manager }).mockResolvedValue({ ...VIEWERS.doctor });
    const s = setup({ hub: fakeHub({ getAgendaViewer: viewer }), resolveSession: async () => current });
    await s.controller.start();
    expect(s.state().viewer.canFilterOwn).toBe(true);
    current = session(TENANT_B);
    await s.controller.retry(); // notices the switch: tenant_changed, then viewer + range under B
    await flush();
    expect(s.state().tenantId).toBe(TENANT_B);
    expect(s.state().viewer).toMatchObject({ scope: "own", professionalId: "pro-2" });
    expect((viewer.mock.calls.at(-1)?.[0] as Session).tenantId).toBe(TENANT_B);
  });
});

describe("a restricted doctor books and blocks only on his own agenda (R7, owner 2026-10-09)", () => {
  const createCalls = (s: { hub: AgendaHub }) => (s.hub.createAppointment as ReturnType<typeof vi.fn>).mock.calls;
  const blockCalls = (s: { hub: AgendaHub }) => (s.hub.createBlock as ReturnType<typeof vi.fn>).mock.calls;

  it("Nova consulta and Bloquear send HIS professional_id", async () => {
    const s = asViewer(VIEWERS.doctor);
    await s.controller.start();
    s.controller.openNew();
    await s.controller.createAppt(NEW_APPT);
    expect(createCalls(s)[0][1].professional_id).toBe("pro-2");
    s.controller.openBlock();
    await s.controller.createBlock(BLOCK);
    expect(blockCalls(s)[0][1].professional_id).toBe("pro-2");
  });

  it("a clinic-wide viewer's create sends no doctor (today's behaviour)", async () => {
    const s = asViewer(VIEWERS.manager);
    await s.controller.start();
    s.controller.openNew();
    await s.controller.createAppt(NEW_APPT);
    expect("professional_id" in createCalls(s)[0][1]).toBe(false);
  });

  it("a restricted viewer with no doctor of his own cannot even open the sheets", async () => {
    const s = asViewer(VIEWERS.orphan);
    await s.controller.start();
    s.controller.openNew();
    expect(s.state().modal).toBeNull();
    s.controller.openBlock();
    expect(s.state().modal).toBeNull();
  });

  it("the server's refusals are shown in the sheet with its own sentence (or ours when it sent none)", async () => {
    const s = asViewer(VIEWERS.doctor, {
      createAppointment: vi.fn(async () =>
        Promise.reject(new HubApiError(403, "Você só pode marcar consultas e bloqueios na sua própria agenda.", PROFESSIONAL_NOT_ALLOWED_CODE)),
      ),
      createBlock: vi.fn(async () => Promise.reject(new HubApiError(422, "", NO_OWN_AGENDA_CODE))),
    });
    await s.controller.start();
    s.controller.openNew();
    await s.controller.createAppt(NEW_APPT);
    expect(s.state().modal).toMatchObject({ type: "new", pending: false, error: "Você só pode marcar consultas e bloqueios na sua própria agenda." });
    s.controller.closeModal();
    s.controller.openBlock();
    await s.controller.createBlock(BLOCK);
    expect(s.state().modal).toMatchObject({ type: "block", pending: false, error: VIEWER_COPY.noOwnAgenda });
    expect(s.state().toast).toBeNull();
  });
});

describe("appointments the viewer cannot see (R7 404)", () => {
  it("a deep link to an appointment that is not in his agenda says so and opens nothing", async () => {
    const s = setup({ initial: parseAgendaParams("?consulta=appt-other&data=2026-09-29") });
    await s.controller.start();
    expect(s.state().selectedId).toBeNull();
    expect(s.state().toast).toEqual({ tone: "danger", message: CONSULTA_LINK_NOT_FOUND });
  });

  it("a deep link that IS in the range opens it with no toast", async () => {
    const s = setup({ initial: parseAgendaParams("?consulta=appt-e1&data=2026-09-29") });
    await s.controller.start();
    expect(s.state().selectedId).toBe("hub-e1");
    expect(s.state().toast).toBeNull();
  });

  it("a status action answered 404 explains it, claims nothing and reloads", async () => {
    const s = setup({ hub: fakeHub({ updateAppointmentStatus: vi.fn(async () => Promise.reject(new HubApiError(404, "Appointment not found"))) }) });
    await s.controller.start();
    await s.controller.setStatus(appt(s.state()), "no_show");
    expect(s.state().statusError).toBe(APPT_NOT_FOUND);
    expect(s.state().toast).toBeNull();
    expect(s.state().knownStatus).toEqual({});
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("release, message and cancel answered 404 keep their sheet with the same sentence", async () => {
    const notFound = vi.fn(async () => Promise.reject(new HubApiError(404, "Appointment not found")));
    const r = setup({ hub: fakeHub({ releaseAppointment: notFound }) });
    await r.controller.start();
    await r.controller.openRelease(appt(r.state()));
    await r.controller.confirmRelease({ acknowledged: false, optIn: false, remember: false });
    expect(r.state().modal).toMatchObject({ type: "release", pending: false, error: { kind: "failed", message: APPT_NOT_FOUND } });

    const m = setup({ hub: fakeHub({ messageAppointment: notFound }) });
    await m.controller.start();
    m.controller.openMessage(appt(m.state()));
    await m.controller.sendMessage("Oi", false);
    expect(m.state().modal).toMatchObject({ type: "message", pending: false, error: { kind: "failed", message: APPT_NOT_FOUND } });

    const c = setup({ hub: fakeHub({ cancelAppointment: notFound }) });
    await c.controller.start();
    await c.controller.openCancel(appt(c.state()));
    await c.controller.confirmCancel({ confirm: true, justification: null, notify_outside_window: false });
    expect(c.state().modal).toMatchObject({ type: "cancel", pending: false, error: { kind: "failed", message: APPT_NOT_FOUND } });
  });
});
```

Append to `lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts` (add `getAgendaViewer`, `listCalendarEvents` to its import from `"../secretaria-hub-agenda"`):

```ts
describe("who is looking (R7)", () => {
  it("GET /calendar/viewer", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { agenda_scope: "own", professional_id: "p2", professional_name: "Dr. Beto", can_filter_own: false }));
    expect((await getAgendaViewer(session)).agenda_scope).toBe("own");
    expect(lastCall()[0]).toBe(`${SECRETARIA_HUB_BASE}/tenants/me/calendar/viewer`);
  });

  it("events: mine=true only when asked; the URL is unchanged otherwise", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, []));
    await listCalendarEvents(session, "2026-09-27T03:00:00.000Z", "2026-10-04T03:00:00.000Z", true);
    expect(lastCall()[0]).toBe(
      `${SECRETARIA_HUB_BASE}/tenants/me/calendar/events?start=2026-09-27T03%3A00%3A00.000Z&end=2026-10-04T03%3A00%3A00.000Z&mine=true`,
    );
    await listCalendarEvents(session, "2026-09-27T03:00:00.000Z", "2026-10-04T03:00:00.000Z");
    expect(lastCall()[0]).not.toContain("mine=");
  });
});
```

Append to `components/agenda/__tests__/agenda-view.test.tsx` (add `import { VIEWER_COPY, type AgendaViewer } from "@/lib/agenda/viewer";`):

```tsx
describe("AgendaView — who is looking (R7)", () => {
  const manager: AgendaViewer = { scope: "clinic", professionalId: "pro-1", professionalName: "Dra. Ana", canFilterOwn: true };
  const doctor: AgendaViewer = { scope: "own", professionalId: "pro-2", professionalName: "Dr. Beto", canFilterOwn: false };
  const as = (viewer: AgendaViewer, mine = false): AgendaState => ({ ...loaded([ITEM]), viewer, mine });

  it("'Todos / Só os meus' exists only for a viewer who can filter, as a named group that says which is pressed", () => {
    const html = render(as(manager, true), { onSetMine: noop });
    expect(html).toContain(`role="group" aria-label="${VIEWER_COPY.filterLabel}"`);
    expect(buttonTag(html, VIEWER_COPY.mine)).toContain('aria-pressed="true"');
    expect(buttonTag(html, VIEWER_COPY.all)).toContain('aria-pressed="false"');
    expect(render(as(doctor), { onSetMine: noop })).not.toContain(VIEWER_COPY.mine);
    expect(render(loaded([ITEM]), { onSetMine: noop })).not.toContain(VIEWER_COPY.mine); // secretary / older backend
  });

  it("a restricted doctor keeps Nova consulta and Bloquear, and the sheet shows the doctor locked to him", () => {
    const html = render({ ...as(doctor), modal: { type: "new", pending: false, error: null } });
    expect(buttonTag(html, "Nova consulta")).not.toContain("disabled");
    expect(html).toContain("Dr. Beto");
    expect(html).toContain(VIEWER_COPY.lockedHint);
  });

  it("a restricted viewer with no doctor of his own cannot create, and the buttons say why", () => {
    const html = render(as({ ...doctor, professionalId: null, professionalName: null }));
    expect(buttonTag(html, "Nova consulta")).toContain("disabled");
    expect(buttonTag(html, "Bloquear")).toContain("disabled");
    expect(html).toContain(VIEWER_COPY.noOwnAgenda);
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/viewer.test.ts lib/agenda/__tests__/screen.test.ts lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts components/agenda/__tests__/agenda-view.test.tsx`
Expected: FAIL (`../viewer` missing; `getAgendaViewer`, `setMine`, `viewer`, `mine` missing).

- [ ] **Step 3: `lib/agenda/viewer.ts`**

```ts
// lib/agenda/viewer.ts — WHO is looking at the agenda (TASK-032 R5; R7 GET /calendar/viewer;
// spec 2026-10-09 §5.A; owner's decision 2026-10-09 on creating). The server is the
// authority: it filters GET /events, answers 404 for an appointment the viewer may not
// see and decides the doctor of a new row. This module only mirrors it, so a doctor
// restricted to his own agenda is never OFFERED what the server would refuse: no
// "Todos / Só os meus" switch, and "Nova consulta"/"Bloquear" locked to himself. Pure.

import type { AgendaViewerWire } from "./secretaria-hub-agenda";

export type AgendaViewer = {
  /** "clinic" = every doctor; "own" = only `professionalId`'s appointments. */
  scope: "clinic" | "own";
  professionalId: string | null;
  professionalName: string | null;
  /** A clinic-wide viewer who is also a doctor: the "Todos / Só os meus" switch. */
  canFilterOwn: boolean;
};

/**
 * Before the read answers, when it fails, and on a backend without the route:
 * today's look — no switch, a create sends no doctor. Safe because the server
 * still applies the real rule (an "own" doctor's create defaults to himself, his
 * read is filtered, another doctor's id is 404).
 */
export const CLINIC_VIEWER: AgendaViewer = { scope: "clinic", professionalId: null, professionalName: null, canFilterOwn: false };

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() !== "" ? v.trim() : null);

/** The wire, read defensively: an unknown scope reads as "own" (never show more than the server allows). */
export function viewerFromWire(w: AgendaViewerWire | null | undefined): AgendaViewer {
  if (!w || typeof w !== "object") return CLINIC_VIEWER;
  const scope = w.agenda_scope === "clinic" ? "clinic" : "own";
  const professionalId = text(w.professional_id);
  return {
    scope,
    professionalId,
    professionalName: text(w.professional_name),
    // Only the server's yes, and only where it makes sense (a clinic-wide viewer with a doctor of his own).
    canFilterOwn: scope === "clinic" && professionalId !== null && w.can_filter_own === true,
  };
}

/** The doctor a restricted viewer's new appointment/block is locked to; null = not locked. */
export function lockedProfessional(v: AgendaViewer): { id: string; name: string } | null {
  if (v.scope !== "own" || !v.professionalId) return null;
  return { id: v.professionalId, name: v.professionalName ?? "Você" };
}

/** A restricted viewer with no doctor of his own has no agenda to write on (R7 422 no_own_agenda). */
export function canCreate(v: AgendaViewer): boolean {
  return !(v.scope === "own" && !v.professionalId);
}

/** Whether GET /events is narrowed: only for a viewer the switch exists for (a receptionist's would be 422). */
export function wantsOwnOnly(v: AgendaViewer, mine: boolean): boolean {
  return mine && v.canFilterOwn;
}

export const VIEWER_COPY = {
  filterLabel: "Mostrar consultas",
  all: "Todos",
  mine: "Só os meus",
  lockedLabel: "Profissional",
  lockedHint: "Você marca apenas na sua própria agenda.",
  noOwnAgenda: "Seu usuário não está ligado a um profissional da clínica: não há agenda própria para marcar.",
} as const;

/** An action on an id the server answered 404 for (gone, or not this viewer's: R7 never says which). */
export const APPT_NOT_FOUND = "Esta consulta não foi encontrada na sua agenda. A agenda foi atualizada.";
/** A `?consulta=` / `?consultaId=` link whose appointment is not in the loaded agenda. */
export const CONSULTA_LINK_NOT_FOUND = "A consulta do link não foi encontrada na sua agenda.";
/** R7 403 professional_not_allowed on create, when the server sent no sentence. */
export const CREATE_OTHER_AGENDA = "Você só pode marcar consultas e bloqueios na sua própria agenda.";
```

- [ ] **Step 4: The calls (`lib/agenda/secretaria-hub-agenda.ts`)**

(a) Header route list: add `//   GET   /tenants/me/calendar/viewer                       -> AgendaViewerRead (R7: who is looking)` and change the events line's comment to mention `&mine=`.

(b) Replace `listCalendarEvents` with:

```ts
export function listCalendarEvents(
  session: Session,
  startIso: string,
  endIso: string,
  mine = false,
): Promise<CalendarEventWire[]> {
  // `mine=true` only for a viewer whose switch exists (lib/agenda/viewer.ts::wantsOwnOnly):
  // a receptionist's would be 422 no_own_agenda. Absent = the URL is exactly what it was.
  const qs = `?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}${mine ? "&mine=true" : ""}`;
  return hubFetch<CalendarEventWire[]>(session, "/tenants/me/calendar/events" + qs);
}
```

(c) In `AppointmentCreatePayload` (after `patient_id?`) and in `BlockCreatePayload` (after `description?`) add:

```ts
  /** R7: whose agenda. Sent only for a viewer restricted to his own (lib/agenda/viewer.ts::lockedProfessional). */
  professional_id?: string | null;
```

(d) Next to the other refusal codes (Task 4) add:

```ts
export const PROFESSIONAL_NOT_ALLOWED_CODE = "professional_not_allowed"; // another doctor's agenda (403)
export const NO_OWN_AGENDA_CODE = "no_own_agenda"; // a viewer with no doctor of his own (422)

/** GET /calendar/viewer (R7). `agenda_scope` kept a string: an unknown value reads as "own". */
export type AgendaViewerWire = {
  agenda_scope: string;
  professional_id: string | null;
  professional_name: string | null;
  can_filter_own: boolean;
};

/** Who is looking — read once when the agenda opens (and after a clinic switch). */
export function getAgendaViewer(session: Session): Promise<AgendaViewerWire> {
  return hubFetch<AgendaViewerWire>(session, "/tenants/me/calendar/viewer");
}
```

- [ ] **Step 5: State, URL and controller (`lib/agenda/screen.ts`)**

(a) Imports: add

```ts
import {
  APPT_NOT_FOUND,
  CLINIC_VIEWER,
  CONSULTA_LINK_NOT_FOUND,
  CREATE_OTHER_AGENDA,
  VIEWER_COPY,
  canCreate,
  lockedProfessional,
  viewerFromWire,
  wantsOwnOnly,
  type AgendaViewer,
} from "./viewer";
```

add `NO_OWN_AGENDA_CODE, PROFESSIONAL_NOT_ALLOWED_CODE` to the value import from `./secretaria-hub-agenda` and `AgendaViewerWire` to its type import. In the header comment, extend the last bullet to: "`?data=`, `?view=`, `?consultaId=` and `?meus=1` survive a reload (static export)."

(b) URL. Replace `export type AgendaParams = …` with

```ts
export type AgendaParams = { anchorKey: string | null; view: ViewMode | null; consultaId: string | null; mine: boolean };
```

in `parseAgendaParams` add `mine: q.get("meus") === "1",` as the last key of the returned object, and in `agendaSearch`, right after `if (consultaId) q.set("consultaId", consultaId);` add:

```ts
  // "Só os meus" (R7 can_filter_own): only for the viewer the switch exists for; anyone else's ?meus=1 is dropped.
  if (wantsOwnOnly(state.viewer, state.mine)) q.set("meus", "1");
```

(c) State. In `AgendaState`, after `paidAuto: boolean;` add:

```ts
  /** Who is looking (R7 GET /viewer). CLINIC_VIEWER until read, or when the read failed. */
  viewer: AgendaViewer;
  /** "Só os meus" asked (URL ?meus=1 or the switch); honoured only when viewer.canFilterOwn. */
  mine: boolean;
```

and in `INITIAL_AGENDA_STATE` add `viewer: CLINIC_VIEWER,` and `mine: false,` after `paidAuto: false,`.

(d) Actions. The `init` member becomes `| { type: "init"; anchorKey: string; view: ViewMode; pendingSelection: string | null; mine?: boolean }`; add the unscoped `| { type: "set_mine"; mine: boolean }` after `navigate`, and to `TenantScopedAction`:

```ts
  /** R7 GET /viewer answered for this clinic. */
  | { type: "viewer_loaded"; viewer: AgendaViewer }
```

(e) Reducer. In `agendaReducer`: the `init` case returns `{ ...state, anchorKey: action.anchorKey, view: action.view, pendingSelection: action.pendingSelection, mine: action.mine ?? state.mine }`; in the `tenant_changed` case add `mine: state.mine,` after `anchorKey: state.anchorKey,` (the viewer resets to `CLINIC_VIEWER` with the rest of the state and is read again); add the case `case "set_mine": return { ...state, mine: action.mine };`. In `applyScoped` add `case "viewer_loaded": return { ...state, viewer: action.viewer };`, and in the `range_loaded` case replace the `pendingSelection` block with:

```ts
      let { selectedId, pendingSelection } = state;
      let toast = state.toast;
      if (pendingSelection) {
        const hit = action.items.find((a) => a.appointmentId === pendingSelection || a.id === pendingSelection);
        if (hit) selectedId = hit.id;
        // R7 answers the same for a gone appointment and another doctor's: never say which.
        else toast = { tone: "danger", message: CONSULTA_LINK_NOT_FOUND };
        pendingSelection = null;
      }
      return { ...state, load: { phase: "loaded" }, items: action.items, selectedId, pendingSelection, toast };
```

(f) `AgendaHub`: replace the `listCalendarEvents` member with `listCalendarEvents: (s: Session, startIso: string, endIso: string, mine?: boolean) => Promise<CalendarEventWire[]>;` and add `getAgendaViewer: (s: Session) => Promise<AgendaViewerWire>;`.

(g) Helpers, next to `isCalendarNotConnected`:

```ts
/** 404 on an id route: the appointment does not exist FOR THIS VIEWER (R7 answers the same for another doctor's). */
export function isApptNotFound(e: unknown): boolean {
  return e instanceof HubApiError && e.status === 404;
}

/** The sentence for a refused create/block: R7's viewer rules speak for themselves. */
function createErrorText(e: unknown, fallback: string): string {
  if (isCalendarNotConnected(e)) return CALENDAR_NOT_CONNECTED_WRITE;
  if (e instanceof HubApiError && e.status === 403 && e.code === PROFESSIONAL_NOT_ALLOWED_CODE) return e.message || CREATE_OTHER_AGENDA;
  if (e instanceof HubApiError && e.status === 422 && e.code === NO_OWN_AGENDA_CODE) return e.message || VIEWER_COPY.noOwnAgenda;
  return fallback;
}
```

(h) Controller.

- In `fresh()`, in the clinic-switch branch replace `void loadRange();` with `void loadViewer(s).then(() => loadRange());` (the new clinic's viewer before its range).
- In `loadRange`, replace `const events = await hub.listCalendarEvents(s, startIso, endIso);` with `const events = await hub.listCalendarEvents(s, startIso, endIso, wantsOwnOnly(st.viewer, st.mine));`.
- Add after `loadRange`:

```ts
  /**
   * R7 GET /viewer. Any failure (outage, a backend without the route) keeps today's
   * look (CLINIC_VIEWER): the server still applies the real rule to every read and write.
   */
  async function loadViewer(s: Session): Promise<void> {
    try {
      const w = await hub.getAgendaViewer(s);
      dispatch(scoped(s.tenantId, { type: "viewer_loaded", viewer: viewerFromWire(w) }));
    } catch (e) {
      console.error("agenda: failed to read who is looking", errorFacts(e));
      if (isSignedOut(e)) return; // the range read right after reports the signed-out state
      dispatch(scoped(s.tenantId, { type: "viewer_loaded", viewer: CLINIC_VIEWER }));
      noticeSwitch(e);
    }
  }

  /** "Todos" / "Só os meus" — a read filter for a clinic-wide viewer who is also a doctor, never a permission. */
  function setMine(mine: boolean): Promise<void> {
    const st = getState();
    if (!st.viewer.canFilterOwn || st.mine === mine) return Promise.resolve();
    dispatch({ type: "set_mine", mine });
    syncUrl();
    return loadRange();
  }
```

- In `start()`: the `init` dispatch gains `mine: init.mine ?? false,`, and `if (resolved) await loadRange();` becomes:

```ts
    if (resolved) {
      // The viewer first: a manager's ?meus=1 applies to the very first read, a receptionist's never does.
      await loadViewer(resolved);
      await loadRange();
    }
```

- `openNew` and `openBlock`: their guard becomes `if (!canWrite(getState()) || !canCreate(getState().viewer)) return;`.
- `createAppt`: before `try {` add `const locked = lockedProfessional(getState().viewer);`; in the `hub.createAppointment(s, { … })` body add, after `phone: data.phone || null,`: `...(locked ? { professional_id: locked.id } : {}),`; replace its catch's `await onWriteError(s, e, isCalendarNotConnected(e) ? CALENDAR_NOT_CONNECTED_WRITE : CREATE_FAILED);` with `await onWriteError(s, e, createErrorText(e, CREATE_FAILED));`.
- `createBlock`: same — `const locked = lockedProfessional(getState().viewer);` before `try {`; the call becomes `await hub.createBlock(s, { start: startIso, end: endIso, summary: formatBlockSummary(data.reason), ...(locked ? { professional_id: locked.id } : {}) });`; its catch uses `createErrorText(e, BLOCK_FAILED)`.
- `patchStatus` (Task 5): in its `catch`, right before the final `own({ type: "status_done", error: isCalendarNotConnected(e) ? … })`, add:

```ts
      if (isApptNotFound(e)) {
        own({ type: "status_done", error: APPT_NOT_FOUND });
        refreshAfter(s.tenantId);
        return;
      }
```

- `confirmRelease` (Task 5) and `sendMessage` (Task 5): in each `catch`, insert before the final `} else {` branch:

```ts
      } else if (isApptNotFound(e)) {
        own({ type: "modal_error", error: { kind: "failed", message: APPT_NOT_FOUND } });
        refreshAfter(s.tenantId);
```

- `confirmCancel`: in the inner `catch` of the cancel call, insert before its final `} else {` branch the same `else if (isApptNotFound(e)) { … }` block.
- Add `setMine,` to the returned object, after `setStatus,`.

- [ ] **Step 6: The locked doctor in the sheets (`components/agenda/Modals.tsx`)**

Import `import { VIEWER_COPY } from "../../lib/agenda/viewer";`. Add above `NewApptModal`:

```tsx
/** A doctor restricted to his own agenda books and blocks only on it (R7): shown, never editable. */
function LockedProfessional({ pro }: { pro: { id: string; name: string } | null | undefined }) {
  if (!pro) return null;
  return (
    <p className="agm-hint">
      <strong>{VIEWER_COPY.lockedLabel}:</strong> {pro.name}. {VIEWER_COPY.lockedHint}
    </p>
  );
}
```

Add `lockedProfessional?: { id: string; name: string } | null;` to `NewApptModalProps` and `BlockModalProps` (after `error?`), add `lockedProfessional` to both components' destructured parameters, and render `<LockedProfessional pro={lockedProfessional} />` right before `<p className="agm-hint">{NEW_APPT_COPY.noMessage}</p>` and right before `<p className="agm-hint">{BLOCK_COPY.notice}</p>`. The controller adds the id to the request (Step 5); the sheet only shows it.

- [ ] **Step 7: The switch and the create buttons (`components/agenda/AgendaView.tsx`)**

Imports: `import { VIEWER_COPY, canCreate, lockedProfessional } from "@/lib/agenda/viewer";`. Add to `AgendaViewProps`: `onSetMine?: (mine: boolean) => void;`. Next to `const writable = canWrite(state);` add:

```ts
  // R7 (spec §5.A): mirror who is looking; the server enforces it regardless.
  const creatable = canCreate(state.viewer);
  const locked = lockedProfessional(state.viewer);
  const createHint = !writable ? WRITE_DISABLED_HINT : !creatable ? VIEWER_COPY.noOwnAgenda : undefined;
```

Right after the `agenda-views` `<div role="group" aria-label="Visualização">…</div>` add:

```tsx
        {state.viewer.canFilterOwn && (
          // Only a clinic-wide viewer who is also a doctor (R7 can_filter_own): a secretary or a
          // restricted doctor never sees it. Same pressed-button pattern as the view switch.
          <div className="agenda-views" role="group" aria-label={VIEWER_COPY.filterLabel}>
            {([false, true] as const).map((mine) => (
              <Button
                key={String(mine)}
                size="sm"
                variant={state.mine === mine ? "primary" : "ghost"}
                aria-pressed={state.mine === mine}
                onClick={() => p.onSetMine?.(mine)}
                disabled={!anchor}
              >
                {mine ? VIEWER_COPY.mine : VIEWER_COPY.all}
              </Button>
            ))}
          </div>
        )}
```

On the "Bloquear" and "Nova consulta" buttons replace `disabled={!writable}` with `disabled={!writable || !creatable}` and `title={writable ? undefined : WRITE_DISABLED_HINT}` with `title={createHint}`; when `createHint` is `VIEWER_COPY.noOwnAgenda` also render it once, visibly, right after the toolbar: `{writable && !creatable && <p className="agm-hint">{VIEWER_COPY.noOwnAgenda}</p>}` (inside `<div className="agenda-notices">`, after `<Notice … />`). On `<NewApptModal …>` and `<BlockModal …>` add `lockedProfessional={locked}`.

- [ ] **Step 8: Wire `AgendaScreen.tsx`**

Import `getAgendaViewer` from `@/lib/agenda/secretaria-hub-agenda` and add it to `HUB`; on `<AgendaView …>` add `onSetMine={(mine) => void controller.setMine(mine)}`. `app/agenda/page.tsx` needs nothing: it already passes `parseAgendaParams(...)`, which now carries `mine`.

- [ ] **Step 9: Run tests, type check, build**

Run: `npx vitest run lib/agenda components/agenda` — Expected: PASS (older suites: the default fake viewer is the secretary, so `mine` is `false` and nothing else moves; the three `parseAgendaParams` pins carry `mine: false`).
Run: `npm run typecheck` — Expected: no errors (any other `AgendaHub` literal in a test gains `getAgendaViewer`).
Run: `npm run build` — Expected: success (no new route; `?meus=1` is read through the existing `useSearchParams` in `app/agenda/page.tsx`).

- [ ] **Step 10: Commit**

```bash
git add lib/agenda/viewer.ts lib/agenda/secretaria-hub-agenda.ts lib/agenda/screen.ts components/agenda/Modals.tsx components/agenda/AgendaView.tsx components/agenda/AgendaScreen.tsx lib/agenda/__tests__/viewer.test.ts lib/agenda/__tests__/screen.test.ts lib/agenda/__tests__/secretaria-hub-agenda-calls.test.ts components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): who is looking - Todos/So os meus for manager-doctors, a restricted doctor books only on his own agenda, hidden appointments (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 8: Editar/Remarcar — form rules, controller, and the old reschedule path removed

**Files:**
- Create: `lib/agenda/edit.ts`
- Modify: `lib/agenda/screen.ts`, `lib/agenda/secretaria-hub-agenda.ts` (drop `rescheduleAppointment` / `AppointmentReschedulePayload`), `lib/agenda/modal-forms.ts` and `lib/agenda/cancel-preview.ts` (drop the reschedule helpers), `components/agenda/AgendaView.tsx`, `components/agenda/AgendaScreen.tsx`, `components/agenda/Modals.tsx` (one comment line)
- Delete: `components/agenda/RescheduleModal.tsx`, `components/agenda/__tests__/reschedule-modal.test.tsx`
- Test: `lib/agenda/__tests__/edit.test.ts` (new), `lib/agenda/__tests__/screen.test.ts`, `lib/agenda/__tests__/modal-forms.test.ts`, `components/agenda/__tests__/agenda-view.test.tsx` (modify)

**Interfaces:**
- Consumes: `editAppointment`, `AppointmentEditPayload`, `EDIT_*_CODE`, `NOT_LIVE_CODE` (Task 4); `actionToast` (Task 4); `rememberPaidNotices`, `loadCheck`, `paidAuto` (Task 5); the six facts on `Appt` (`professionalId`, `professionalName`, `service`, `attendeeName`, `phone`, `patientChannel` — Task 1); `isApptNotFound`, `APPT_NOT_FOUND` (Task 7); `getProfessionals`, `getServices`, `ProfessionalWire`, `ServiceWire` (`lib/real/secretaria-hub.ts`); `slotIsoRangeFromDateKey`.
- Produces (Task 9 uses these exact names):
  - `lib/agenda/edit.ts`: `type EditForm = { date: string; start: number; professionalId: string; service: string; insurance: string; attendeeName: string; attendeeIsPatient: boolean; phone: string }` (pre-filled with the appointment's current values; a field is sent only when it differs; `""` in `professionalId`/`service`/`phone` = keep); `initialEditForm(appt): EditForm`; `type EditIssue = "missing_date" | "past" | "invalid_phone"`; `editFormIssues(appt: Appt, form: EditForm, todayKey: string | null): EditIssue[]` (the phone is checked only when changed); `buildEditPayload(appt, form, o: { notifyOutsideWindow: boolean; allowOverlap: boolean }): AppointmentEditPayload | null` (null = nothing changed); `type EditProfessional = { id: string; name: string }`; `type EditService = { name: string; professionalIds: readonly string[] }`; `type EditOptions = { status: "loading" } | { status: "loaded"; professionals: EditProfessional[]; services: EditService[] } | { status: "failed" }`; `editOptionsFrom(pros, services): EditOptions`; `serviceChoices(options, professionalId): EditService[]`; `type EditModalError = { kind: "slot_unavailable" | "changed" | "not_live" | "failed"; message: string }`; `editErrorFrom(e: unknown): EditModalError` (404 → `not_live` with `APPT_NOT_FOUND`; 403 `professional_not_allowed` → `failed` with the backend's sentence); `type EditConfirm = { form: EditForm; optIn: boolean; remember: boolean }`; copy `EDIT_COPY`, `EDIT_FAILED`, `EDIT_SLOT_TAKEN`, `EDIT_CHANGED`, `EDIT_NOT_LIVE`, `EDIT_CALENDAR_UNAVAILABLE`, `EDITED_BASE`, `EDIT_ISSUE_TEXT`.
  - `screen.ts`: `AgendaModal` gains `{ type: "edit"; appt: Appt; pending: boolean; error: EditModalError | null; options: EditOptions; overlapSeen: boolean }` and loses `reschedule`; scoped `{ type: "edit_options_loaded"; appointmentId: string; options: EditOptions }`; `AgendaHub` gains `editAppointment`, `getProfessionals: (s) => Promise<Pick<ProfessionalWire, "id" | "name" | "is_active">[]>`, `getServices: (s) => Promise<Pick<ServiceWire, "name" | "is_active" | "professional_ids">[]>` and loses `rescheduleAppointment`; controller `openEdit(appt): Promise<void>`, `confirmEdit(c: EditConfirm): Promise<void>`; `openReschedule`, `needWindowCheck`, `confirmReschedule`, `rescheduledToast`, `RESCHEDULE_*` are gone.
  - `AgendaViewProps` lose `onOpenReschedule`, `onNeedWindowCheck`, `onConfirmReschedule` (Task 9 adds `onOpenEdit`, `onConfirmEdit`).

- [ ] **Step 1: Write the failing tests**

Create `lib/agenda/__tests__/edit.test.ts`:

```ts
// Editar/Remarcar (spec 2026-10-09 §1; R7 POST …/edit): only what changed is sent.
import { describe, expect, it } from "vitest";

import { HubApiError } from "@/lib/real/secretaria-hub";
import {
  EDIT_CALENDAR_UNAVAILABLE,
  EDIT_FAILED,
  buildEditPayload,
  editErrorFrom,
  editFormIssues,
  editOptionsFrom,
  initialEditForm,
  serviceChoices,
} from "../edit";
import { slotIsoRangeFromDateKey } from "../hub-mapping";
import type { Appt } from "../types";
import { APPT_NOT_FOUND } from "../viewer";

// What the agenda now carries (R7 Task 14 facts, mapped in Task 1).
const appt: Appt = {
  id: "hub-1", date: "2026-09-29", day: 2, start: 9 * 60, dur: 30,
  patient: "Maria Souza", type: "Consulta", status: "agendado", appointmentId: "ap-1", insurance: "Unimed",
  professionalId: "p1", professionalName: "Ana Souza", service: "Consulta", phone: "5511999990000", patientChannel: "whatsapp",
};
const flags = { notifyOutsideWindow: false, allowOverlap: false };

describe("initialEditForm", () => {
  it("pre-fills everything the agenda carries", () => {
    expect(initialEditForm(appt)).toEqual({
      date: "2026-09-29", start: 540, professionalId: "p1", service: "Consulta", insurance: "Unimed",
      attendeeName: "", attendeeIsPatient: true, phone: "5511999990000",
    });
    expect(initialEditForm({ ...appt, attendeeName: "João Pedro" })).toMatchObject({ attendeeName: "João Pedro", attendeeIsPatient: false });
  });

  it("an appointment the read carried no facts for starts empty, never invented", () => {
    const bare: Appt = { id: "hub-2", date: "2026-09-29", day: 2, start: 540, dur: 30, status: "agendado", appointmentId: "ap-2" };
    expect(initialEditForm(bare)).toMatchObject({ professionalId: "", service: "", insurance: "", attendeeName: "", attendeeIsPatient: true, phone: "" });
  });
});

describe("buildEditPayload", () => {
  it("an untouched form sends nothing", () => {
    expect(buildEditPayload(appt, initialEditForm(appt), flags)).toBeNull();
  });

  it("date or time changed -> start_at, as the slot's own local instant", () => {
    const form = { ...initialEditForm(appt), date: "2026-10-02", start: 600 };
    expect(buildEditPayload(appt, form, flags)).toEqual({
      start_at: slotIsoRangeFromDateKey("2026-10-02", 600, 30).startIso, notify_outside_window: false, allow_overlap: false,
    });
    expect(buildEditPayload(appt, { ...initialEditForm(appt), start: 570 }, flags)?.start_at).toBe(
      slotIsoRangeFromDateKey("2026-09-29", 570, 30).startIso,
    );
  });

  it("doctor and service only when they differ from the current ones", () => {
    const out = buildEditPayload(appt, { ...initialEditForm(appt), professionalId: "pro-2", service: "Retorno" }, flags);
    expect(out).toMatchObject({ professional_id: "pro-2", service: "Retorno" });
    expect(out).not.toHaveProperty("start_at");
    expect(buildEditPayload(appt, { ...initialEditForm(appt), service: " Consulta " }, flags)).toBeNull();
    // A new doctor with the service left on "keep" ("" after the doctor changed): only the doctor travels.
    const onlyPro = buildEditPayload(appt, { ...initialEditForm(appt), professionalId: "pro-2", service: "" }, flags);
    expect(onlyPro).toMatchObject({ professional_id: "pro-2" });
    expect(onlyPro).not.toHaveProperty("service");
  });

  it("convênio: a new label is sent trimmed, an emptied one clears (null), an unchanged one is not sent", () => {
    expect(buildEditPayload(appt, { ...initialEditForm(appt), insurance: "  Particular " }, flags)?.insurance).toBe("Particular");
    expect(buildEditPayload(appt, { ...initialEditForm(appt), insurance: "   " }, flags)?.insurance).toBeNull();
    expect(buildEditPayload(appt, { ...initialEditForm(appt), insurance: " Unimed " }, flags)).toBeNull();
  });

  it("attendee: a typed name is sent; 'the patient themself' clears a name that was there, and is a no-op otherwise", () => {
    const typed = buildEditPayload(appt, { ...initialEditForm(appt), attendeeIsPatient: false, attendeeName: " João Pedro " }, flags);
    expect(typed?.attendee_name).toBe("João Pedro");
    const forOther: Appt = { ...appt, attendeeName: "João Pedro" };
    expect(buildEditPayload(forOther, { ...initialEditForm(forOther), attendeeIsPatient: true }, flags)?.attendee_name).toBeNull();
    expect(buildEditPayload(forOther, initialEditForm(forOther), flags)).toBeNull();
    // Unticked but left blank still means "the patient themself": nothing changed.
    expect(buildEditPayload(appt, { ...initialEditForm(appt), attendeeIsPatient: false }, flags)).toBeNull();
  });

  it("contact phone: sent as digits only when the digits changed; the same number typed differently or emptied is not sent", () => {
    expect(buildEditPayload(appt, { ...initialEditForm(appt), phone: "+55 (11) 98888-7777" }, flags)?.phone).toBe("5511988887777");
    expect(buildEditPayload(appt, { ...initialEditForm(appt), phone: "+55 (11) 99999-0000" }, flags)).toBeNull();
    expect(buildEditPayload(appt, { ...initialEditForm(appt), phone: "" }, flags)).toBeNull();
  });

  it("carries the paid-notice and encaixe flags", () => {
    const out = buildEditPayload(appt, { ...initialEditForm(appt), service: "Retorno" }, { notifyOutsideWindow: true, allowOverlap: true });
    expect(out).toMatchObject({ notify_outside_window: true, allow_overlap: true });
  });
});

describe("editFormIssues", () => {
  it("missing date, past date (once today is known), and a CHANGED phone without country code", () => {
    expect(editFormIssues(appt, { ...initialEditForm(appt), date: "" }, "2026-09-29")).toEqual(["missing_date"]);
    expect(editFormIssues(appt, { ...initialEditForm(appt), date: "2026-09-28" }, "2026-09-29")).toEqual(["past"]);
    expect(editFormIssues(appt, { ...initialEditForm(appt), date: "2026-09-28" }, null)).toEqual([]);
    expect(editFormIssues(appt, { ...initialEditForm(appt), phone: "11988887777" }, "2026-09-29")).toEqual(["invalid_phone"]);
    expect(editFormIssues(appt, { ...initialEditForm(appt), phone: "+55 11 98888-7777" }, "2026-09-29")).toEqual([]);
    expect(editFormIssues(appt, { ...initialEditForm(appt), phone: "5".repeat(16) }, "2026-09-29")).toEqual(["invalid_phone"]);
  });

  it("a legacy stored phone without country code never blocks a form that does not touch it", () => {
    const legacy: Appt = { ...appt, phone: "11988887777" };
    expect(editFormIssues(legacy, { ...initialEditForm(legacy), service: "Retorno" }, "2026-09-29")).toEqual([]);
  });
});

describe("options", () => {
  const options = editOptionsFrom(
    [
      { id: "p2", name: "Bruno Lima", is_active: true },
      { id: "p1", name: "Ana Souza", is_active: true },
      { id: "p3", name: "Carlos Antigo", is_active: false },
    ],
    [
      { name: "Retorno", is_active: true, professional_ids: ["p1"] },
      { name: "Consulta", is_active: true, professional_ids: ["p1", "p2"] },
      { name: "Aposentado", is_active: false, professional_ids: ["p1"] },
    ],
  );

  it("keeps active professionals (by name) and active services", () => {
    expect(options).toEqual({
      status: "loaded",
      professionals: [{ id: "p1", name: "Ana Souza" }, { id: "p2", name: "Bruno Lima" }],
      services: [{ name: "Consulta", professionalIds: ["p1", "p2"] }, { name: "Retorno", professionalIds: ["p1"] }],
    });
  });

  it("services follow the chosen doctor; 'keep' offers all; nothing before the lists load", () => {
    expect(serviceChoices(options, "p2").map((s) => s.name)).toEqual(["Consulta"]);
    expect(serviceChoices(options, "").map((s) => s.name)).toEqual(["Consulta", "Retorno"]);
    expect(serviceChoices({ status: "loading" }, "")).toEqual([]);
  });
});

describe("editErrorFrom", () => {
  const err = (status: number, code: string | null, message = "Mensagem do backend.") => new HubApiError(status, message, code, code ? { code, message } : null);

  it("maps R7's codes, keeping the backend's own Portuguese sentence", () => {
    expect(editErrorFrom(err(409, "slot_unavailable", "Este horário não está livre na agenda."))).toEqual({
      kind: "slot_unavailable", message: "Este horário não está livre na agenda.",
    });
    expect(editErrorFrom(err(409, "appointment_changed")).kind).toBe("changed");
    expect(editErrorFrom(err(409, "not_live")).kind).toBe("not_live");
    expect(editErrorFrom(err(422, "invalid_phone", "Informe o telefone com DDI e DDD, por exemplo 5511988887777."))).toEqual({
      kind: "failed", message: "Informe o telefone com DDI e DDD, por exemplo 5511988887777.",
    });
  });

  it("Google refused (502) is said plainly; anything unknown is the generic failure", () => {
    expect(editErrorFrom(err(502, null, "Bad Gateway"))).toEqual({ kind: "failed", message: EDIT_CALENDAR_UNAVAILABLE });
    expect(editErrorFrom(err(500, null))).toEqual({ kind: "failed", message: EDIT_FAILED });
    expect(editErrorFrom(new TypeError("network"))).toEqual({ kind: "failed", message: EDIT_FAILED });
  });

  it("R7 viewer rules: another doctor's agenda keeps the backend's sentence; a hidden appointment is 'not found' and blocks a retry", () => {
    const msg = "Você só pode manter a consulta na sua própria agenda. Peça à recepção para trocar o médico.";
    expect(editErrorFrom(err(403, "professional_not_allowed", msg))).toEqual({ kind: "failed", message: msg });
    expect(editErrorFrom(new HubApiError(404, "Appointment not found"))).toEqual({ kind: "not_live", message: APPT_NOT_FOUND });
  });
});
```

In `lib/agenda/__tests__/screen.test.ts`:

(a) Remove `RESCHEDULE_VERDICT_CHANGED,` from the `"../screen"` import; add `import { EDIT_CHANGED, EDIT_FAILED } from "../edit";` (`EDIT_CHANGED` is used below only through `toMatchObject` kinds — keep the import for the not-live/changed assertions).

(b) In `fakeHub`, replace the `rescheduleAppointment: …` line with:

```ts
    editAppointment: vi.fn(async (_s, id) => ({ ...wire(id, "rescheduled"), patient_notice: "whatsapp_sent" })),
    getProfessionals: vi.fn(async () => [{ id: "pro-1", name: "Dra. Ana", is_active: true }]),
    getServices: vi.fn(async () => [{ name: "Retorno", is_active: true, professional_ids: ["pro-1"] }]),
```

(c) Delete the whole `describe("reschedule", …)` block.

(d) Append at the end of the file:

```ts
describe("Editar/Remarcar (R7 POST …/edit)", () => {
  const later = () => event("e1", 29, 11); // 11:00, after NOW (10:00)
  const opened = async (hubOver: Partial<AgendaHub> = {}) => {
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [later()]), ...hubOver }) });
    await s.controller.start();
    s.controller.select(appt(s.state()));
    await s.controller.openEdit(appt(s.state()));
    return s;
  };
  const formOf = (s: Awaited<ReturnType<typeof opened>>, over: Partial<EditForm> = {}): EditForm => ({
    ...initialEditForm(appt(s.state())),
    ...over,
  });
  const editCalls = (s: { hub: AgendaHub }) => (s.hub.editAppointment as ReturnType<typeof vi.fn>).mock.calls;

  it("opening reads the 24 h window, the clinic flag and the doctor/service lists", async () => {
    const s = await opened();
    expect(s.hub.getCancelPreview).toHaveBeenCalledWith(expect.anything(), "appt-e1");
    expect(s.state().modal).toMatchObject({
      type: "edit", pending: false, error: null, overlapSeen: false,
      options: { status: "loaded", professionals: [{ id: "pro-1", name: "Dra. Ana" }], services: [{ name: "Retorno", professionalIds: ["pro-1"] }] },
    });
  });

  it("lists that fail to load leave the sheet usable (options failed)", async () => {
    const s = await opened({ getServices: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))) });
    expect(s.state().modal).toMatchObject({ type: "edit", options: { status: "failed" } });
  });

  it("does not open for a terminal appointment or a Google-only slot", async () => {
    const t = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [{ ...later(), status: "attended" as const }]) }) });
    await t.controller.start();
    await t.controller.openEdit(appt(t.state()));
    expect(t.state().modal).toBeNull();
    const g = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [event("g1", 29, 11, null)]) }) });
    await g.controller.start();
    await g.controller.openEdit(appt(g.state(), "hub-g1"));
    expect(g.state().modal).toBeNull();
  });

  it("sends only what changed, learns the status, closes, toasts the notice and refetches", async () => {
    const s = await opened();
    await s.controller.confirmEdit({ form: formOf(s, { date: "2026-09-30" }), optIn: false, remember: false });
    await flush();
    expect(editCalls(s)[0][2]).toEqual({
      start_at: new Date(2026, 8, 30, 11, 0).toISOString(), notify_outside_window: false, allow_overlap: false,
    });
    expect(s.state().modal).toBeNull();
    expect(s.state().knownStatus["appt-e1"]).toBe("rescheduled");
    expect(s.state().toast).toEqual({ tone: "success", message: "Consulta alterada. O paciente foi avisado pelo WhatsApp." });
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("an untouched form sends nothing", async () => {
    const s = await opened();
    await s.controller.confirmEdit({ form: formOf(s), optIn: false, remember: false });
    expect(s.hub.editAppointment).not.toHaveBeenCalled();
  });

  it("sends nothing while the 24 h verdict is loading", async () => {
    const p = deferred<ReturnType<typeof preview>>();
    const s = setup({ hub: fakeHub({ listCalendarEvents: vi.fn(async () => [later()]), getCancelPreview: vi.fn(() => p.promise) }) });
    await s.controller.start();
    void s.controller.openEdit(appt(s.state()));
    await s.controller.confirmEdit({ form: { ...initialEditForm(appt(s.state())), service: "Retorno" }, optIn: false, remember: false });
    expect(s.hub.editAppointment).not.toHaveBeenCalled();
  });

  it("outside 24 h: paid only with the opt-in or the clinic flag; 'Não perguntar novamente' saves the flag first", async () => {
    const off = await opened({ getCancelPreview: vi.fn(async () => preview({ inside_window: false })) });
    await off.controller.confirmEdit({ form: formOf(off, { service: "Retorno" }), optIn: false, remember: false });
    expect(editCalls(off)[0][2].notify_outside_window).toBe(false);

    const on = await opened({ getCancelPreview: vi.fn(async () => preview({ inside_window: false })) });
    await on.controller.confirmEdit({ form: formOf(on, { service: "Retorno" }), optIn: true, remember: true });
    expect(editCalls(on)[0][2].notify_outside_window).toBe(true);
    const approve = (on.hub.approvePaidNotices as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(approve).toBeLessThan((on.hub.editAppointment as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]);

    const auto = await opened({ getCancelPreview: vi.fn(async () => preview({ inside_window: false })), getPaidNoticesAutoApproved: vi.fn(async () => true) });
    await auto.controller.confirmEdit({ form: formOf(auto, { service: "Retorno" }), optIn: false, remember: false });
    expect(editCalls(auto)[0][2].notify_outside_window).toBe(true);
  });

  it("slot taken: the backend's sentence, then a second save goes as an 'encaixe' (allow_overlap)", async () => {
    const edit = vi
      .fn()
      .mockRejectedValueOnce(new HubApiError(409, "Este horário não está livre na agenda.", "slot_unavailable", { code: "slot_unavailable" }))
      .mockResolvedValueOnce({ ...wire("appt-e1", "rescheduled"), patient_notice: "portal_chat" });
    const s = await opened({ editAppointment: edit });
    await s.controller.confirmEdit({ form: formOf(s, { start: 12 * 60 }), optIn: false, remember: false });
    expect(s.state().modal).toMatchObject({
      type: "edit", pending: false, overlapSeen: true, error: { kind: "slot_unavailable", message: "Este horário não está livre na agenda." },
    });
    await s.controller.confirmEdit({ form: formOf(s, { start: 12 * 60 }), optIn: false, remember: false });
    expect(edit.mock.calls[1][2].allow_overlap).toBe(true);
    expect(s.state().modal).toBeNull();
  });

  it("another edit won meanwhile, or the appointment closed: explained, the agenda reloads, nothing is resent", async () => {
    const changed = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(409, "", "appointment_changed"))) });
    await changed.controller.confirmEdit({ form: formOf(changed, { service: "Retorno" }), optIn: false, remember: false });
    expect(changed.state().modal).toMatchObject({ error: { kind: "changed", message: EDIT_CHANGED } });
    expect(changed.hub.listCalendarEvents).toHaveBeenCalledTimes(2);

    const closed = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(409, "Esta consulta já foi cancelada ou encerrada.", "not_live"))) });
    await closed.controller.confirmEdit({ form: formOf(closed, { service: "Retorno" }), optIn: false, remember: false });
    expect(closed.state().modal).toMatchObject({ error: { kind: "not_live" } });
  });

  it("a failure keeps the sheet open with its error and claims nothing", async () => {
    const s = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))) });
    await s.controller.confirmEdit({ form: formOf(s, { service: "Retorno" }), optIn: false, remember: false });
    expect(s.state().modal).toMatchObject({ type: "edit", pending: false, error: { kind: "failed", message: EDIT_FAILED } });
    expect(s.state().toast).toBeNull();
    expect(s.state().knownStatus).toEqual({});
  });

  it("Google disconnected is named", async () => {
    const s = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(422, "Google Calendar not connected. Complete OAuth onboarding first."))) });
    await s.controller.confirmEdit({ form: formOf(s, { service: "Retorno" }), optIn: false, remember: false });
    expect(s.state().modal).toMatchObject({ error: { kind: "failed", message: CALENDAR_NOT_CONNECTED_WRITE } });
  });

  it("R7 403 professional_not_allowed (a restricted doctor moving his appointment): the backend's sentence, nothing learned", async () => {
    const msg = "Você só pode manter a consulta na sua própria agenda. Peça à recepção para trocar o médico.";
    const s = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(403, msg, PROFESSIONAL_NOT_ALLOWED_CODE))) });
    await s.controller.confirmEdit({ form: formOf(s, { professionalId: "pro-9" }), optIn: false, remember: false });
    expect(s.state().modal).toMatchObject({ type: "edit", pending: false, error: { kind: "failed", message: msg } });
    expect(s.state().knownStatus).toEqual({});
  });

  it("R7 404 (the appointment is not in this viewer's agenda any more): 'não encontrada', save blocked, the agenda reloads", async () => {
    const s = await opened({ editAppointment: vi.fn(async () => Promise.reject(new HubApiError(404, "Appointment not found"))) });
    await s.controller.confirmEdit({ form: formOf(s, { service: "Retorno" }), optIn: false, remember: false });
    expect(s.state().modal).toMatchObject({ error: { kind: "not_live", message: APPT_NOT_FOUND } });
    expect(s.hub.listCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it("a double click sends once", async () => {
    const d = deferred<AppointmentWire>();
    const s = await opened({ editAppointment: vi.fn(() => d.promise) });
    const first = s.controller.confirmEdit({ form: formOf(s, { service: "Retorno" }), optIn: false, remember: false });
    await s.controller.confirmEdit({ form: formOf(s, { service: "Retorno" }), optIn: false, remember: false });
    d.resolve(wire("appt-e1", "confirmed"));
    await first;
    expect(s.hub.editAppointment).toHaveBeenCalledTimes(1);
  });
});
```

Add to the imports of `screen.test.ts`: `import { initialEditForm, type EditForm } from "../edit";` (merge with the `EDIT_CHANGED, EDIT_FAILED` import from `"../edit"`). `APPT_NOT_FOUND` and `PROFESSIONAL_NOT_ALLOWED_CODE` are already imported (Task 7).

In `lib/agenda/__tests__/modal-forms.test.ts`: remove from the import list `buildReschedulePayload`, `isSameSlot`, `rescheduleConfirmLabel`, `rescheduleDateIssue`, `rescheduleSuggestion`, `rescheduleWarningState`; delete the `describe("reschedule", …)` block **except** its last test `it("patientFirstName trusts only a name the console wrote", …)`, which moves into `describe("drawer gating and status actions", …)` unchanged (it still uses `hubEvent` and `mapHubEventToAppt`); the import `import { mapHubEventToAppt, slotIsoRangeFromDateKey } from "../hub-mapping";` becomes `import { mapHubEventToAppt } from "../hub-mapping";` (the deleted `buildReschedulePayload` test was its only other user).

In `components/agenda/__tests__/agenda-view.test.tsx`, remove `onOpenReschedule: noop,`, `onNeedWindowCheck: noop,` and `onConfirmReschedule: noop,` from the `render` helper's props.

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/edit.test.ts lib/agenda/__tests__/screen.test.ts`
Expected: FAIL (`../edit` missing; `openEdit`/`confirmEdit` missing).

- [ ] **Step 3: `lib/agenda/edit.ts`**

```ts
// lib/agenda/edit.ts — "Editar/Remarcar" (owner's decision 2026-10-09: one action
// replaces "Remarcar" and the disabled "Editar"; R7 POST …/edit). Pure, so the
// "send only what changed" rule is testable without a DOM.
//
// Since R7 the events read carries everything the form edits (doctor, stored
// service, attendee, contact phone, plus date/time and convênio), so the form is
// pre-filled with the CURRENT values and a field travels only when it differs from
// them; an untouched form builds nothing. Duration is not editable: R7 takes it
// from the new service, else keeps it. A doctor restricted to his own agenda gets
// the doctor field locked by the sheet (EditModal); R7's 403 is the backstop.

import { HubApiError, type ProfessionalWire, type ServiceWire } from "../real/secretaria-hub";
import { slotIsoRangeFromDateKey } from "./hub-mapping";
import {
  EDIT_APPOINTMENT_CHANGED_CODE,
  EDIT_SLOT_UNAVAILABLE_CODE,
  NOT_LIVE_CODE,
  type AppointmentEditPayload,
} from "./secretaria-hub-agenda";
import type { Appt } from "./types";
import { APPT_NOT_FOUND } from "./viewer";

export const EDIT_COPY = {
  title: "Editar/Remarcar consulta",
  date: "Data",
  time: "Horário",
  professional: "Profissional",
  service: "Serviço",
  keep: "Manter o atual",
  noProfessional: "Sem profissional definido",
  currentProfessional: "Profissional atual",
  noService: "Não informado",
  lockedProfessional: "Você só altera consultas da sua própria agenda.",
  insurance: "Convênio",
  insuranceHint: "Ex.: Unimed, Particular. Em branco = não informado.",
  attendee: "Nome de quem será atendido",
  attendeeHint: "Preencha só se a consulta for de outra pessoa.",
  attendeeIsPatient: "A consulta é do próprio paciente",
  phone: "Telefone de contato da consulta",
  phoneHint:
    "Com DDI e DDD, por exemplo 5511988887777. Apagar o número não remove o telefone: ele fica como está. Não muda o WhatsApp nem o e-mail de acesso do paciente.",
  noticeTitle: "Aviso ao paciente",
  noticeHint: "O paciente recebe o que mudou, com os botões Confirmar, Cancelar e Alterar Dados.",
  nothingChanged: "Altere ao menos um campo para salvar.",
  save: "Salvar alterações",
  saveAnyway: "Salvar mesmo assim (encaixe)",
  savePaid: "Salvar e enviar aviso pago",
  checking: "Verificando…",
  pending: "Salvando…",
  back: "Voltar",
  listsFailed: "Não foi possível carregar profissionais e serviços: eles ficam como estão.",
} as const;

export const EDIT_FAILED = "Não foi possível alterar a consulta. Nada foi alterado. Tente novamente.";
export const EDIT_SLOT_TAKEN = "Este horário não está livre na agenda.";
export const EDIT_CHANGED = "A consulta foi alterada por outra pessoa. A agenda foi atualizada: revise e tente de novo.";
export const EDIT_NOT_LIVE = "Esta consulta já foi cancelada ou encerrada.";
export const EDIT_CALENDAR_UNAVAILABLE = "O Google Calendar não respondeu. Nada foi alterado. Tente novamente em instantes.";
export const EDITED_BASE = "Consulta alterada.";

export type EditForm = {
  /** "YYYY-MM-DD", local. */
  date: string;
  /** Minutes from midnight, local. */
  start: number;
  /** Pre-filled with the current doctor ("" = none yet / keep). Sent only when it differs. */
  professionalId: string;
  /** Pre-filled with the stored service ("" = keep). Sent only when it differs. */
  service: string;
  /** Pre-filled with the current convênio; "" clears it. */
  insurance: string;
  /** Pre-filled with who the appointment is for ("" = the patient themself). */
  attendeeName: string;
  /** Ticked = the appointment is the patient's own (no attendee name). */
  attendeeIsPatient: boolean;
  /** Pre-filled with the contact phone; "" or the same digits = keep. */
  phone: string;
};

/** The form as the appointment is now (R7 facts, Task 1); absent facts start empty, never invented. */
export function initialEditForm(appt: Appt): EditForm {
  return {
    date: appt.date,
    start: appt.start,
    professionalId: appt.professionalId ?? "",
    service: appt.service ?? "",
    insurance: appt.insurance ?? "",
    attendeeName: appt.attendeeName ?? "",
    attendeeIsPatient: !appt.attendeeName,
    phone: appt.phone ?? "",
  };
}

const PHONE_MIN_DIGITS = 12; // R7: 12-15 digits with country code
const PHONE_MAX_DIGITS = 15;
const phoneDigits = (v: string): string => v.replace(/\D/g, "");

export type EditIssue = "missing_date" | "past" | "invalid_phone";

export const EDIT_ISSUE_TEXT: Readonly<Record<EditIssue, string>> = {
  missing_date: "Escolha uma data.",
  past: "Escolha uma data a partir de hoje.",
  invalid_phone: "Informe o telefone com DDI e DDD, por exemplo 5511988887777.",
};

/**
 * Why the form cannot be saved yet. `todayKey` is null before mount (no clock): no
 * "past" verdict then. The phone is checked only when it CHANGED: a legacy stored
 * number without country code never blocks a form that does not touch it.
 */
export function editFormIssues(appt: Appt, form: EditForm, todayKey: string | null): EditIssue[] {
  const out: EditIssue[] = [];
  if (form.date === "") out.push("missing_date");
  else if (todayKey && form.date < todayKey) out.push("past");
  const digits = phoneDigits(form.phone);
  const changed = digits !== "" && digits !== phoneDigits(appt.phone ?? "");
  if (changed && (digits.length < PHONE_MIN_DIGITS || digits.length > PHONE_MAX_DIGITS)) {
    out.push("invalid_phone");
  }
  return out;
}

/** The POST body with ONLY the changed fields, or null when nothing changed. */
export function buildEditPayload(
  appt: Appt,
  form: EditForm,
  o: { notifyOutsideWindow: boolean; allowOverlap: boolean },
): AppointmentEditPayload | null {
  const payload: AppointmentEditPayload = { notify_outside_window: o.notifyOutsideWindow, allow_overlap: o.allowOverlap };
  let changed = false;
  if (form.date !== "" && (form.date !== appt.date || form.start !== appt.start)) {
    payload.start_at = slotIsoRangeFromDateKey(form.date, form.start, appt.dur).startIso;
    changed = true;
  }
  if (form.professionalId && form.professionalId !== (appt.professionalId ?? "")) {
    payload.professional_id = form.professionalId;
    changed = true;
  }
  const service = form.service.trim();
  if (service && service !== (appt.service ?? "").trim()) {
    payload.service = service;
    changed = true;
  }
  const insurance = form.insurance.trim();
  if (insurance !== (appt.insurance ?? "").trim()) {
    payload.insurance = insurance === "" ? null : insurance;
    changed = true;
  }
  // "" = the patient themself (R7 null); sent only when it differs from who it is for now.
  const attendee = form.attendeeIsPatient ? "" : form.attendeeName.trim();
  if (attendee !== (appt.attendeeName ?? "").trim()) {
    payload.attendee_name = attendee === "" ? null : attendee;
    changed = true;
  }
  const digits = phoneDigits(form.phone);
  if (digits && digits !== phoneDigits(appt.phone ?? "")) {
    payload.phone = digits;
    changed = true;
  }
  return changed ? payload : null;
}

export type EditProfessional = { id: string; name: string };
export type EditService = { name: string; professionalIds: readonly string[] };
export type EditOptions =
  | { status: "loading" }
  | { status: "loaded"; professionals: EditProfessional[]; services: EditService[] }
  | { status: "failed" };

const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, "pt-BR");

/** Active professionals and services only (R7 refuses the others), sorted by name. */
export function editOptionsFrom(
  pros: readonly Pick<ProfessionalWire, "id" | "name" | "is_active">[],
  services: readonly Pick<ServiceWire, "name" | "is_active" | "professional_ids">[],
): EditOptions {
  return {
    status: "loaded",
    professionals: pros.filter((p) => p.is_active).map((p) => ({ id: p.id, name: p.name })).sort(byName),
    services: services
      .filter((s) => s.is_active)
      .map((s) => ({ name: s.name, professionalIds: [...s.professional_ids] }))
      .sort(byName),
  };
}

/** Services the chosen doctor offers; "keep the current doctor" offers every active one (R7 validates). */
export function serviceChoices(options: EditOptions, professionalId: string): EditService[] {
  if (options.status !== "loaded") return [];
  if (!professionalId) return options.services;
  return options.services.filter((s) => s.professionalIds.includes(professionalId));
}

export type EditModalError =
  | { kind: "slot_unavailable"; message: string }
  | { kind: "changed"; message: string }
  | { kind: "not_live"; message: string }
  | { kind: "failed"; message: string };

/** What the sheet says about a refusal. The backend's structured `message` is Portuguese and shown as is. */
export function editErrorFrom(e: unknown): EditModalError {
  if (!(e instanceof HubApiError)) return { kind: "failed", message: EDIT_FAILED };
  // R7: gone, or not this viewer's - never said which; the save is blocked and the agenda reloads.
  if (e.status === 404) return { kind: "not_live", message: APPT_NOT_FOUND };
  if (e.status === 409 && e.code === EDIT_SLOT_UNAVAILABLE_CODE) return { kind: "slot_unavailable", message: e.message || EDIT_SLOT_TAKEN };
  if (e.status === 409 && e.code === EDIT_APPOINTMENT_CHANGED_CODE) return { kind: "changed", message: e.message || EDIT_CHANGED };
  if (e.status === 409 && e.code === NOT_LIVE_CODE) return { kind: "not_live", message: e.message || EDIT_NOT_LIVE };
  if (e.status === 502) return { kind: "failed", message: e.code && e.message ? e.message : EDIT_CALENDAR_UNAVAILABLE };
  if (e.code && e.message) return { kind: "failed", message: e.message };
  return { kind: "failed", message: EDIT_FAILED };
}

/** What the sheet hands the controller. */
export type EditConfirm = {
  form: EditForm;
  /** "Enviar mesmo assim": the paid notice for a WhatsApp patient outside 24 h. */
  optIn: boolean;
  /** "Não perguntar novamente" (only with optIn). */
  remember: boolean;
};
```

- [ ] **Step 4: Controller (`lib/agenda/screen.ts`)**

(a) Imports: add `import { EDITED_BASE, buildEditPayload, editErrorFrom, editOptionsFrom, type EditConfirm, type EditModalError, type EditOptions } from "./edit";` and `import type { ProfessionalWire, ServiceWire } from "@/lib/real/secretaria-hub";`; add `AppointmentEditPayload` to the type import from `./secretaria-hub-agenda` and remove `AppointmentReschedulePayload` from it; remove `rescheduleWarningState` from the `./modal-forms` import.

(b) Delete `RESCHEDULE_VERDICT_CHANGED`, `RESCHEDULE_FAILED`, `RESCHEDULE_CANCELLED`, the `rescheduledToast` function, the `rescheduleAppointment` member of `AgendaHub`, and the controller functions `openReschedule`, `needWindowCheck`, `confirmReschedule` (and their names in the returned object). In the file header comment replace "(reschedule, status PATCH, the cancel card's 24h re-check)" with "(Editar/Remarcar, status PATCH, release, patient message, the cancel card's 24h re-check)".

(c) In `AgendaModal` replace `| { type: "reschedule"; appt: Appt; pending: boolean; error: string | null }` with:

```ts
  /** "Editar/Remarcar" (R7). `overlapSeen`: the backend said the slot is taken; the next save is an "encaixe". */
  | { type: "edit"; appt: Appt; pending: boolean; error: EditModalError | null; options: EditOptions; overlapSeen: boolean }
```

(d) `TenantScopedAction`: add `| EditModalError` to the `modal_error` error union and add:

```ts
  /** The doctor/service lists for the open Editar/Remarcar sheet. */
  | { type: "edit_options_loaded"; appointmentId: string; options: EditOptions }
```

(e) In `applyScoped`, inside the `modal_error` case, before `return next;` add:

```ts
      if (m?.type === "edit" && kind === "slot_unavailable") return { ...next, modal: { ...m, overlapSeen: true } };
```

and add the case (after `paid_auto_loaded`):

```ts
    case "edit_options_loaded": {
      const m = state.modal;
      if (m?.type !== "edit" || m.appt.appointmentId !== action.appointmentId) return state; // another sheet's answer
      return { ...state, modal: { ...m, options: action.options } };
    }
```

(f) `AgendaHub`: add

```ts
  editAppointment: (s: Session, appointmentId: string, payload: AppointmentEditPayload) => Promise<AppointmentActionWire>;
  getProfessionals: (s: Session) => Promise<Pick<ProfessionalWire, "id" | "name" | "is_active">[]>;
  getServices: (s: Session) => Promise<Pick<ServiceWire, "name" | "is_active" | "professional_ids">[]>;
```

(g) Add the controller methods (after `sendMessage`):

```ts
  /** Opens Editar/Remarcar: the 24 h window + clinic flag, and the doctor/service lists, in parallel. */
  function openEdit(appt: Appt): Promise<void> {
    if (!canWrite(getState()) || !appt.appointmentId || !actionable(appt)) return Promise.resolve();
    const appointmentId = appt.appointmentId;
    dispatch({
      type: "open_modal",
      modal: { type: "edit", appt, pending: false, error: null, options: { status: "loading" }, overlapSeen: false },
    });
    return Promise.all([loadCheck(appointmentId), loadEditOptions(appointmentId)]).then(() => undefined);
  }

  async function loadEditOptions(appointmentId: string): Promise<void> {
    const s = await fresh();
    if (!s) return;
    try {
      const [pros, services] = await Promise.all([hub.getProfessionals(s), hub.getServices(s)]);
      dispatch(scoped(s.tenantId, { type: "edit_options_loaded", appointmentId, options: editOptionsFrom(pros, services) }));
    } catch (e) {
      console.error("agenda: failed to load professionals/services", errorFacts(e));
      noticeSwitch(e);
      dispatch(scoped(s.tenantId, { type: "edit_options_loaded", appointmentId, options: { status: "failed" } }));
    }
  }

  /**
   * "Editar/Remarcar" (R7 POST …/edit). Sends only what changed; nothing while the
   * 24 h verdict loads. The paid notice goes with "Enviar mesmo assim" or the clinic
   * flag; "Não perguntar novamente" is stored first. A taken slot turns the next save
   * into an "encaixe" (allow_overlap); a concurrent change or a closed appointment
   * reloads the agenda and sends nothing more.
   */
  async function confirmEdit(c: EditConfirm): Promise<void> {
    const m = getState().modal;
    if (m?.type !== "edit" || m.pending || !m.appt.appointmentId) return;
    const st = getState();
    const notice = cancelNoticeState(m.appt, st.check);
    if (notice.kind === "loading") return;
    const outside = notice.kind === "whatsapp_outside_window";
    const payload = buildEditPayload(m.appt, c.form, {
      notifyOutsideWindow: outside && (c.optIn || st.paidAuto),
      allowOverlap: m.overlapSeen,
    });
    if (!payload) return; // nothing changed: the sheet keeps its button disabled; this is the backstop
    const appointmentId = m.appt.appointmentId;
    const s = await beginModalWrite("edit");
    if (!s) return;
    const own = (a: TenantScopedAction) => dispatch(scoped(s.tenantId, a));
    try {
      if (outside && c.optIn && c.remember && !st.paidAuto && !(await rememberPaidNotices(s))) return;
      const res = await hub.editAppointment(s, appointmentId, payload);
      own({ type: "known_status", appointmentId, status: res.status });
      own({ type: "modal_done" });
      own({ type: "toast", toast: actionToast(EDITED_BASE, res) });
      refreshAfter(s.tenantId);
    } catch (e) {
      console.error("agenda: failed to edit appointment", errorFacts(e));
      if (isTenantSwitched(e)) {
        await fresh();
        return;
      }
      const error: EditModalError = isCalendarNotConnected(e)
        ? { kind: "failed", message: CALENDAR_NOT_CONNECTED_WRITE }
        : editErrorFrom(e);
      own({ type: "modal_error", error });
      if (error.kind === "changed" || error.kind === "not_live") refreshAfter(s.tenantId);
    } finally {
      own({ type: "modal_pending", pending: false });
    }
  }
```

and add `openEdit, confirmEdit,` to the returned object.

- [ ] **Step 5: Remove the reschedule path elsewhere**

- `lib/agenda/secretaria-hub-agenda.ts`: delete the `AppointmentReschedulePayload` type, the `rescheduleAppointment` function and the `POST …/reschedule` line of the header route list (the backend keeps the route; the agenda no longer calls it).
- `lib/agenda/modal-forms.ts`: delete `isSameSlot`, `buildReschedulePayload`, `RescheduleWarningState`, `rescheduleWarningState`, `rescheduleConfirmLabel`, `rescheduleDateIssue`, `clinicDisplay`, `rescheduleSuggestion` and the `// Reschedule (R4)` section title; remove `describeRescheduleMessage`, `RescheduleMessageWarning` and `AppointmentReschedulePayload` from its imports; in the header comment replace `RescheduleModal` with `EditModal`. Keep `patientFirstName` (moved under the Drawer section title, unchanged).
- `lib/agenda/cancel-preview.ts`: delete `RescheduleMessageWarning`, `describeRescheduleMessage` and the four `reschedule*` entries of `CANCEL_COPY`; in the header comment replace "and the reschedule message warning (ADDENDUM R2/R3/R4)" with "(ADDENDUM R2/R3; TASK-032 R5 flag copy)".
- `components/agenda/Modals.tsx`: the comment line `// Cancel lives in CancelCard.tsx, reschedule in RescheduleModal.tsx.` becomes `// Cancel lives in CancelCard.tsx, Editar/Remarcar in EditModal.tsx.`
- Delete `components/agenda/RescheduleModal.tsx` and `components/agenda/__tests__/reschedule-modal.test.tsx` (`git rm`).
- `components/agenda/AgendaView.tsx`: delete the `RescheduleModal` import and element, the `checkedDates` constant (and its comment), `const resched = …`, the `AppointmentReschedulePayload` import, and the props `onOpenReschedule`, `onNeedWindowCheck`, `onConfirmReschedule` from `AgendaViewProps`. Keep `todayKey` (Task 9 uses it).
- `components/agenda/AgendaScreen.tsx`: remove `rescheduleAppointment` from the import and from `HUB`; add `editAppointment` (from `@/lib/agenda/secretaria-hub-agenda`) and `getProfessionals`, `getServices` (from `@/lib/real/secretaria-hub`) to the imports and to `HUB`; remove the `onOpenReschedule`, `onNeedWindowCheck` and `onConfirmReschedule` props of `<AgendaView>`. In its header comment nothing changes.

- [ ] **Step 6: Run tests, type check, build**

Run: `npx vitest run lib/agenda components/agenda` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors. `git grep -n "reschedule" -- lib components` must list only comments/tests that talk about the backend route or `rescheduled` (the status value), never a call.
Run: `npm run build` — Expected: success.

- [ ] **Step 7: Commit**

```bash
git rm components/agenda/RescheduleModal.tsx components/agenda/__tests__/reschedule-modal.test.tsx
git add lib/agenda/edit.ts lib/agenda/screen.ts lib/agenda/secretaria-hub-agenda.ts lib/agenda/modal-forms.ts lib/agenda/cancel-preview.ts components/agenda/Modals.tsx components/agenda/AgendaView.tsx components/agenda/AgendaScreen.tsx lib/agenda/__tests__/edit.test.ts lib/agenda/__tests__/screen.test.ts lib/agenda/__tests__/modal-forms.test.ts components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): Editar/Remarcar rules and controller on R7 POST edit; old reschedule path removed (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 9: The Editar/Remarcar sheet and its wiring

**Files:**
- Create: `components/agenda/EditModal.tsx`
- Modify: `components/agenda/agenda-modals.css` (append), `components/agenda/AgendaView.tsx`, `components/agenda/AgendaScreen.tsx`
- Test: `components/agenda/__tests__/edit-modal.test.tsx` (new), `components/agenda/__tests__/agenda-view.test.tsx` (append)

**Interfaces:**
- Consumes: Task 8 (`EDIT_COPY`, `EDIT_ISSUE_TEXT`, `EditForm`, `EditConfirm`, `EditOptions`, `EditModalError`, `initialEditForm`, `editFormIssues(appt, form, todayKey)`, `buildEditPayload`, `serviceChoices`, controller `openEdit`/`confirmEdit`); `lockedProfessional` (Task 7, computed as `locked` in `AgendaView`); the `Appt` facts (Task 1); `CancelNoticeBlock` (Task 6); `startOptions`, `cancelNoticeState`, `WindowCheck` (`modal-forms.ts`); primitives `Sheet`, `Button`, `Select`, `TextInput`, `Icon`.
- Produces: `EditModalView(props: EditModalViewProps)` (stateless) and `EditModal(props: EditModalProps)` (owns the form and the two ticks; resets on every open); new **optional** `AgendaViewProps`: `onOpenEdit?: (appt: Appt) => void`, `onConfirmEdit?: (c: EditConfirm) => void`.

```ts
export type EditModalViewProps = {
  open: boolean; appt: Appt | null; check: WindowCheck; autoApproved: boolean; options: EditOptions;
  todayKey: string | null; pending: boolean; error: EditModalError | null; overlapSeen: boolean;
  lockedProfessional: { id: string; name: string } | null;
  form: EditForm; onForm: (patch: Partial<EditForm>) => void;
  optIn: boolean; onOptIn: (next: boolean) => void; remember: boolean; onRemember: (next: boolean) => void;
  onClose: () => void; onConfirm: (c: EditConfirm) => void;
};
```

- [ ] **Step 1: Write the failing tests**

Create `components/agenda/__tests__/edit-modal.test.tsx`:

```tsx
// Editar/Remarcar (spec 2026-10-09 §1; R7 POST …/edit) via renderToStaticMarkup.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { EditModalView, type EditModalViewProps } from "../EditModal";
import { EDIT_COPY, EDIT_ISSUE_TEXT, initialEditForm, type EditOptions } from "../../../lib/agenda/edit";
import { CANCEL_COPY } from "../../../lib/agenda/cancel-preview";
import type { WindowCheck } from "../../../lib/agenda/modal-forms";
import type { Appt } from "../../../lib/agenda/types";

const noop = () => {};
// What the agenda carries since R7 (Task 1 facts).
const appt: Appt = {
  id: "hub-1", date: "2026-09-29", day: 2, start: 9 * 60, dur: 30,
  patient: "Maria Souza", type: "Consulta", status: "agendado", appointmentId: "ap-1", insurance: "Unimed",
  professionalId: "p1", professionalName: "Ana Souza", service: "Consulta", phone: "5511999990000", patientChannel: "whatsapp",
};
const loaded = (over: Partial<{ inside_window: boolean; whatsapp_link: string | null }> = {}): WindowCheck => ({
  status: "loaded",
  preview: {
    inside_window: true, professional_name: "Dra. Ana", template_cost_brl: "R$ 0,35", cost_is_estimate: false,
    whatsapp_link: "https://wa.me/5511999990000", ...over,
  },
});
const options: EditOptions = {
  status: "loaded",
  professionals: [{ id: "p1", name: "Ana Souza" }, { id: "p2", name: "Bruno Lima" }],
  services: [{ name: "Consulta", professionalIds: ["p1", "p2"] }, { name: "Retorno", professionalIds: ["p1"] }],
};
const render = (over: Partial<EditModalViewProps> = {}) =>
  renderToStaticMarkup(
    <EditModalView
      open appt={appt} check={loaded()} autoApproved={false} options={options} todayKey="2026-09-28"
      pending={false} error={null} overlapSeen={false} lockedProfessional={null} form={initialEditForm(appt)} onForm={noop}
      optIn={false} onOptIn={noop} remember={false} onRemember={noop} onClose={noop} onConfirm={noop}
      {...over}
    />,
  );
const save = (html: string) => /<button[^>]*class="btn btn--primary[^"]*"[^>]*>(?:(?!<\/button>).)*<\/button>/.exec(html)![0];
const form = (over: Partial<ReturnType<typeof initialEditForm>>) => ({ ...initialEditForm(appt), ...over });

describe("EditModal — fields", () => {
  it("every field has a visible, bound label", () => {
    const html = render();
    for (const label of [EDIT_COPY.date, EDIT_COPY.time, EDIT_COPY.professional, EDIT_COPY.service, EDIT_COPY.insurance, EDIT_COPY.attendee, EDIT_COPY.phone]) {
      expect(html).toMatch(new RegExp(`<label for="[^"]+" class="field-label">${label}</label>`));
    }
    const cb = /<input id="([^"]+)" type="checkbox"/.exec(html)![1];
    expect(html).toContain(`<label for="${cb}">${EDIT_COPY.attendeeIsPatient}</label>`);
  });

  it("pre-fills everything the agenda carries: date, time, doctor, service, convênio, contact phone", () => {
    const html = render();
    expect(html).toContain('value="2026-09-29"');
    expect(html).toContain('<option value="540" selected="">09:00</option>');
    expect(html).toContain('<option value="p1" selected="">Ana Souza</option>');
    expect(html).toContain('<option value="Consulta" selected="">Consulta</option>');
    expect(html).toContain('value="Unimed"');
    expect(html).toContain('value="5511999990000"');
  });

  it("services follow the chosen doctor; after a doctor change the service keeps the current one", () => {
    const html = render({ form: form({ professionalId: "p2", service: "" }) });
    expect(html).toContain('<option value="" selected="">Manter o atual (Consulta)</option>');
    expect(html).toContain('<option value="Consulta">Consulta</option>');
    expect(html).not.toContain('<option value="Retorno">');
  });

  it("an appointment with no doctor yet starts on 'Sem profissional definido'; the contact phone is never taken from the preview", () => {
    const bare: Appt = { ...appt, professionalId: undefined, professionalName: undefined, phone: undefined };
    const html = render({ appt: bare, form: initialEditForm(bare) });
    expect(html).toContain(`<option value="" selected="">${EDIT_COPY.noProfessional}</option>`);
    expect(html).not.toContain('value="5511999990000"');
  });

  it("a doctor restricted to his own agenda: the doctor field is locked to him, with the reason", () => {
    const html = render({ lockedProfessional: { id: "p1", name: "Ana Souza" } });
    const select = /<label for="[^"]+" class="field-label">Profissional<\/label><select[^>]*>[\s\S]*?<\/select>/.exec(html)![0];
    expect(select).toContain('disabled=""');
    expect(select).toContain('<option value="p1" selected="">Ana Souza</option>');
    expect(select).not.toContain('value="p2"');
    expect(html).toContain(EDIT_COPY.lockedProfessional);
  });

  it("lists that failed to load say so; the current doctor and service stay, nothing else is offered", () => {
    const html = render({ options: { status: "failed" } });
    expect(html).toContain(EDIT_COPY.listsFailed);
    expect(html).toContain('<option value="p1" selected="">Ana Souza</option>');
    expect(html).not.toContain('<option value="p2">');
  });
});

describe("EditModal — save button", () => {
  it("an untouched form cannot be saved, and says why", () => {
    const html = render();
    expect(save(html)).toContain("disabled");
    expect(html).toContain(EDIT_COPY.nothingChanged);
  });

  it("any change enables it", () => {
    expect(save(render({ form: form({ date: "2026-10-02" }) }))).not.toContain("disabled");
    expect(save(render({ form: form({ service: "Retorno" }) }))).not.toContain("disabled");
  });

  it("a past date or a phone without country code blocks it with the reason", () => {
    const past = render({ form: form({ date: "2026-09-27" }) });
    expect(past).toContain(EDIT_ISSUE_TEXT.past);
    expect(save(past)).toContain("disabled");
    const phone = render({ form: form({ phone: "11988887777" }) });
    expect(phone).toContain(EDIT_ISSUE_TEXT.invalid_phone);
    expect(save(phone)).toContain("disabled");
  });

  it("waits for the 24 h verdict; says 'Salvar mesmo assim (encaixe)' after a taken slot", () => {
    expect(save(render({ check: { status: "loading" }, form: form({ service: "Retorno" }) }))).toContain(EDIT_COPY.checking);
    expect(save(render({ check: { status: "loading" }, form: form({ service: "Retorno" }) }))).toContain("disabled");
    const taken = render({ overlapSeen: true, error: { kind: "slot_unavailable", message: "Este horário não está livre na agenda." }, form: form({ start: 600 }) });
    expect(save(taken)).toContain(EDIT_COPY.saveAnyway);
    expect(save(taken)).not.toContain("disabled");
    expect(taken).toMatch(/role="alert"[^>]*>(?:(?!<\/p>).)*Este horário não está livre na agenda\./);
  });

  it("pending: busy and locked", () => {
    const html = render({ pending: true, form: form({ service: "Retorno" }) });
    expect(save(html)).toContain('aria-busy="true"');
    expect(save(html)).toContain(EDIT_COPY.pending);
  });

  it("a closed appointment cannot be saved again", () => {
    expect(save(render({ error: { kind: "not_live", message: "Esta consulta já foi cancelada ou encerrada." }, form: form({ service: "Retorno" }) }))).toContain("disabled");
  });
});

describe("EditModal — patient notice", () => {
  it("outside 24 h: the paid opt-in and 'Não perguntar novamente'; the button says it will pay", () => {
    const html = render({ check: loaded({ inside_window: false }), form: form({ service: "Retorno" }) });
    expect(html).toContain(CANCEL_COPY.rememberLabel);
    expect(html).toContain(EDIT_COPY.noticeHint);
    expect(save(render({ check: loaded({ inside_window: false }), form: form({ service: "Retorno" }), optIn: true }))).toContain(EDIT_COPY.savePaid);
  });

  it("clinic flag on: no question; the paid notice is announced", () => {
    const html = render({ check: loaded({ inside_window: false }), autoApproved: true, form: form({ service: "Retorno" }) });
    expect(html).toContain(CANCEL_COPY.autoApproved);
    expect(html).not.toContain(CANCEL_COPY.rememberLabel);
  });
});
```

Append to `components/agenda/__tests__/agenda-view.test.tsx`:

```tsx
describe("AgendaView — Editar/Remarcar", () => {
  const later: Appt = { ...ITEM, start: 11 * 60 };

  it("the drawer's Editar/Remarcar is enabled once wired", () => {
    const html = render({ ...loaded([later]), selectedId: later.id }, { onOpenEdit: noop });
    expect(buttonTag(html, "Editar/Remarcar")).not.toContain("disabled");
  });

  it("renders the sheet for an open edit modal", () => {
    const state: AgendaState = {
      ...loaded([later]),
      selectedId: later.id,
      modal: { type: "edit", appt: later, pending: false, error: null, options: { status: "loading" }, overlapSeen: false },
    };
    expect(render(state)).toContain("Telefone de contato da consulta");
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run components/agenda/__tests__/edit-modal.test.tsx components/agenda/__tests__/agenda-view.test.tsx`
Expected: FAIL (`../EditModal` missing; Editar/Remarcar disabled).

- [ ] **Step 3: CSS**

Append to `components/agenda/agenda-modals.css`:

```css
/* Editar/Remarcar (TASK-032 R5). No motion on purpose. */
.agm-edit-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.agm-edit-grid .agm-edit-wide { grid-column: 1 / -1; }
@media (max-width: 640px) { .agm-edit-grid { grid-template-columns: 1fr; } }
```

- [ ] **Step 4: `components/agenda/EditModal.tsx`**

```tsx
"use client";

// EditModal — "Editar/Remarcar" (owner's decision 2026-10-09: one action replaces
// "Remarcar" and the disabled "Editar"; R7 POST …/edit). In one save the clinic
// changes date/time, doctor, service, convênio, who the appointment is for and the
// appointment's contact phone. Only what changed is sent
// (lib/agenda/edit.ts::buildEditPayload); an untouched form cannot be saved.
//
// Every field starts on the appointment's CURRENT value (R7 facts, Task 1); a doctor
// restricted to his own agenda gets the doctor field locked to himself (R7 "own";
// the server's 403 professional_not_allowed is the backstop). The patient is told what changed, with the reminder's three buttons
// (R7); a WhatsApp patient outside 24 h is told only with "Enviar mesmo assim" or
// the clinic flag (CancelNoticeBlock). A taken slot turns the button into
// "Salvar mesmo assim (encaixe)". Stateless view + stateful shell (reset on open).

import { useEffect, useId, useState } from "react";

import { Sheet } from "../primitives/Sheet";
import { Button } from "../primitives/Button";
import { Select } from "../primitives/Select";
import { TextInput } from "../primitives/Input";
import { Icon } from "../icons";
import { CancelNoticeBlock } from "./CancelCard";
import { fmtRange, fmtTime } from "../../lib/agenda/types";
import type { Appt } from "../../lib/agenda/types";
import { dayLabelFromKey } from "../../lib/agenda/calendar-dates";
import { cancelNoticeState, startOptions } from "../../lib/agenda/modal-forms";
import type { WindowCheck } from "../../lib/agenda/modal-forms";
import {
  EDIT_COPY,
  EDIT_ISSUE_TEXT,
  buildEditPayload,
  editFormIssues,
  initialEditForm,
  serviceChoices,
  type EditConfirm,
  type EditForm,
  type EditModalError,
  type EditOptions,
} from "../../lib/agenda/edit";
import "./agenda-modals.css";

export type EditModalViewProps = {
  open: boolean;
  appt: Appt | null;
  check: WindowCheck;
  autoApproved: boolean;
  options: EditOptions;
  /** Local "YYYY-MM-DD" of today — the date field's min; null before mount. */
  todayKey: string | null;
  pending: boolean;
  error: EditModalError | null;
  overlapSeen: boolean;
  form: EditForm;
  onForm: (patch: Partial<EditForm>) => void;
  optIn: boolean;
  onOptIn: (next: boolean) => void;
  remember: boolean;
  onRemember: (next: boolean) => void;
  onClose: () => void;
  onConfirm: (c: EditConfirm) => void;
};

export function EditModalView(p: EditModalViewProps) {
  const insuranceHintId = useId();
  const attendeeHintId = useId();
  const phoneHintId = useId();
  const selfId = useId();
  const appt = p.appt;
  const notice = appt ? cancelNoticeState(appt, p.check) : ({ kind: "loading" } as const);
  const checking = notice.kind === "loading";
  const issues = appt ? editFormIssues(appt, p.form, p.todayKey) : [];
  const nothingChanged = appt ? buildEditPayload(appt, p.form, { notifyOutsideWindow: false, allowOverlap: false }) === null : true;
  const blocked = !appt || p.pending || checking || issues.length > 0 || nothingChanged || p.error?.kind === "not_live";
  const paid = notice.kind === "whatsapp_outside_window" && (p.optIn || p.autoApproved);
  const label =
    p.pending ? EDIT_COPY.pending
    : checking ? EDIT_COPY.checking
    : p.overlapSeen ? EDIT_COPY.saveAnyway
    : paid ? EDIT_COPY.savePaid
    : EDIT_COPY.save;
  // Doctor: locked to himself for a restricted viewer (R7 "own"); otherwise the clinic's active
  // doctors plus the current one (even if inactive now), or "no doctor yet" when there is none.
  const locked = p.lockedProfessional;
  const professionals = p.options.status === "loaded" ? p.options.professionals : [];
  const currentPro = appt?.professionalId
    ? { value: appt.professionalId, label: appt.professionalName ?? EDIT_COPY.currentProfessional }
    : null;
  const professionalOptions = locked
    ? [{ value: locked.id, label: locked.name }]
    : [
        ...(currentPro ? [] : [{ value: "", label: EDIT_COPY.noProfessional }]),
        ...(currentPro && !professionals.some((x) => x.id === currentPro.value) ? [currentPro] : []),
        ...professionals.map((x) => ({ value: x.id, label: x.name })),
      ];
  // Service: what the chosen doctor offers; "" = keep the current one (after a doctor change);
  // the current one stays listed while the doctor is the current one.
  const currentService = appt?.service?.trim() ?? "";
  const sameDoctor = !p.form.professionalId || p.form.professionalId === (appt?.professionalId ?? "");
  const services = serviceChoices(p.options, p.form.professionalId);
  const serviceOptions = [
    { value: "", label: currentService ? `${EDIT_COPY.keep} (${currentService})` : EDIT_COPY.noService },
    ...(currentService && sameDoctor && !services.some((x) => x.name === currentService)
      ? [{ value: currentService, label: currentService }]
      : []),
    ...services.map((x) => ({ value: x.name, label: x.name })),
  ];

  const footer = (
    <>
      <Button variant="ghost" onClick={p.onClose} disabled={p.pending}>
        {EDIT_COPY.back}
      </Button>
      <Button
        variant="primary"
        icon="sliders"
        disabled={blocked}
        aria-busy={p.pending || undefined}
        onClick={blocked ? undefined : () => p.onConfirm({ form: p.form, optIn: p.optIn, remember: p.remember && p.optIn })}
      >
        {label}
      </Button>
    </>
  );

  return (
    <Sheet open={p.open} onClose={p.onClose} title={EDIT_COPY.title} footer={footer} className="agm-card agm-card--wide" dismissible={!p.pending}>
      {appt && (
        <>
          <p className="agm-sub">
            {appt.patient || "Paciente"} · {dayLabelFromKey(appt.date)} · {fmtRange(appt.start, appt.dur)}
          </p>

          <div className="agm-edit-grid">
            <TextInput
              label={EDIT_COPY.date}
              type="date"
              value={p.form.date}
              min={p.todayKey ?? undefined}
              onChange={(e) => p.onForm({ date: e.target.value })}
              disabled={p.pending}
            />
            <Select
              label={EDIT_COPY.time}
              value={String(p.form.start)}
              options={startOptions(p.form.start).map((m) => ({ value: String(m), label: fmtTime(m) }))}
              onChange={(v) => p.onForm({ start: Number(v) })}
              disabled={p.pending}
            />
            <Select
              label={EDIT_COPY.professional}
              value={locked ? locked.id : p.form.professionalId}
              options={professionalOptions}
              // A new doctor may not offer the current service: it goes back to "keep" ("");
              // choosing the current doctor again restores the current service.
              onChange={(v) => p.onForm({ professionalId: v, service: v === (appt.professionalId ?? "") ? currentService : "" })}
              disabled={p.pending || locked !== null}
            />
            <Select
              label={EDIT_COPY.service}
              value={p.form.service}
              options={serviceOptions}
              onChange={(v) => p.onForm({ service: v })}
              disabled={p.pending}
            />
            {locked && <p className="agm-hint agm-edit-wide">{EDIT_COPY.lockedProfessional}</p>}
            {p.options.status === "failed" && <p className="agm-hint agm-edit-wide">{EDIT_COPY.listsFailed}</p>}

            <div className="agm-edit-wide">
              <TextInput
                label={EDIT_COPY.insurance}
                value={p.form.insurance}
                aria-describedby={insuranceHintId}
                onChange={(e) => p.onForm({ insurance: e.target.value })}
                disabled={p.pending}
              />
              <p id={insuranceHintId} className="agm-hint">{EDIT_COPY.insuranceHint}</p>
            </div>

            <div className="agm-edit-wide">
              <TextInput
                label={EDIT_COPY.attendee}
                value={p.form.attendeeName}
                aria-describedby={attendeeHintId}
                onChange={(e) => p.onForm({ attendeeName: e.target.value })}
                disabled={p.pending || p.form.attendeeIsPatient}
              />
              <p id={attendeeHintId} className="agm-hint">{EDIT_COPY.attendeeHint}</p>
              <div className="agm-optin">
                <input
                  id={selfId}
                  type="checkbox"
                  checked={p.form.attendeeIsPatient}
                  disabled={p.pending}
                  onChange={(e) => p.onForm({ attendeeIsPatient: e.target.checked })}
                />
                <label htmlFor={selfId}>{EDIT_COPY.attendeeIsPatient}</label>
              </div>
            </div>

            <div className="agm-edit-wide">
              <TextInput
                label={EDIT_COPY.phone}
                type="tel"
                value={p.form.phone}
                placeholder="5511988887777"
                aria-describedby={phoneHintId}
                onChange={(e) => p.onForm({ phone: e.target.value })}
                disabled={p.pending}
              />
              <p id={phoneHintId} className="agm-hint">{EDIT_COPY.phoneHint}</p>
            </div>
          </div>

          {issues.map((i) => (
            <p key={i} className="agm-hint">{EDIT_ISSUE_TEXT[i]}</p>
          ))}
          {issues.length === 0 && nothingChanged && <p className="agm-hint">{EDIT_COPY.nothingChanged}</p>}

          <section className="agm-section" aria-label={EDIT_COPY.noticeTitle}>
            <h3 className="agm-section-title">{EDIT_COPY.noticeTitle}</h3>
            <p className="agm-hint">{EDIT_COPY.noticeHint}</p>
            <CancelNoticeBlock
              state={notice}
              optIn={p.optIn}
              onOptIn={p.onOptIn}
              disabled={p.pending}
              autoApproved={p.autoApproved}
              remember={p.remember}
              onRemember={p.onRemember}
            />
          </section>

          {p.error && (
            <p className="agm-note agm-note--danger" role="alert">
              <Icon name="alert" size={16} />
              <span>{p.error.message}</span>
            </p>
          )}
        </>
      )}
    </Sheet>
  );
}

/** Before the first open (no appointment yet): nothing is rendered from it. */
const BLANK_FORM: EditForm = {
  date: "", start: 0, professionalId: "", service: "", insurance: "", attendeeName: "", attendeeIsPatient: false, phone: "",
};

export type EditModalProps = Omit<EditModalViewProps, "form" | "onForm" | "optIn" | "onOptIn" | "remember" | "onRemember">;

export function EditModal(props: EditModalProps) {
  const { open, appt } = props;
  const [form, setForm] = useState<EditForm>(() => (appt ? initialEditForm(appt) : BLANK_FORM));
  const [optIn, setOptIn] = useState(false);
  const [remember, setRemember] = useState(false);
  // Fresh form on every open, seeded from what the agenda knows; billed choices never carry over.
  useEffect(() => {
    if (!open || !appt) return;
    setForm(initialEditForm(appt));
    setOptIn(false);
    setRemember(false);
  }, [open, appt?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <EditModalView
      {...props}
      form={form}
      onForm={(patch) => setForm((f) => ({ ...f, ...patch }))}
      optIn={optIn}
      onOptIn={setOptIn}
      remember={remember}
      onRemember={setRemember}
    />
  );
}
```

- [ ] **Step 5: Wire `AgendaView.tsx`**

Imports: `import { EditModal } from "./EditModal";`, `import type { EditConfirm } from "@/lib/agenda/edit";`. Add to `AgendaViewProps`:

```ts
  onOpenEdit?: (appt: Appt) => void;
  onConfirmEdit?: (c: EditConfirm) => void;
```

Next to `const release = …` add `const edit = modal?.type === "edit" ? modal : null;`. On `<Drawer …>` add `onEdit={writable ? p.onOpenEdit : undefined}`. After `<MessageModal … />` add:

```tsx
      <EditModal
        open={edit !== null}
        appt={edit?.appt ?? null}
        check={state.check}
        autoApproved={state.paidAuto}
        options={edit?.options ?? { status: "loading" }}
        todayKey={todayKey}
        pending={edit?.pending ?? false}
        error={edit?.error ?? null}
        overlapSeen={edit?.overlapSeen ?? false}
        lockedProfessional={locked}
        onClose={p.onCloseModal}
        onConfirm={(c) => p.onConfirmEdit?.(c)}
      />
```

- [ ] **Step 6: Wire `AgendaScreen.tsx`**

In the `<AgendaView …>` props add:

```tsx
      onOpenEdit={(appt) => void controller.openEdit(appt)}
      onConfirmEdit={(c) => void controller.confirmEdit(c)}
```

- [ ] **Step 7: Run tests, type check, build**

Run: `npx vitest run components/agenda lib/agenda` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors.
Run: `npm run build` — Expected: success.

- [ ] **Step 8: Commit**

```bash
git add components/agenda/EditModal.tsx components/agenda/agenda-modals.css components/agenda/AgendaView.tsx components/agenda/AgendaScreen.tsx components/agenda/__tests__/edit-modal.test.tsx components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): Editar/Remarcar sheet (date, time, doctor, service, convênio, attendee, contact phone) (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 10: Configuration data layer — the `Reminders` slice (paid-notice flag + extra-reminder lead: hydrate, edit, save)

**Files:**
- Modify: `lib/real/secretaria-hub.ts` (`TenantConfigWire`, `TenantConfigUpdatePayload`: the two reminder keys; `paid_notices_auto_approved` is already there since Task 4)
- Modify: `lib/contexto/types.ts`, `lib/contexto/hub-mapping.ts`, `lib/contexto/snapshot.ts` (`TenantSlices` only), `lib/contexto/screen.ts`
- Create: `lib/contexto/reminders.ts`
- Test: `lib/contexto/__tests__/reminders.test.ts`

**Interfaces:**
- Consumes: R7's `paid_notices_auto_approved` (Task 2) and `reminders_v2_enabled` (read-only) / `reminder_extra_lead_minutes` (`1500..20160`, `null` = off; Task 16) — read by `GET /tenants/me/config`, written by `PUT /tenants/me/configuration` `{"tenant": {...}}` (see "Consumes"). Each half still renders only when its key is present, so an older backend hides it instead of writing nowhere.
- Produces:
  - `type Reminders = { supported: boolean; v2Enabled: boolean; extraLeadMinutes: number | null; paidSupported: boolean; paidAutoApproved: boolean }`, `DEFAULT_REMINDERS` (`types.ts`); `TenantSlices.reminders: Reminders`.
  - `applyWireReminders(cfg: TenantConfigWire): Reminders` (`hub-mapping.ts`); `buildConfigUpdatePayload(..., clinicDays, reminders?: Reminders)` — the new **9th** parameter is optional; `reminder_extra_lead_minutes` is added only when `reminders?.supported`, `paid_notices_auto_approved` only when `reminders?.paidSupported`.
  - Reducer actions `{ type: "set_reminder_lead"; extraLeadMinutes: number | null }` and `{ type: "set_paid_notices"; value: boolean }`.
  - `lib/contexto/reminders.ts`: `LEAD_PRESET_DAYS`, `LEAD_MIN_MINUTES = 1500`, `LEAD_MAX_MINUTES = 20160`, `leadInRange(min: number): boolean`, `formatLead(min)`, `leadOptions(current)`, `parseLead(value)`.
  - `buildConfigUpdatePayload` omits a lead outside `1500..20160` (a legacy value R7 would 422): absent = unchanged.

- [ ] **Step 1: Write the failing tests**

Create `lib/contexto/__tests__/reminders.test.ts`:

```ts
// The "Lembretes e avisos" slice (TASK-032 R5): hydration is fail-open; each key is written only when the backend exposed it.
import { describe, expect, it } from "vitest";

import { applyWireReminders, buildConfigUpdatePayload } from "../hub-mapping";
import { DEFAULT_REMINDERS, type Reminders } from "../types";
import { tenantSlicesFromWire } from "../snapshot";
import { INITIAL_SCREEN_STATE, screenReducer } from "../screen";
import { formatLead, leadInRange, leadOptions, parseLead } from "../reminders";
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
    expect(DEFAULT_REMINDERS).toEqual({ supported: false, v2Enabled: false, extraLeadMinutes: null, paidSupported: false, paidAutoApproved: false });
  });

  it("R7's paid-notice flag alone makes the switch supported, with its stored value", () => {
    expect(applyWireReminders(tenantWire({ paid_notices_auto_approved: true }))).toEqual(r({ paidSupported: true, paidAutoApproved: true }));
    expect(applyWireReminders(tenantWire({ paid_notices_auto_approved: false }))).toEqual(r({ paidSupported: true }));
  });

  it("the lead key present (even null) makes the lead supported; null is 'no extra reminder'", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: false, reminder_extra_lead_minutes: null }))).toEqual(r({ supported: true }));
  });

  it("reads the lead and the read-only v2 flag", () => {
    expect(applyWireReminders(tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: 7200 }))).toEqual(
      r({ supported: true, v2Enabled: true, extraLeadMinutes: 7200 }),
    );
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

describe("buildConfigUpdatePayload — the two keys", () => {
  it("NEVER sends either key when the backend did not expose it (parameter omitted, or not supported)", () => {
    for (const p of [payloadFor(), payloadFor(tenantWire(), DEFAULT_REMINDERS)]) {
      expect("reminder_extra_lead_minutes" in p).toBe(false);
      expect("paid_notices_auto_approved" in p).toBe(false);
    }
  });

  it("the lead, when supported; 'off' travels as an explicit null (distinct from omitted)", () => {
    expect(payloadFor(tenantWire(), r({ supported: true, extraLeadMinutes: 7200 })).reminder_extra_lead_minutes).toBe(7200);
    const off = payloadFor(tenantWire(), r({ supported: true }));
    expect("reminder_extra_lead_minutes" in off).toBe(true);
    expect(off.reminder_extra_lead_minutes).toBeNull();
  });

  it("the paid flag, when supported — false included (the switch turned back off)", () => {
    expect(payloadFor(tenantWire(), r({ paidSupported: true, paidAutoApproved: true })).paid_notices_auto_approved).toBe(true);
    expect(payloadFor(tenantWire(), r({ paidSupported: true, paidAutoApproved: false })).paid_notices_auto_approved).toBe(false);
  });

  it("never sends the read-only v2 flag", () => {
    expect("reminders_v2_enabled" in payloadFor(tenantWire(), r({ supported: true, v2Enabled: true, extraLeadMinutes: 7200 }))).toBe(false);
  });

  it("a legacy lead outside R7's 1500..20160 is never re-sent (it would 422 the whole save); the bounds themselves are", () => {
    for (const legacy of [720, 1440, 20161, 43200]) {
      expect("reminder_extra_lead_minutes" in payloadFor(tenantWire(), r({ supported: true, extraLeadMinutes: legacy }))).toBe(false);
    }
    expect(payloadFor(tenantWire(), r({ supported: true, extraLeadMinutes: 1500 })).reminder_extra_lead_minutes).toBe(1500);
    expect(payloadFor(tenantWire(), r({ supported: true, extraLeadMinutes: 20160 })).reminder_extra_lead_minutes).toBe(20160);
  });
});

describe("set_reminder_lead / set_paid_notices", () => {
  it("change only their own field; the support flags stay as hydrated", () => {
    let st = screenReducer(INITIAL_SCREEN_STATE, { type: "session_resolved", session: { token: "x.e30.y", tenantId: TENANT_ID, email: "d@exemplo.test", role: "manager", userId: "u" } });
    st = screenReducer(st, { type: "hydration", action: { type: "hydration_started", generation: 2, rosterGeneration: 2 } });
    st = screenReducer(st, {
      type: "tenant_loaded", generation: 2, tenantId: TENANT_ID,
      wire: tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: null, paid_notices_auto_approved: true }),
    });
    st = screenReducer(st, { type: "set_reminder_lead", extraLeadMinutes: 7200 });
    st = screenReducer(st, { type: "set_paid_notices", value: false });
    expect(st.tenant.reminders).toEqual({ supported: true, v2Enabled: true, extraLeadMinutes: 7200, paidSupported: true, paidAutoApproved: false });
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

  it("every preset is inside R7's 1500..20160; a legacy value outside it is still shown as stored", () => {
    for (const o of leadOptions(null).slice(1)) expect(leadInRange(Number(o.value))).toBe(true);
    expect(leadInRange(1499)).toBe(false);
    expect(leadInRange(20161)).toBe(false);
    expect(leadOptions(720).map((o) => o.value)).toContain("720");
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

In `lib/real/secretaria-hub.ts`, in `TenantConfigWire`, after the `paid_notices_auto_approved?: boolean;` line Task 4 added, add:

```ts
  // TASK-032 reminders, exposed by R7 Task 16. Both ABSENT on an older backend:
  // the extra-reminder field is hidden then and never written.
  // `reminder_extra_lead_minutes`: 1500..20160 minutes; null = no extra reminder.
  reminders_v2_enabled?: boolean;
  reminder_extra_lead_minutes?: number | null;
```

and in `TenantConfigUpdatePayload`, after `paid_notices_auto_approved: boolean;` add:

```ts
  reminder_extra_lead_minutes: number | null;
  // reminders_v2_enabled is READ-ONLY: never part of a PUT body
```

- [ ] **Step 4: Types, mapping, payload, slices, reducer**

`lib/contexto/types.ts` — after `DEFAULT_PIX_DEPOSIT`:

```ts
// ---------------------------------------------------------------------------
// Lembretes e avisos (TASK-032 R5) — the extra reminder and the paid-notice switch
// ---------------------------------------------------------------------------

// Each half is shown and written ONLY when the backend exposed its key:
// `supported` <- `reminder_extra_lead_minutes` (R7 Task 16),
// `paidSupported` <- `paid_notices_auto_approved` (R7 Task 2). `v2Enabled`
// (`reminders_v2_enabled`) is READ-ONLY: the owner turns reminders on per clinic.
export type Reminders = {
  supported: boolean;
  v2Enabled: boolean;
  /** Minutes before the appointment; null = no extra reminder. */
  extraLeadMinutes: number | null;
  paidSupported: boolean;
  /** "Enviar avisos pagos sem perguntar" — the agenda's "Não perguntar novamente". */
  paidAutoApproved: boolean;
};

export const DEFAULT_REMINDERS: Reminders = {
  supported: false,
  v2Enabled: false,
  extraLeadMinutes: null,
  paidSupported: false,
  paidAutoApproved: false,
};
```

`lib/contexto/hub-mapping.ts` — add `DEFAULT_REMINDERS` and `type Reminders` to the existing imports from `./types` and `import { leadInRange } from "./reminders";` (created in Step 5), then add after `applyWirePixDeposit`:

```ts
// Lembretes e avisos: fail open. A key the backend does not send leaves its half
// unsupported (hidden, never written). A non-positive or non-numeric lead reads as
// "no extra reminder", never as a number; only the literal `true` approves paid notices.
export function applyWireReminders(cfg: TenantConfigWire): Reminders {
  const paid = cfg.paid_notices_auto_approved;
  const paidPart = { paidSupported: typeof paid === "boolean", paidAutoApproved: paid === true };
  if (cfg.reminder_extra_lead_minutes === undefined) return { ...DEFAULT_REMINDERS, ...paidPart };
  const lead = cfg.reminder_extra_lead_minutes;
  return {
    supported: true,
    v2Enabled: cfg.reminders_v2_enabled === true,
    extraLeadMinutes: typeof lead === "number" && Number.isFinite(lead) && lead > 0 ? Math.trunc(lead) : null,
    ...paidPart,
  };
}
```

and in `buildConfigUpdatePayload`: replace the signature tail

```ts
  clinicDays: DayConfig[],
): TenantConfigUpdatePayload {
  return {
```

with

```ts
  clinicDays: DayConfig[],
  reminders?: Reminders,
): TenantConfigUpdatePayload {
  const payload: TenantConfigUpdatePayload = {
```

and the end

```ts
    google_calendar_mode: gcalMode,
  };
}
```

with

```ts
    google_calendar_mode: gcalMode,
  };
  // Only what the backend exposed: an explicit null turns the extra reminder off,
  // an explicit false turns paid notices back to "ask every time". A legacy lead
  // outside R7's 1500..20160 is left out (absent = unchanged): resending it would 422
  // the whole save, and the clinic did not touch it.
  const lead = reminders?.extraLeadMinutes ?? null;
  if (reminders?.supported && (lead === null || leadInRange(lead))) payload.reminder_extra_lead_minutes = lead;
  if (reminders?.paidSupported) payload.paid_notices_auto_approved = reminders.paidAutoApproved;
  return payload;
}
```

Also extend the comment above the function with one line: `Lembretes e avisos (reminder_extra_lead_minutes / paid_notices_auto_approved) only when the backend exposes each key.`

`lib/contexto/snapshot.ts` — import `applyWireReminders` (from `./hub-mapping`, with the other `applyWire*`) and `DEFAULT_REMINDERS` / `type Reminders` (from `./types`); add `reminders: Reminders;` to `TenantSlices` (after `pixDeposit`); add `reminders: applyWireReminders(cfg),` to `tenantSlicesFromWire` (after `pixDeposit`) and `reminders: DEFAULT_REMINDERS,` to `emptyTenantSlices` (after `pixDeposit`). (Do not touch `SectionId`/`dirtySections` here: Task 11.)

`lib/contexto/screen.ts` — add to `ScreenAction` after the `set_pix` member:

```ts
  | { type: "set_reminder_lead"; extraLeadMinutes: number | null }
  | { type: "set_paid_notices"; value: boolean }
```

add the reducer cases after `case "set_pix": …`:

```ts
    case "set_reminder_lead":
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...state.tenant.reminders, extraLeadMinutes: action.extraLeadMinutes } },
      };
    case "set_paid_notices":
      return {
        ...state,
        tenant: { ...state.tenant, reminders: { ...state.tenant.reminders, paidAutoApproved: action.value } },
      };
```

and pass `tenant.reminders` as the new last argument of `buildConfigUpdatePayload(...)` inside `buildSaveDeps` (after `tenant.clinicDays,`).

- [ ] **Step 5: `lib/contexto/reminders.ts`**

```ts
// lib/contexto/reminders.ts — the choices behind the "Lembrete extra" field (TASK-032 R5).
// Pure, so a stored value that is not a preset is testable without a DOM.

export const LEAD_PRESET_DAYS = [2, 3, 5, 7, 10, 14] as const;
const DAY_MIN = 1440;

/** R7 Task 16 bounds: more than the fixed 1-day reminder, at most 14 days. */
export const LEAD_MIN_MINUTES = 1500;
export const LEAD_MAX_MINUTES = 20160;

/** Whether R7 accepts this lead (an integer within the bounds). */
export function leadInRange(min: number): boolean {
  return Number.isInteger(min) && min >= LEAD_MIN_MINUTES && min <= LEAD_MAX_MINUTES;
}

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

Run: `npx vitest run lib/contexto` — Expected: PASS (existing payload equality tests unchanged: the new parameter is omitted by tests and both support flags are false for the fixtures).
Run: `npm run typecheck` — if a test builds a `TenantSlices` object literally and now misses `reminders`, add `reminders: DEFAULT_REMINDERS` to that literal (import from `@/lib/contexto/types`). Expected after that: no errors.

- [ ] **Step 7: Commit**

```bash
git add lib/real/secretaria-hub.ts lib/contexto/types.ts lib/contexto/hub-mapping.ts lib/contexto/snapshot.ts lib/contexto/screen.ts lib/contexto/reminders.ts lib/contexto/__tests__/reminders.test.ts
git commit -m "feat(contexto): reminders slice - paid-notice switch and extra reminder lead, written only when exposed (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Plus any test file touched only to add the new slice field; list it in `git add`.)

---

## Task 11: "Lembretes e avisos" section in the Contexto screen, and the post-consult hint

**Files:**
- Create: `components/contexto/ReminderSection.tsx`
- Modify: `lib/contexto/snapshot.ts` (`SectionId`, `dirtySections`), `lib/contexto/toc.ts` (`TOC_LABELS`), `components/contexto/ContextoView.tsx`, `components/contexto/PostConsultSection.tsx` (one tip), `components/contexto/contexto.css` (one rule)
- Test: `components/contexto/__tests__/reminder-section.test.tsx` (new), `components/contexto/__tests__/contexto-screen.test.tsx`, `lib/contexto/__tests__/snapshot.test.ts`, `lib/contexto/__tests__/toc.test.ts`, `components/contexto/__tests__/post-consult-section.test.tsx` (modify/append)

**Interfaces:**
- Consumes: `Reminders`, `leadOptions`, `parseLead`, actions `set_reminder_lead` / `set_paid_notices` (Task 10); primitives `Panel`, `Select`, `Badge`, `Toggle`.
- Produces: `ReminderSection({ v: Reminders; setLead: (minutes: number | null) => void; setPaid: (on: boolean) => void; readOnly?: boolean })` (hook-free); `SectionId` gains `"rem"`; `CONTEXTO_SECTION_ORDER` becomes `["prof","gcal","ctx","facts","srv","disp","msg","rem","pix","pos"]`; `TOC_LABELS.rem = "Lembretes e avisos"`; `remVisible(r: Reminders): boolean` exported from `ContextoView.tsx`; the `rem` anchor and its TOC entry render **only** when `remVisible(tenant.reminders)`; `POST_CONSULT_TIP` exported from `PostConsultSection.tsx`.

- [ ] **Step 1: Write the failing tests**

Create `components/contexto/__tests__/reminder-section.test.tsx`:

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
const noop = () => {};
const html = (v: Reminders, readOnly = false) =>
  renderToStaticMarkup(<ReminderSection v={v} setLead={noop} setPaid={noop} readOnly={readOnly} />);

describe("ReminderSection — paid notices (R7)", () => {
  it("a named switch says in words whether paid notices go without asking", () => {
    const off = html({ ...DEFAULT_REMINDERS, paidSupported: true });
    expect(off).toContain('aria-label="Lembretes e avisos"');
    expect(off).toContain('role="switch" aria-checked="false" aria-label="Enviar avisos pagos sem perguntar"');
    expect(off).toContain("A agenda pergunta antes de cada aviso pago.");
    const on = html({ ...DEFAULT_REMINDERS, paidSupported: true, paidAutoApproved: true });
    expect(on).toContain('aria-checked="true"');
    expect(on).toContain("Os avisos pagos saem sem perguntar.");
  });

  it("wires the switch to setPaid and locks it while read-only", () => {
    const setPaid = vi.fn();
    const toggle = findAllByType(ReminderSection({ v: { ...DEFAULT_REMINDERS, paidSupported: true, paidAutoApproved: true }, setLead: noop, setPaid }), Toggle)[0];
    (toggle.props as { onChange: (v: boolean) => void }).onChange(false);
    expect(setPaid).toHaveBeenCalledWith(false);
    const locked = findAllByType(ReminderSection({ v: { ...DEFAULT_REMINDERS, paidSupported: true }, setLead: noop, setPaid, readOnly: true }), Toggle)[0];
    expect((locked.props as { disabled?: boolean }).disabled).toBe(true);
  });

  it("no paid switch when the backend does not expose the flag", () => {
    expect(html({ ...DEFAULT_REMINDERS, supported: true })).not.toContain('role="switch"');
  });
});

describe("ReminderSection — extra reminder", () => {
  it("has a labelled select (Lembrete extra) with Desligado selected when nothing is configured", () => {
    const out = html(both);
    expect(out).toMatch(/<label for="[^"]+" class="field-label">Lembrete extra<\/label>/);
    expect(out).toContain('<option value="off" selected="">Desligado</option>');
  });

  it("shows the stored value even when it is not a preset", () => {
    expect(html({ ...both, extraLeadMinutes: 1500 })).toContain('<option value="1500" selected="">1500 minutos antes</option>');
  });

  it("says in words whether the automatic reminders are on, never by colour alone", () => {
    expect(html(both)).toContain("Lembretes ainda não ativos");
    const on = html({ ...both, v2Enabled: true });
    expect(on).toContain("Lembretes ativos");
    expect(on).not.toContain("ainda não ativos");
  });

  it("wires the select to setLead: a preset becomes minutes, Desligado becomes null; read-only locks it", () => {
    const setLead = vi.fn();
    const select = findAllByType(ReminderSection({ v: both, setLead, setPaid: noop }), Select)[0];
    const props = select.props as { value: string; onChange: (v: string) => void };
    expect(props.value).toBe("off");
    props.onChange("7200");
    props.onChange("off");
    expect(setLead.mock.calls).toEqual([[7200], [null]]);
    const locked = findAllByType(ReminderSection({ v: both, setLead, setPaid: noop, readOnly: true }), Select)[0];
    expect((locked.props as { disabled?: boolean }).disabled).toBe(true);
  });

  it("no extra-reminder field while the backend does not expose it", () => {
    expect(html({ ...DEFAULT_REMINDERS, paidSupported: true })).not.toContain("Lembrete extra");
  });
});
```

In `components/contexto/__tests__/contexto-screen.test.tsx`:
- replace `expect(CONTEXTO_SECTION_ORDER).toEqual(["prof", "gcal", "ctx", "facts", "srv", "disp", "msg", "pix", "pos"]);` with `expect(CONTEXTO_SECTION_ORDER).toEqual(["prof", "gcal", "ctx", "facts", "srv", "disp", "msg", "rem", "pix", "pos"]);`;
- in that same test and in `it("is absent when the screen passes no completeness …")`, replace `expect(ids).toEqual([...CONTEXTO_SECTION_ORDER]);` with:

```tsx
    // The default fixture's backend exposes neither reminder key, so the "rem" anchor is not drawn.
    expect(ids).toEqual(CONTEXTO_SECTION_ORDER.filter((id) => id !== "rem"));
```

  (the `labels` list and `toHaveLength(9)` in the first test stay as they are);
- append at the end of the file:

```tsx
describe("ContextoView — Lembretes e avisos (TASK-032 R5)", () => {
  const withWire = (wire: ReturnType<typeof tenantWire>) =>
    reduce([
      ...STARTED,
      { type: "tenant_loaded", generation: 2, tenantId: TENANT_ID, wire },
      { type: "roster_loaded", rosterGeneration: 2, tenantId: TENANT_ID, rows: [row], configs: [professionalWire(PROF_A)] },
      { type: "professional_selected", id: PROF_A },
      { type: "professional_loaded", id: PROF_A, rosterGeneration: 2, wire: professionalWire(PROF_A) },
    ]);
  const anchors = (html: string) => [...html.matchAll(/<div id="([a-z]+)" class="contexto-anchor">/g)].map((m) => m[1]);

  it("older backend: no section at all", () => {
    const html = render(withWire(tenantWire()));
    expect(html).not.toContain("Lembretes e avisos");
    expect(html).not.toContain('<div id="rem"');
  });

  it("R7 backend (paid flag only): the section sits between Mensagens and Sinal via Pix, with the switch and no lead field", () => {
    const html = render(withWire(tenantWire({ paid_notices_auto_approved: false })));
    const ids = anchors(html);
    expect(ids).toEqual([...CONTEXTO_SECTION_ORDER]);
    expect(ids.indexOf("rem")).toBe(ids.indexOf("msg") + 1);
    expect(html).toContain('aria-label="Enviar avisos pagos sem perguntar"');
    expect(html).not.toContain("Lembrete extra");
  });

  it("backend exposing the lead too: the select shows the stored value", () => {
    const html = render(withWire(tenantWire({ paid_notices_auto_approved: true, reminder_extra_lead_minutes: 7200 })));
    expect(html).toContain('<option value="7200" selected="">5 dias antes</option>');
  });

  it("the controls are locked while a save is in flight", () => {
    const html = render({ ...withWire(tenantWire({ paid_notices_auto_approved: true, reminder_extra_lead_minutes: null })), saving: true });
    const section = /<section[^>]*aria-label="Lembretes e avisos"[\s\S]*?<\/section>/.exec(html)![0];
    expect(section).toMatch(/<select[^>]*disabled=""/);
    expect(section).toMatch(/role="switch"[^>]*disabled=""/);
  });
});
```

Append to `lib/contexto/__tests__/snapshot.test.ts`:

```ts
describe("dirtySections — rem (TASK-032 R5)", () => {
  const base = () =>
    tenantSlicesFromWire(tenantWire({ reminders_v2_enabled: true, reminder_extra_lead_minutes: null, paid_notices_auto_approved: false }));

  it("changing the lead or the paid switch marks only 'rem'", () => {
    const a = base();
    expect(dirtySections({ tenant: { ...base(), reminders: { ...a.reminders, extraLeadMinutes: 7200 } }, professional: null }, { tenant: a, professional: null })).toEqual(["rem"]);
    expect(dirtySections({ tenant: { ...base(), reminders: { ...a.reminders, paidAutoApproved: true } }, professional: null }, { tenant: a, professional: null })).toEqual(["rem"]);
  });

  it("the backend-derived flags never make the section dirty", () => {
    const a = base();
    const b = { ...base(), reminders: { ...a.reminders, v2Enabled: !a.reminders.v2Enabled, supported: !a.reminders.supported, paidSupported: !a.reminders.paidSupported } };
    expect(dirtySections({ tenant: b, professional: null }, { tenant: a, professional: null })).toEqual([]);
  });
});
```

(`dirtySections` and `tenantSlicesFromWire` are already imported there; add `import { tenantWire } from "./fixtures";`.)

In `lib/contexto/__tests__/toc.test.ts`, in `it("uses the section titles the manager sees on screen", …)` add `expect(TOC_LABELS.rem).toBe("Lembretes e avisos");` (the "label for every section id" test keeps passing because `TOC_LABELS` gains `rem` together with the order).

Append to `components/contexto/__tests__/post-consult-section.test.tsx` (inside the file's first `describe`, using its `render` helper):

```tsx
  it("says when the post-consult message is sent and what happens when it is empty (TASK-032 R5)", () => {
    const html = render();
    expect(html).toContain(POST_CONSULT_TIP);
    expect(POST_CONSULT_TIP).toContain("Compareceu");
    expect(POST_CONSULT_TIP).toContain("Em branco");
  });
```

and add `POST_CONSULT_TIP` to its import from `"../PostConsultSection"`.

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run components/contexto lib/contexto`
Expected: FAIL (`../ReminderSection` missing; order mismatch; `POST_CONSULT_TIP` missing).

- [ ] **Step 3: `SectionId`, dirty detection, TOC label**

`lib/contexto/snapshot.ts`: change `export type SectionId = "ctx" | "msg" | "pos" | "pix" | "facts" | "prof" | "srv" | "disp" | "gcal";` to

```ts
export type SectionId = "ctx" | "msg" | "rem" | "pos" | "pix" | "facts" | "prof" | "srv" | "disp" | "gcal";
```

import `type Reminders` from `./types` (if Task 10 did not already), add next to `comparablePix`:

```ts
// Only the lead and the paid switch are editable; the support flags and v2 are backend-derived.
function comparableReminders(r: Reminders): unknown {
  return { extraLeadMinutes: r.extraLeadMinutes, paidAutoApproved: r.paidAutoApproved };
}
```

and in `dirtySections`, after the `pix` block:

```ts
  if (!same(comparableReminders(current.tenant.reminders), comparableReminders(baseline.tenant.reminders))) {
    sections.push("rem");
  }
```

`lib/contexto/toc.ts`: add `rem: "Lembretes e avisos",` to `TOC_LABELS` between `msg` and `pix`.

- [ ] **Step 4: `components/contexto/ReminderSection.tsx`**

```tsx
// ReminderSection — "Lembretes e avisos" (TASK-032 R5; spec 2026-10-09 §2 and the
// 2026-10-03 spec §4.5). Two independent halves, each shown only when the backend
// exposes its key, so no control ever saves nowhere:
//  - "Avisos pagos do WhatsApp" (R7 `paid_notices_auto_approved`): the agenda's
//    "Não perguntar novamente" sets it; here the clinic turns it back off (or on).
//  - "Lembrete extra" (`reminder_extra_lead_minutes`): the clinic's ONE configurable
//    reminder; the 1-day and 1-hour reminders are fixed product behaviour.
//
// Hook-free (like PixSection) so tests can walk its element tree. Honest about the
// reminders switch: `reminders_v2_enabled` is the owner's per-clinic interruptor and
// is READ-ONLY here — the badge says in words whether it is on.

import { Panel } from "../primitives/Panel";
import { Select } from "../primitives/Select";
import { Badge } from "../primitives/Badge";
import { Toggle } from "../primitives/Toggle";
import { leadOptions, parseLead } from "@/lib/contexto/reminders";
import type { Reminders } from "@/lib/contexto/types";
import "./contexto.css";

export type ReminderSectionProps = {
  v: Reminders;
  setLead: (minutes: number | null) => void;
  setPaid: (on: boolean) => void;
  /** Locked while the screen cannot save (loading, error, saving). */
  readOnly?: boolean;
};

export function ReminderSection({ v, setLead, setPaid, readOnly }: ReminderSectionProps) {
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
            extra, mais cedo.
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
            value={String(v.extraLeadMinutes ?? "off")}
            onChange={(next) => setLead(parseLead(next))}
            label="Lembrete extra"
            options={leadOptions(v.extraLeadMinutes)}
            disabled={readOnly}
          />
          <p className="contexto-field-tip">
            Enviado antes dos lembretes fixos de 1 dia e 1 hora. Fica desligado até você escolher um prazo.
          </p>
        </div>
      )}
    </Panel>
  );
}
```

`contexto.css` has no sub-heading rule yet (checked on `c2d3ff7`); append to `components/contexto/contexto.css`:

```css
/* Sub-heading inside a section (TASK-032 R5 "Lembretes e avisos"). */
.contexto-subtitle { margin: 0; font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
```



- [ ] **Step 5: Wire `ContextoView.tsx`**

Import `import { ReminderSection } from "./ReminderSection";` and `import type { Reminders } from "@/lib/contexto/types";`. In `CONTEXTO_SECTION_ORDER` insert `"rem",` between `"msg",` and `"pix",`. After the constant add:

```tsx
/** "Lembretes e avisos" exists only when the backend exposed at least one of its keys. */
export function remVisible(r: Reminders): boolean {
  return r.supported || r.paidSupported;
}
```

In the `sections` record add after `msg`:

```tsx
    rem: (
      <ReminderSection
        v={tenant.reminders}
        setLead={(minutes) => dispatch({ type: "set_reminder_lead", extraLeadMinutes: minutes })}
        setPaid={(on) => dispatch({ type: "set_paid_notices", value: on })}
        readOnly={tenantReadOnly}
      />
    ),
```

Right before the `return (` of the view, add `const visibleOrder = CONTEXTO_SECTION_ORDER.filter((id) => id !== "rem" || remVisible(tenant.reminders));` and use it in the two places that iterate the order: `<SectionToc order={visibleOrder} …/>` and `{visibleOrder.map((id) => (`.

- [ ] **Step 6: The post-consult hint (`components/contexto/PostConsultSection.tsx`)**

Add above the component:

```tsx
// TASK-032 R7: "Compareceu" in the agenda now sends this message once, at once.
export const POST_CONSULT_TIP =
  "Enviada ao paciente quando a clínica marca “Compareceu” na agenda, uma vez por consulta, exatamente como está escrita. Em branco, a secretarIA envia uma mensagem padrão perguntando como foi a consulta.";
```

and replace the first tip paragraph (the one starting "Mensagem pronta que a secretarIA usa com o paciente logo depois da consulta") with:

```tsx
      <p className="contexto-field-tip">{POST_CONSULT_TIP}</p>
```

- [ ] **Step 7: Run tests, type check, build**

Run: `npx vitest run components/contexto lib/contexto` — Expected: PASS.
Run: `npm run typecheck` — Expected: no errors.
Run: `npm run build` — Expected: success.

- [ ] **Step 8: Commit**

```bash
git add components/contexto/ReminderSection.tsx components/contexto/contexto.css components/contexto/ContextoView.tsx components/contexto/PostConsultSection.tsx lib/contexto/snapshot.ts lib/contexto/toc.ts components/contexto/__tests__/reminder-section.test.tsx components/contexto/__tests__/contexto-screen.test.tsx components/contexto/__tests__/post-consult-section.test.tsx lib/contexto/__tests__/snapshot.test.ts lib/contexto/__tests__/toc.test.ts
git commit -m "feat(contexto): Lembretes e avisos section (paid-notice switch, extra reminder) and the post-consult hint (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 12: Checkpoint doc and final validation

**Files:**
- Create: `docs/CHECKPOINT_brain_message_lembretes_agenda.md`
- Modify: `docs/CHECKPOINT_brain_message_agenda.md` (one pointer line)

- [ ] **Step 1: Full validation**

Run, from the worktree (PowerShell), in this order:
`npm run typecheck` — Expected: no errors.
`npm test` — Expected: all suites green. If a failure looks unrelated, run the same test on the base commit (`git stash` is not allowed here: use `git worktree add` of `main`, or `git diff main --stat` to confirm the failing file is untouched) and report **pre-existing** vs **new** separately; do not "fix" unrelated reds.
`npm run build` — Expected: static export succeeds.
`git grep -n "rescheduleAppointment\|RescheduleModal\|STATUS_ACTIONS\|create_not_allowed" -- lib components app` — Expected: nothing.
`git grep -n "localStorage\|sessionStorage" -- lib/agenda components/agenda` — Expected: nothing (the "Só os meus" choice lives in `?meus=1`).
Never `npm run lint`.

- [ ] **Step 2: Write `docs/CHECKPOINT_brain_message_lembretes_agenda.md`**

```markdown
# CHECKPOINT — Lembretes e ações da clínica na agenda (TASK-032 R5, TASK-045)

**Estado:** implementado e testado localmente; commitado na branch `task/TASK-045-lembretes-r5-front`; NÃO mesclado, NÃO pushado, NÃO deployado. Depende do brain-api com `agenda_scope` e do backend R7 (TASK-044) mesclados e deployados, nesta ordem: migração da secretarIA → brain-api → API + worker da secretarIA → este front.

## O que entrou onde
- `lib/agenda/confirmation.ts` — mapeamento do read (status, `confirmation_count`, `display_state`, `attention`, `reminders`, inclusive `staff_confirm`/`staff_edit`), `confirmationLook` (neutro / ✓ / ✓✓ / vermelho; status encerrado usa a cor do status), legenda, linha do tempo, `applyStaffConfirmation`.
- `lib/agenda/actions.ts` — matriz de ações pelo relógio (antes do início / depois do início / encerrada / sem relógio).
- `lib/agenda/notice.ts` — `patient_notice` → frase e tom (`actionToast`), link wa.me seguro, decisão do aviso pago (`decidePaidNotice`).
- `lib/agenda/release.ts`, `lib/agenda/edit.ts` — regras de Liberar (retenção do Pix) e de Editar/Remarcar (só o que mudou).
- `lib/agenda/secretaria-hub-agenda.ts` — PATCH status com `notify_outside_window`, `release`, `message`, `edit`, flag da clínica; `reschedule` saiu da agenda.
- `lib/agenda/screen.ts` — `paidAuto`, modais `paid_notice`/`release`/`message`/`edit`, guardas 409, virada ao vivo, alias `?consulta=`; quem está olhando (`viewer`, lido antes do primeiro intervalo e de novo ao trocar de clínica), "Só os meus" (`mine`, `?meus=1`, `setMine`), médico travado na criação, 404 "não encontrada na sua agenda".
- `lib/agenda/viewer.ts` — `AgendaViewer` a partir do `GET /calendar/viewer` (R7), `lockedProfessional`, `canCreate`, `wantsOwnOnly`.
- `lib/agenda/hub-mapping.ts::mapAppointmentFacts` — médico, serviço, atendido, telefone de contato e canal do paciente vindos da agenda (R7); `modal-forms.ts::reachFor` usa o canal (Portal nunca é perguntado e é avisado no chat).
- `components/agenda/` — `Drawer` (selo, linha do tempo, ações por horário, profissional), `PaidNoticeSheet`, `ReleaseCard`, `MessageModal`, `EditModal` (pré-preenchido; médico travado para o médico restrito), `CancelCard` com "Não perguntar novamente", toast com link wa.me, tons na grade, seletor "Todos / Só os meus", `Modals.tsx` com o médico travado em Nova consulta/Bloquear.
- `app/styles/tokens.css` — `--st-neutral-*`, `--st-confirm2-*`, `--st-attn-*` nos dois temas.
- `lib/contexto/` + `components/contexto/ReminderSection.tsx` — seção "Lembretes e avisos" (chave de avisos pagos; estado dos lembretes só leitura; lembrete extra 1500..20160 min, valor antigo fora da faixa não é reenviado); dica de quando a mensagem pós-consulta é enviada.

## Regras que os testes fixam
"Faltou" nunca antes do início; depois do início só Compareceu/Faltou/Editar; encerrada sem botões; status encerrado nunca recolorido por `display_state`; aviso pago perguntado só para WhatsApp fora das 24 h sem autorização; Portal nunca perguntado; "Não perguntar novamente" gravado antes da ação e falha não envia nada; toda ação mostra o que aconteceu com o aviso; Editar envia só o que mudou em relação ao que já estava preenchido; liberar com sinal pago exige o aviso marcado; vermelho→verde ao confirmar; médico restrito sem seletor, criando só na própria agenda e com o médico travado no Editar; gestor-médico alterna "Todos / Só os meus" (`?meus=1`); recepção sem seletor; consulta oculta = "não encontrada na sua agenda"; sem animação nova; backend antigo renderiza como antes.

## Contrato
Copiado de `secretarIA/docs/superpowers/plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md` ("Produces for R5") em 2026-10-09; ajustes ficam em `secretaria-hub-agenda.ts`, `notice.ts` e `edit.ts`.

## Pendências
- Console de conversas por papel (médico vê só os próprios pacientes; gestor alterna; secretária vê tudo): plano separado, ainda não escrito (spec 2026-10-09 §5.D).
- Recepção/gestão ainda marca "Nova consulta" sem escolher o médico (como antes); o backend já aceita `professional_id` — melhoria futura.
- Consulta de paciente sem cadastro (só telefone) ou de backend sem o canal nunca é perguntada sobre aviso pago; a resposta do backend diz se o aviso saiu.
- Atualização automática da agenda quando o paciente confirma pelo WhatsApp: fora do escopo (só relê ao navegar/agir).
- Prova ao vivo (agent-browser, dois temas, teclado, leitor de tela) só depois do deploy do R7 e do pedido do dono.
```

- [ ] **Step 3: Pointer line**

Append to `docs/CHECKPOINT_brain_message_agenda.md` one line: `- Confirmação ✓/✓✓, ações da consulta por horário, avisos ao paciente, Editar/Remarcar e "Lembretes e avisos": ver CHECKPOINT_brain_message_lembretes_agenda.md (TASK-032 R5).`

- [ ] **Step 4: Manual check the executor can do before handing over (no deploy)**

`next dev` runs in mock mode with no real session (the agenda renders its signed-out state), so the visual states cannot be seen locally without a backend. Do **not** invent demo data in the product code. The two-theme / keyboard / screen-reader pass is part of the post-deploy live proof below.

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_brain_message_lembretes_agenda.md docs/CHECKPOINT_brain_message_agenda.md
git commit -m "docs(agenda): checkpoint for reminders and clinic actions on the agenda (TASK-032 R5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Deploy and live proof (not part of the tasks)

**Deploy is never part of this plan.** The front deploys only **after brain-api (`agenda_scope`) and R7 (and R1–R4/R6, already live) are deployed**, in this order: secretarIA migration (`alembic upgrade head`) → brain-api → `secretaria_api` and `secretaria-worker` together (`GET /build` `deploy_parity=match`) → this front — and **only after the owner asks for it**. Clinic switch `reminders_v2_enabled` on only for the test clinic first. Wrong-order symptom: a manager-doctor sees only his own appointments and no switch (R7's rollout rule while brain-api lacks `agenda_scope`).

Live proof, after that, with `agent-browser` (skill) against the real console on the test clinic, in light **and** dark theme:
1. A booked appointment with no answer shows the neutral look; after a reminder deadline passes it turns red with "Sem confirmação" and the legend; a cancelled / attended / no-show one keeps its own colour.
2. Drawer before the start: Liberar horário, Confirmar, Compareceu, Editar/Remarcar, Cancelar consulta, Enviar mensagem; after the start: Compareceu, Faltou, Editar/Remarcar; closed: none. Keyboard-only: Tab reaches the actions in order, Enter opens each sheet, Escape closes.
3. Confirmar on a WhatsApp patient inside 24 h: no question, green at once, toast "O paciente foi avisado pelo WhatsApp"; outside 24 h: the "Enviar mesmo assim?" sheet with the cost; "Não perguntar novamente" makes the next one silent and Contexto's switch shows it on; turning it off brings the question back. A Portal patient: never asked, toast "O aviso ficou na conversa do paciente no Portal…".
3b. Who is looking, with three test users of the test clinic: the secretary sees every doctor and no switch; a manager who is also a doctor sees "Todos / Só os meus", "Só os meus" survives a reload (`?meus=1`); a doctor limited to his own agenda sees only his appointments, no switch, "Nova consulta"/"Bloquear" show his name locked and the new row lands on his agenda, a link `?consulta=` to a colleague's appointment says "não foi encontrada na sua agenda".
4. Compareceu sends the clinic's post-consult text (or the default) once; Faltou before the start is not offered.
5. Editar/Remarcar: changing only the convênio sends only that; a taken time offers "Salvar mesmo assim (encaixe)"; the patient receives what changed with the three buttons.
6. Liberar horário on a Pix-paid appointment keeps the retention checkbox; the clinic's warning e-mail link (`?consulta=…&data=…`) opens that appointment's drawer.
7. Reduced motion (browser emulation) shows no animation on the new elements; a screen reader reads the badge text, the action names, the switches and the timeline list.

---

## Self-Review

**Spec coverage (2026-10-09).** §1 action matrix and "Faltou not before start": Task 3 (`actionPhase`/`drawerActions`, drawer markup), Task 5 (409 `no_show_before_start`). One Editar/Remarcar with date/time, service, doctor, convênio, attendee, phone: Tasks 8–9 (pre-filled from Task 1's facts). Cancel keeps the doctor's choices: Task 6 (card unchanged except the flag/remember and the Portal copy). §2 every action notifies, Portal never asked (by `patient_channel`), paid question with cost + "Enviar mesmo assim" + "Não perguntar novamente", flag back off in Configurações: Tasks 4–6, 9, 11. Compareceu post-consult text configurable with default: Task 11 (existing field, new hint). §3 contract: Task 4 (calls, vocabulary), Task 5 (PATCH shape, guards, cancel notice). Colour from status: Tasks 1–3. §5.A who sees the agenda (secretary all, manager-doctor toggle, other doctor only his own, hidden = not found) and the owner's "create only on his own agenda": Task 7 (+ the locked doctor in Tasks 8–9). §5.B extra reminder (read-only state, 1500..20160): Tasks 10–11. §5.C the agenda carries the Editar data: Task 1 (mapping), Tasks 8–9 (pre-fill), Task 3 (doctor in the drawer). §5.D console of conversations: out of scope (separate plan, recorded in Task 12's pendências). **2026-10-03 spec** §4.1 looks, §4.4 legend/timeline/release/message, §4.5 extra lead: Tasks 1–2, 3, 5–6, 10–11. Criterion 8 (green at once) and 9 (release respects Pix): Tasks 5–6. Out of scope: automatic release, push, polling.

**Review Focus → test.** 1 clock gate: Task 3 (`actions.test.ts`, drawer "before/after/without a clock"), Task 5 (`no_show_before_start`). 2 terminal colour: Task 1 (mapping test), Task 2 (grid test), Task 3 (drawer "terminal statuses keep their own look"). 3 paid question: Task 4 (`decidePaidNotice` by channel), Task 5 ("status actions — the paid-notice question", Portal and no-channel cases, release/message/cancel remember), Task 6 (sheet, cards, `reachFor`), Task 9 (edit notice). 4 honest notice: Task 4 (`actionToast`), Task 5 (toasts), Task 6 (toast link markup). 5 edit: Task 8 (`buildEditPayload` against pre-filled values, legacy phone, 403/404, controller), Task 9 (button states, locked doctor). 6 Pix gate: Tasks 4, 5, 6. 7 live flip: Task 5. 8 double clicks: Task 5 (status, release), Task 8 (edit). 9 old backend: Tasks 1, 2, 3, 5 (cancel fallback), 7 (viewer read failure), 10, 11. 10 names/motion/themes: Tasks 2, 3, 6, 7, 9, 11. 11 scoped doctor: Task 7 ("restricted doctor" controller + view tests, deep link, 404s), Tasks 8–9 (locked doctor, 403). 12 manager toggle: Task 7 ("manager-doctor", reload with `?meus=1`). 13 secretary: Task 7 ("a secretary's ?meus=1 never reaches the server"). 14 viewer per clinic: Task 7 ("after a clinic switch the viewer is read again"). 15 legacy lead: Task 10 ("a legacy lead outside R7's 1500..20160 is never re-sent").

**Placeholders.** None: every code step carries the code. R7's contract (first and second round) was read from its plan on 2026-10-09; if R7 changes before execution, only the constants/types at the top of `secretaria-hub-agenda.ts`, `notice.ts`, `viewer.ts` and `edit.ts` move.

**Type consistency.** `ConfirmationLook` fields are used identically in Tasks 2–3. `DrawerActionId`/`STATUS_OF`/`ACTION_LABEL` (Task 3) feed the drawer only. `PaidNoticeAsk` (Task 4) is the `ask` of the `paid_notice` modal (Task 5) and of `PaidNoticeSheetViewProps` (Task 6). `ReleaseConfirm = { acknowledged, optIn, remember }` matches `ReleaseCardView.onConfirm`, `AgendaViewProps.onConfirmRelease` and `confirmRelease`. `sendMessage(text, notify, remember)` matches `MessageModalView.onSend` and `AgendaViewProps.onSendMessage`. `confirmCancel(body, remember)` matches `CancelCardView.onConfirm` and `AgendaViewProps.onConfirmCancel`. `choosePaidNotice({ send, remember })` matches the sheet and the view prop. `EditConfirm = { form, optIn, remember }` matches `EditModalView.onConfirm`, `AgendaViewProps.onConfirmEdit` and `confirmEdit`. `updateAppointmentStatus(s, id, { status, notify_outside_window })` is the same in the client (Task 4), `AgendaHub` and every fake. `Reminders` fields (`supported`, `v2Enabled`, `extraLeadMinutes`, `paidSupported`, `paidAutoApproved`) match across Tasks 10–11; the actions are `set_reminder_lead` and `set_paid_notices` in both. `AgendaViewer = { scope, professionalId, professionalName, canFilterOwn }` (Task 7) is `AgendaState.viewer`, the input of `lockedProfessional`/`canCreate`/`wantsOwnOnly`, and what `AgendaView` passes (as `locked`) to `NewApptModal`/`BlockModal`/`EditModal.lockedProfessional: { id, name } | null` (Tasks 7, 9). `decidePaidNotice(preview, autoApproved, channel)` (Task 4) is called only by `paidNoticeDecision(s, appt)` (Task 5). `editFormIssues(appt, form, todayKey)` (Task 8) is the only signature, used by `EditModalView` (Task 9). `listCalendarEvents(s, startIso, endIso, mine?)` matches the client, `AgendaHub` and `loadRange` (Task 7).
