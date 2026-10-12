# Lembretes R8 — "Meus pacientes" no console e escolha do médico na agenda Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) In the Brain-Message console every person at the clinic keeps seeing every conversation; a viewer who is a doctor (has a secretarIA professional of his own) gets one extra filter chip, **"Meus pacientes"**, that narrows the list to patients who have or had an appointment with him. (2) In the agenda, "Nova consulta" and "Bloquear horário" let a clinic-wide viewer pick the doctor from the clinic's active professionals (or "Sem profissional definido"); a restricted doctor stays locked to himself (R5).

**Architecture:** The staff conversation list is built in **secretarIA** (`api/hub/conversations.py::list_conversations`, `GET /tenants/me/conversations`), called by the console **directly with the hub token** (`Brain-Message-Frontend/lib/real/console-api.real.ts::secretariaList` → `hubFetch(s, "/tenants/me/conversations")`); brain-api only mints/introspects the hub token and already tells secretarIA the viewer's `professional_id` (R7). So the filter is one optional query flag on that route — `?mine=true` — resolved against `api/hub/deps.py::get_agenda_viewer` and a correlated `EXISTS` over `appointments`; **no brain-api change**. The console learns "am I a doctor?" from the R7 route `GET /tenants/me/calendar/viewer` (`professional_id`), fetches the id set of `?mine=true` only while the chip is on, and filters the list it already has. The agenda picker reuses the professional list the R5 Editar/Remarcar sheet already loads (`AgendaHub.getProfessionals` = secretarIA `GET /tenants/me/professionals`), the same rows R7's `_creation_professional` validates `professional_id` against.

**Tech Stack:** secretarIA — Python 3.12, FastAPI, SQLAlchemy 2 async, pytest + pytest-asyncio (in-memory SQLite). Brain-Message-Frontend — Next.js 15 static export, React 19, TypeScript, vitest (node env, `renderToStaticMarkup`, no jsdom).

**Spec:** `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.D (console — replaced 2026-10-09 by this round's decision) and §5.E (doctor picker). Earlier plans this builds on: `docs/superpowers/plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md` (R7 — viewer, `_creation_professional`), `docs/superpowers/plans/2026-10-03-lembretes-r5-front-agenda.md` (R5 — Task 7, viewer/locked doctor in the sheets).

**Order and deploy:** Part A (secretarIA) then Part B (Brain-Message-Frontend). Part B builds on **R5 merged into Brain-Message-Frontend `main`** — do not start it before that merge. Deploy order (no task deploys anything): `secretaria_api` (the route changed; the worker is untouched, deploy it too only to keep `GET /build` `deploy_parity=match`) → Brain-Message-Frontend. No migration. A front deployed before the API: the chip's id fetch gets the old full list (FastAPI ignores the unknown `mine` query) — "Meus pacientes" would show everything; therefore **backend first**.

**Execution context (worktrees, task id):** TASK-046 (next free id after TASK-045). Register both worktrees in `C:\TECH\BRAIN\tasks\TASK-046\TASK.md` (copy of `tasks/TEMPLATE.md`; `Deployment: NOT AUTHORIZED`).
- **Part A:** `C:\TECH\BRAIN-worktrees\TASK-046\secretarIA`, branch `task/TASK-046-meus-pacientes-api`, created from the commit that contains R7 (`cb526d5`, branch `task/TASK-044-main-integration`, or `main` once it contains it). Precondition: `git merge-base --is-ancestor cb526d5 HEAD` succeeds and `grep -n "def get_agenda_viewer" src/secretaria/api/hub/deps.py` prints a line.
- **Part B:** `C:\TECH\BRAIN-worktrees\TASK-046\Brain-Message-Frontend`, branch `task/TASK-046-meus-pacientes-front`, created from Brain-Message-Frontend `main` **after** R5 (TASK-045) is merged. Precondition: `grep -n "export function lockedProfessional" lib/agenda/viewer.ts`, `grep -n "getProfessionals" lib/agenda/screen.ts` and `grep -n "function LockedProfessional" components/agenda/Modals.tsx` each print a line. Code below quotes R5 as of `task/TASK-045-lembretes-r5-front` (2026-10-09, uncommitted tip of d54f3a6); if the merged R5 renamed a symbol, keep the behaviour and use the merged name.
- Never run in the main checkouts (`C:\TECH\BRAIN\secretarIA`, `C:\TECH\BRAIN\Brain-Message-Frontend`).

## Global Constraints

- **Filter, never a permission (spec §5.D).** `GET /tenants/me/conversations` without `mine` (or `mine=false`) returns exactly today's list to **every** viewer, a restricted (`"own"`) doctor included. No conversation route other than the list changes; `GET/POST /{id}/…` stay unscoped.
- **"Meus pacientes" rule (decided here, spec silent):** a conversation is listed when its `Patient` has at least one `Appointment` of the **same tenant** with `professional_id = viewer.professional_id` and status in `SCHEDULED | CONFIRMED | RESCHEDULED | ATTENDED | NO_SHOW` (named constant `DOCTOR_PATIENT_STATUSES`; `CANCELLED` excluded). Blocks (no patient) never count. A conversation appears once however many appointments match. Ordering stays newest activity first.
- `mine=true` for a viewer without a professional of his own (secretary, unlinked user) → **422** `{"detail": {"code": "no_own_agenda", "message": "Seu usuário não está ligado a um profissional da clínica."}}` (same code/message as R7's `GET /calendar/events?mine=true`).
- The chip is shown only when `GET /tenants/me/calendar/viewer` returns a non-empty `professional_id`; any failure of that read = no chip (never an error screen). PreCheck threads leave the list while the chip is on.
- **Doctor picker (spec §5.E):** clinic-wide viewer → `<Select label="Profissional">` with "Escolha o profissional" (empty, blocks submit), the clinic's **active** professionals sorted by name (pt-BR), and "Sem profissional definido" (sends no `professional_id`; R7 stores `NULL`). Initial value: the viewer's own professional when he is in the list; else the only professional when there is exactly one; else none picked (`""`); list empty or failed → "Sem profissional definido". Restricted doctor → R5's locked line, no list, no request for the list; the controller sends his id whatever the form carries.
- R7 refusals on create are shown in the sheet with the backend's sentence: 403 `professional_not_allowed`, 422 `no_own_agenda` (R5), and new here 422 `unknown_professional` (doctor deactivated between load and submit → sentence shown, list re-read).
- UI copy in Portuguese; code, comments, test names in English. No new dependency. No ESLint (`npm run lint` must not be run). No Tailwind; reuse existing classes (`agm-hint`, `chip`).
- **secretarIA tests** run from Git Bash with the base interpreter (App Control blocks the venv's `python.exe`):
  `PYTHONPATH="src;.;.venv/Lib/site-packages" BOT_ALLOWLIST_WA_IDS="" /c/Users/mateu/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest <file> -q`
  (below abbreviated `PYT <file>`; paste the full form if your shell keeps no aliases).
- secretarIA lint: never `ruff format .`. Created files: `uvx ruff format <file>` then `uvx ruff check --fix <file>`. Modified files: `uvx ruff check <file>`, fix only lines you touched. Files are CRLF: after each task `git diff --stat` must show only the touched lines (a whole-file diff = line endings flipped → `git checkout -- <file>` and redo). After every Write/Edit: `LC_ALL=C.UTF-8 grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]' <files>` prints nothing.
- **Front validation:** `npm test` (vitest run), `npm run typecheck` (or `.\node_modules\.bin\tsc.cmd --noEmit`), `npm run build`. Single file: `npx vitest run <path>`.
- Commits: `git add <explicit paths>` (never `-A`); every message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Never push, merge to main, deploy, or touch a real database.**

## Review Focus

1. **A restricted doctor opening the console without the chip** must still see every conversation of the clinic (the new dependency on the viewer must not turn into scoping). Pinned in Task 1 (`test_without_mine_every_viewer_sees_the_whole_clinic`, `own` and `clinic`).
2. **A patient whose only appointment with the doctor was cancelled**, or who has a cancelled one with him and a live one with another doctor, is not "his" — and one with both cancelled and attended appointments with him appears once. Pinned in Task 1 (status parametrisation + `test_one_row_per_conversation_and_cancelled_only_is_not_mine`).
3. **Manager-doctor whose `professional_id` came back but the chip's id fetch fails** (backend refusing 422/500): the list must show an error with "Tentar novamente", never an empty "Nenhuma conversa" that reads as "you have no patients". Pinned in Task 3 (`fetchMyPatientConversationIds` rethrows the 422; `applyConversationFilter` with `myPatients` and no id set returns `[]`) and Task 4 (`ChatScreen` keeps `visible` null until ids arrive and routes the failure through the existing `fail`).
4. **Receptionist in a clinic with several doctors clicking "Agendar" without choosing** must not create a doctor-less appointment silently. Pinned in Task 5 (`initialProChoice` → `""`, `proChoiceReady("") === false`) and Task 6 (BlockModal renders disabled until a choice).
5. **A doctor deactivated between opening the sheet and saving**: the backend's 422 `unknown_professional` sentence shows in the sheet and the list is re-read (the stale doctor disappears). Pinned in Task 6 (`screen.test.ts` "unknown_professional").

---

## Produces for Part B — exact wire shapes

All under the secretarIA hub base, hub bearer token.

**`GET /tenants/me/conversations`** — unchanged without the flag. New optional query `mine` (`true`/`false`, default `false`; FastAPI bool parsing, so `1`/`0`/`yes`/`no` also parse; garbage → FastAPI's standard 422 `{"detail":[...]}`).
- `mine=true`, viewer has a professional → `200` `ConversationRead[]` (same keys as today: `id, patient_wa_id, patient_name, handover_state, last_message_at`), only the "Meus pacientes" rows, newest activity first (`last_message_at` desc, nulls last).
- `mine=true`, viewer without a professional → `422` `{"detail":{"code":"no_own_agenda","message":"Seu usuário não está ligado a um profissional da clínica."}}`.

**`GET /tenants/me/calendar/viewer`** (R7, unchanged) → `{"agenda_scope":"clinic"|"own","professional_id":str|null,"professional_name":str|null,"can_filter_own":bool}`. The chip exists iff `professional_id` is a non-empty string.

**`GET /tenants/me/professionals`** (unchanged) → `ProfessionalWire[]`; the picker reads `id`, `name`, `is_active`.

**`POST /tenants/me/calendar/appointments`** and **`POST /tenants/me/calendar/blocks`** (R7, unchanged; verified in `api/hub/calendar.py::_creation_professional` and pinned by `tests/test_hub_agenda_scope_actions.py`): optional `professional_id: UUID | null`.
- Clinic-wide viewer: absent/`null` → stored `NULL` (201); an **active** professional of this clinic → stored (201); inactive / other clinic / unknown → `422 {"detail":{"code":"unknown_professional","message":"Este profissional não pertence a esta clínica."}}`.
- Restricted viewer: absent or his own id → his own (201); another id → `403 {"detail":{"code":"professional_not_allowed","message":"Você só pode marcar consultas e bloqueios na sua própria agenda."}}`; no professional of his own → `422 {"detail":{"code":"no_own_agenda",…}}`. All refusals happen before Google is touched.

## Decisions taken where the spec is silent

- **Which appointments make a patient "mine":** `SCHEDULED`, `CONFIRMED`, `RESCHEDULED` (has — still happening), `ATTENDED` (had), `NO_SHOW` (had: the slot was his patient's; a doctor wants to follow up a missed patient). **`CANCELLED` does not count**: it neither happened nor will, and counting it would list patients who cancelled and rebooked elsewhere. This reads the owner's "já tiveram, ou têm, consulta" literally. The filter is not a permission, so an excluded patient is always one tap away in "Todas".
- **Match by patient, not by conversation:** `Appointment.patient_id = Conversation.patient_id` (a patient's appointment booked from the hub with `patient_id` set, or by the bot, counts for every conversation of that patient). Phone-only hub bookings (`patient_id` NULL) and Google-only events cannot be matched and do not count. An appointment booked for a third party belongs to the booker's conversation — the booker is the one talking.
- **Who sees the chip:** anyone whose viewer has a `professional_id` (restricted doctor and manager-doctor alike), not only `can_filter_own`.
- **Chip semantics:** an independent toggle that combines with "Todas / Aguardando / Não lidas / Minhas" and with the search, not a fifth exclusive scope (a doctor asking "my patients waiting for an answer" is the expected use). Not persisted across reloads (YAGNI).
- **PreCheck threads** have no appointment link: hidden while the chip is on.
- **Picker default:** must choose when the clinic has 2+ active doctors and the viewer is not one of them — a doctor-less booking is invisible to restricted doctors (R7 §5.A), so it must be deliberate.
- **Picker source:** `AgendaHub.getProfessionals` (secretarIA `GET /tenants/me/professionals`), already wired in the agenda for Editar/Remarcar: same ids and same `is_active` the server validates; brain-api's `getDoctorProfessionals` is a different (user-link) list and would need an id mapping.

## File Structure

**Part A — secretarIA (`C:\TECH\BRAIN-worktrees\TASK-046\secretarIA`)**
- Modify `src/secretaria/models/appointment.py` — add `DOCTOR_PATIENT_STATUSES` next to the other shared status tuples.
- Create `src/secretaria/services/doctor_patients.py` — `is_patient_of(professional_id) -> ColumnElement[bool]` (correlated EXISTS; the one definition of "my patient").
- Modify `src/secretaria/api/hub/conversations.py` — `list_conversations` takes `mine` + the viewer.
- Create `tests/test_hub_conversations_my_patients.py`.
- Create `docs/CHECKPOINT_conversas_meus_pacientes.md`; modify `CLAUDE.md` (one pointer line).

**Part B — Brain-Message-Frontend (`C:\TECH\BRAIN-worktrees\TASK-046\Brain-Message-Frontend`)**
- Create `lib/real/console-my-patients.ts` — `fetchOwnProfessionalId`, `fetchMyPatientConversationIds`.
- Modify `lib/types.ts` (`Me.ownProfessionalId?`, `ConversationFilter.myPatients?`), `lib/console-api.ts` (optional `listMyPatientConversationIds?`), `lib/real/console-api.real.ts` (me + method), `lib/conversations.ts` (filter by id set + copy).
- Modify `components/console/ConversationList.tsx` (chip + empty copy), `components/console/ChatScreen.tsx` (id fetch while on).
- Create `lib/agenda/create-professional.ts` — pure picker logic.
- Modify `lib/agenda/secretaria-hub-agenda.ts` (`UNKNOWN_PROFESSIONAL_CODE`), `lib/agenda/screen.ts` (load list on open, send choice, error), `components/agenda/Modals.tsx` (picker), `components/agenda/AgendaView.tsx` (props).
- Tests: create `lib/__tests__/console-my-patients.test.ts`, `components/console/__tests__/conversation-list.test.tsx`, `lib/agenda/__tests__/create-professional.test.ts`; modify `lib/__tests__/conversations.test.ts`, `lib/agenda/__tests__/screen.test.ts`, `components/agenda/__tests__/modals.test.tsx`, `components/agenda/__tests__/agenda-view.test.tsx`.
- Create `docs/CHECKPOINT_meus_pacientes_seletor_medico.md`; modify `CLAUDE.md` (one pointer line).

---

# Part A — secretarIA

### Task 1: `?mine=true` on the staff conversation list

**Files:**
- Modify: `src/secretaria/models/appointment.py` (after `TERMINAL_APPOINTMENT_STATUSES`)
- Create: `src/secretaria/services/doctor_patients.py`
- Modify: `src/secretaria/api/hub/conversations.py` (module docstring, imports, `list_conversations`)
- Test: `tests/test_hub_conversations_my_patients.py`

**Interfaces:**
- Consumes: `api/hub/deps.py::get_agenda_viewer -> AgendaViewer` (R7; tests override it — `tests/conftest.py` makes every test `CLINIC_WIDE` by default, `tests/_agenda_viewer.py::view_as(viewer)` replaces it per test); `services/agenda_visibility.py::AgendaViewer(scope: str, professional_id: UUID | None)`; `models/appointment.py::LIVE_APPOINTMENT_STATUSES`.
- Produces: `models.appointment.DOCTOR_PATIENT_STATUSES: tuple[AppointmentStatus, ...]`; `services.doctor_patients.is_patient_of(professional_id: UUID) -> ColumnElement[bool]`; wire `GET /tenants/me/conversations?mine=true` as in "Produces for Part B".

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_conversations_my_patients.py`:

```python
"""GET /tenants/me/conversations?mine=true - "Meus pacientes" (TASK-046 R8, spec 2026-10-09 §5.D).

A FILTER for a viewer who is a doctor, never a permission: without `mine` every viewer
(a doctor restricted to his own agenda included) gets the whole clinic, as before.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    HandoverState,
    Message,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.message import MessageDirection, MessageSender  # noqa: E402
from secretaria.services.agenda_visibility import AgendaViewer  # noqa: E402
from tests._agenda_viewer import view_as  # noqa: E402

ENDPOINT = "/tenants/me/conversations"
NO_OWN_AGENDA = {
    "detail": {
        "code": "no_own_agenda",
        "message": "Seu usuário não está ligado a um profissional da clínica.",
    }
}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _tenant(db, name: str) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name=name, phone_number_id=str(uuid4())[:12])
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    return await _tenant(db, "Clinic")


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _professional(db, tenant_id: UUID, name: str) -> UUID:
    async with db() as session:
        professional = Professional(id=uuid4(), tenant_id=tenant_id, name=name, is_active=True)
        session.add(professional)
        await session.commit()
        return professional.id


async def _conversation(
    db, tenant: Tenant, name: str, *, minutes_ago: int | None = None
) -> Conversation:
    """A patient + his conversation; `minutes_ago` adds one inbound message at that age."""
    async with db() as session:
        patient = Patient(tenant_id=tenant.id, wa_id=str(uuid4().int)[:13], name=name)
        session.add(patient)
        await session.flush()
        conv = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, handover_state=HandoverState.BOT_ACTIVE
        )
        session.add(conv)
        await session.flush()
        if minutes_ago is not None:
            session.add(
                Message(
                    conversation_id=conv.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="oi",
                    created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
                )
            )
        await session.commit()
        await session.refresh(conv)
        return conv


async def _appointment(
    db,
    tenant_id: UUID,
    conversation: Conversation,
    professional_id: UUID,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    *,
    days: int = 1,
) -> None:
    start = datetime.now(UTC) + timedelta(days=days)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=tenant_id,
                patient_id=conversation.patient_id,
                professional_id=professional_id,
                google_event_id=f"evt-{uuid4()}",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=status,
            )
        )
        await session.commit()


def _ids(response) -> list[str]:
    return [row["id"] for row in response.json()]


async def test_mine_lists_only_the_viewers_patients_newest_first(
    client: AsyncClient, db, tenant
) -> None:
    ana = await _professional(db, tenant.id, "Dra. Ana")
    beto = await _professional(db, tenant.id, "Dr. Beto")
    older = await _conversation(db, tenant, "Paciente Antigo", minutes_ago=30)
    newer = await _conversation(db, tenant, "Paciente Novo", minutes_ago=1)
    betos = await _conversation(db, tenant, "Paciente do Beto", minutes_ago=5)
    await _conversation(db, tenant, "Só conversou", minutes_ago=2)
    await _appointment(db, tenant.id, older, ana, AppointmentStatus.ATTENDED, days=-30)
    await _appointment(db, tenant.id, newer, ana)
    await _appointment(db, tenant.id, betos, beto)
    view_as(AgendaViewer("clinic", ana))  # a manager who is also a doctor

    response = await client.get(ENDPOINT, params={"mine": "true"})

    assert response.status_code == 200
    assert _ids(response) == [str(newer.id), str(older.id)]
    assert set(response.json()[0]) == {
        "id",
        "patient_wa_id",
        "patient_name",
        "handover_state",
        "last_message_at",
    }


@pytest.mark.parametrize(
    ("appointment_status", "listed"),
    [
        (AppointmentStatus.SCHEDULED, True),
        (AppointmentStatus.CONFIRMED, True),
        (AppointmentStatus.RESCHEDULED, True),
        (AppointmentStatus.ATTENDED, True),
        (AppointmentStatus.NO_SHOW, True),
        (AppointmentStatus.CANCELLED, False),
    ],
)
async def test_which_appointment_statuses_make_a_patient_mine(
    client: AsyncClient, db, tenant, appointment_status, listed
) -> None:
    ana = await _professional(db, tenant.id, "Dra. Ana")
    conv = await _conversation(db, tenant, "Paciente", minutes_ago=1)
    await _appointment(db, tenant.id, conv, ana, appointment_status)
    view_as(AgendaViewer("own", ana))  # a doctor restricted to his own agenda

    response = await client.get(ENDPOINT, params={"mine": "true"})

    assert response.status_code == 200
    assert _ids(response) == ([str(conv.id)] if listed else [])


async def test_one_row_per_conversation_and_cancelled_only_is_not_mine(
    client: AsyncClient, db, tenant
) -> None:
    ana = await _professional(db, tenant.id, "Dra. Ana")
    beto = await _professional(db, tenant.id, "Dr. Beto")
    twice = await _conversation(db, tenant, "Duas consultas", minutes_ago=1)
    moved = await _conversation(db, tenant, "Cancelou e foi pro Beto", minutes_ago=2)
    await _appointment(db, tenant.id, twice, ana, AppointmentStatus.CANCELLED, days=-10)
    await _appointment(db, tenant.id, twice, ana, AppointmentStatus.ATTENDED, days=-5)
    await _appointment(db, tenant.id, twice, ana, AppointmentStatus.SCHEDULED, days=5)
    await _appointment(db, tenant.id, moved, ana, AppointmentStatus.CANCELLED)
    await _appointment(db, tenant.id, moved, beto, AppointmentStatus.SCHEDULED)
    view_as(AgendaViewer("own", ana))

    response = await client.get(ENDPOINT, params={"mine": "true"})

    assert _ids(response) == [str(twice.id)]


@pytest.mark.parametrize("scope", ["own", "clinic"])
async def test_without_mine_every_viewer_sees_the_whole_clinic(
    client: AsyncClient, db, tenant, scope
) -> None:
    ana = await _professional(db, tenant.id, "Dra. Ana")
    beto = await _professional(db, tenant.id, "Dr. Beto")
    mine = await _conversation(db, tenant, "Da Ana", minutes_ago=1)
    theirs = await _conversation(db, tenant, "Do Beto", minutes_ago=2)
    nobodys = await _conversation(db, tenant, "Sem consulta", minutes_ago=3)
    await _appointment(db, tenant.id, mine, ana)
    await _appointment(db, tenant.id, theirs, beto)
    view_as(AgendaViewer(scope, ana))

    everything = await client.get(ENDPOINT)
    explicit_off = await client.get(ENDPOINT, params={"mine": "false"})

    assert everything.status_code == 200
    assert _ids(everything) == [str(mine.id), str(theirs.id), str(nobodys.id)]
    assert explicit_off.json() == everything.json()


@pytest.mark.parametrize("scope", ["clinic", "own"])
async def test_mine_without_a_professional_of_ones_own_is_422(
    client: AsyncClient, db, tenant, scope
) -> None:
    await _conversation(db, tenant, "Paciente", minutes_ago=1)
    view_as(AgendaViewer(scope, None))  # a receptionist, or a user linked to no doctor

    response = await client.get(ENDPOINT, params={"mine": "true"})

    assert response.status_code == 422
    assert response.json() == NO_OWN_AGENDA


async def test_mine_never_lists_another_clinics_conversation(
    client: AsyncClient, db, tenant
) -> None:
    ana = await _professional(db, tenant.id, "Dra. Ana")
    other = await _tenant(db, "Other Clinic")
    foreign = await _conversation(db, other, "Paciente de outra clínica", minutes_ago=1)
    # Even an (inconsistent) row naming Ana's id in the other clinic must not leak it.
    await _appointment(db, other.id, foreign, ana)
    view_as(AgendaViewer("clinic", ana))

    response = await client.get(ENDPOINT, params={"mine": "true"})

    assert response.status_code == 200
    assert response.json() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYT tests/test_hub_conversations_my_patients.py`
Expected: FAIL — `test_mine_lists_only_the_viewers_patients_newest_first` gets four rows (the flag is ignored today), the 422 tests get 200; `test_without_mine_every_viewer_sees_the_whole_clinic` already PASSES (it pins today's behaviour and must keep passing).

- [ ] **Step 3: Add the status constant**

In `src/secretaria/models/appointment.py`, directly after the `TERMINAL_APPOINTMENT_STATUSES` tuple, add:

```python
# A doctor's patient for the console's "Meus pacientes" filter (TASK-046 R8, spec
# 2026-10-09 §5.D): the booking is happening, happened, or was this patient's slot and
# they missed it. CANCELLED never happened and does not make anyone "his" patient.
DOCTOR_PATIENT_STATUSES: tuple[AppointmentStatus, ...] = LIVE_APPOINTMENT_STATUSES + (
    AppointmentStatus.ATTENDED,
    AppointmentStatus.NO_SHOW,
)
```

- [ ] **Step 4: Create the service clause**

Create `src/secretaria/services/doctor_patients.py`:

```python
"""Which conversations are "my patients" for a doctor (TASK-046 R8, spec 2026-10-09 §5.D).

The console's "Meus pacientes" chip is a FILTER, never a permission: every person at the
clinic sees every conversation; a doctor may narrow the list to patients who have or had
an appointment with him. This module is the one definition of that relation, as a SQL
clause the hub list adds to its own query (one round trip, no per-row lookups).

"Have or had" = an appointment of the same clinic, for this professional, in any status of
`models/appointment.py::DOCTOR_PATIENT_STATUSES` (everything but CANCELLED). The match is
by patient: phone-only bookings and Google-only events have no patient and never count.
"""

from uuid import UUID

from sqlalchemy import ColumnElement, select

from secretaria.models.appointment import DOCTOR_PATIENT_STATUSES, Appointment
from secretaria.models.conversation import Conversation


def is_patient_of(professional_id: UUID) -> ColumnElement[bool]:
    """EXISTS clause, correlated to `Conversation`: its patient is this doctor's patient."""
    return (
        select(Appointment.id)
        .where(
            Appointment.tenant_id == Conversation.tenant_id,
            Appointment.patient_id == Conversation.patient_id,
            Appointment.professional_id == professional_id,
            Appointment.status.in_(DOCTOR_PATIENT_STATUSES),
        )
        .correlate(Conversation)
        .exists()
    )
```

- [ ] **Step 5: Wire the route**

In `src/secretaria/api/hub/conversations.py`:

(a) Module docstring — replace the first entry

```
GET  /tenants/me/conversations                     - list every conversation
                                                       for the authenticated
                                                       tenant, newest activity
                                                       first.
```

with

```
GET  /tenants/me/conversations                     - list every conversation
                                                       for the authenticated
                                                       tenant, newest activity
                                                       first. `?mine=true`
                                                       ("Meus pacientes",
                                                       TASK-046) narrows it to
                                                       the viewer's own
                                                       patients - a filter,
                                                       never a permission.
```

(b) Imports — change

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
```

to

```python
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
```

change

```python
from secretaria.api.hub.deps import get_current_tenant
```

to

```python
from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
```

and add, in alphabetical position among the `secretaria.services` imports:

```python
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.doctor_patients import is_patient_of
```

(c) Replace the whole `list_conversations` function with:

```python
# Same code and sentence as GET /calendar/events?mine=true (api/hub/calendar.py).
_NO_OWN_AGENDA = {
    "code": "no_own_agenda",
    "message": "Seu usuário não está ligado a um profissional da clínica.",
}


@router.get("", response_model=list[ConversationRead])
async def list_conversations(
    mine: bool = Query(
        default=False,
        description=(
            '"Meus pacientes": only conversations whose patient has or had an appointment '
            "with the viewer's own professional (any status but cancelled). A filter, never "
            "a permission: without it every viewer gets the whole clinic."
        ),
    ),
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationRead]:
    # Spec 2026-10-09 §5.D (owner, replacing the earlier role table): everyone at the
    # clinic sees every conversation. The viewer is read ONLY to answer `mine`; it never
    # narrows the default list, not even for a doctor restricted to his own agenda.
    own = viewer.professional_id
    if mine and own is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _NO_OWN_AGENDA)
    # One query: join Conversation -> Patient, LEFT OUTER join a grouped
    # subquery for the most recent message per conversation (regardless of
    # who sent it). No pagination — deliberately minimal for the dashboard.
    last_msg_sub = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_message_at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    stmt = (
        select(Conversation, Patient, last_msg_sub.c.last_message_at)
        .join(Patient, Patient.id == Conversation.patient_id)
        .outerjoin(last_msg_sub, last_msg_sub.c.conversation_id == Conversation.id)
        .where(Conversation.tenant_id == tenant.id)
        # NULLS LAST is native to SQLite >= 3.30 and Postgres alike, so this
        # stays portable across the sqlite test DB and the real Postgres DB.
        .order_by(last_msg_sub.c.last_message_at.desc().nulls_last())
    )
    if mine and own is not None:
        stmt = stmt.where(is_patient_of(own))
    rows = (await session.execute(stmt)).all()
    return [
        _read_model(conversation, patient, last_message_at)
        for conversation, patient, last_message_at in rows
    ]
```

- [ ] **Step 6: Run the new tests and the existing conversation tests**

Run: `PYT tests/test_hub_conversations_my_patients.py tests/test_hub_conversations.py tests/test_agenda_viewer.py`
Expected: PASS (the old file's `test_list_without_token_is_401` still gets 401: `get_current_tenant` runs first).

- [ ] **Step 7: Lint, line endings, code points**

```bash
uvx ruff format src/secretaria/services/doctor_patients.py tests/test_hub_conversations_my_patients.py
uvx ruff check --fix src/secretaria/services/doctor_patients.py tests/test_hub_conversations_my_patients.py
uvx ruff check src/secretaria/api/hub/conversations.py src/secretaria/models/appointment.py
git diff --stat
LC_ALL=C.UTF-8 grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]' src/secretaria/services/doctor_patients.py src/secretaria/api/hub/conversations.py src/secretaria/models/appointment.py tests/test_hub_conversations_my_patients.py
```
Expected: ruff clean on the lines touched; `git diff --stat` shows only a few lines in the two modified files; grep prints nothing.

- [ ] **Step 8: Commit**

```bash
git add src/secretaria/models/appointment.py src/secretaria/services/doctor_patients.py src/secretaria/api/hub/conversations.py tests/test_hub_conversations_my_patients.py
git commit -m "feat(hub): 'Meus pacientes' filter on the staff conversation list (TASK-046 R8)

GET /tenants/me/conversations?mine=true lists only conversations whose patient has or
had a non-cancelled appointment with the viewer's own professional; without the flag
every viewer keeps the whole clinic. 422 no_own_agenda for a viewer with no professional.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2 (Part A close): checkpoint, full suite, graph

**Files:**
- Create: `docs/CHECKPOINT_conversas_meus_pacientes.md`
- Modify: `CLAUDE.md` (one line in "## Documentação")

**Interfaces:**
- Consumes: Task 1.
- Produces: the checkpoint Part B and the deployer read.

- [ ] **Step 1: Full suite**

Run: `PYT tests` (the full form of the command with `tests`). Record `N passed`. For any failure, rerun that test in a throwaway worktree of the base commit (`git worktree add <scratch dir> cb526d5`, same command) and classify it as pre-existing (known: the 00–03h UTC `test_human_backup_plugin` flake) or regression; fix regressions before continuing, then remove the throwaway worktree.

- [ ] **Step 2: Write the checkpoint**

Create `docs/CHECKPOINT_conversas_meus_pacientes.md`:

```markdown
# CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.D (decisão do dono de
2026-10-09 que substitui a tabela anterior). Plano: `docs/superpowers/plans/2026-10-09-lembretes-r8-filtro-meus-pacientes-e-seletor-medico.md`.

## Estado
- Local, commitado na branch `task/TASK-046-meus-pacientes-api` (worktree `BRAIN-worktrees/TASK-046/secretarIA`).
  Não mesclado, não pushado, não deployado. Sem migração.
- Suíte completa: <N> passed (<falhas pré-existentes, se houver, com a prova>).

## O que mudou
- `GET /tenants/me/conversations?mine=true` (`api/hub/conversations.py::list_conversations`): só conversas cujo
  paciente tem/teve consulta com o profissional do próprio usuário. Sem o parâmetro: lista de sempre, para todos
  (inclusive o médico restrito da agenda) — filtro, nunca permissão.
- Regra única em `services/doctor_patients.py::is_patient_of` + `models/appointment.py::DOCTOR_PATIENT_STATUSES`
  (agendada, confirmada, remarcada, compareceu, faltou; **cancelada não conta**; casa por paciente, mesmo tenant).
- Usuário sem profissional próprio + `mine=true` → 422 `no_own_agenda` (mesmo código do `GET /calendar/events?mine=true`).
- O papel/profissional vem do brain-api via `api/hub/deps.py::get_agenda_viewer` (R7); nenhuma mudança no brain-api.

## Deploy (não executado)
`secretaria_api` antes do Brain-Message-Frontend (parte B). O worker não muda; deployar junto só mantém
`deploy_parity=match`. Front antes da API = o filtro mostraria todas as conversas (a API velha ignora `mine`).

## Fora do escopo
- Nenhuma rota `/{id}/…` de conversa mudou; o console continua sem escopo por papel.
- `brain-api/docs/PORTAL_MESSAGING_API.md` não descreve a lista do hub (só a cita); não foi alterado.
```

Replace `<N>` and the failures line with the real values from Step 1.

- [ ] **Step 3: Pointer in CLAUDE.md**

In `CLAUDE.md`, under `## Documentação`, add as the first entry (before the `docs/CHECKPOINT_edicao_ia_nao_perde_rascunho.md` line):

```markdown
`docs/CHECKPOINT_conversas_meus_pacientes.md` — TASK-046 R8: `GET /tenants/me/conversations?mine=true` ("Meus pacientes", filtro e não permissão; cancelada não conta); local, não deployado; API antes do front.
```

- [ ] **Step 4: Graph**

Run `graphify update .` (if the exe is blocked by App Control, `python -m graphify update .`). If neither runs, write one line in the checkpoint "graphify não atualizado: <motivo>".

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_conversas_meus_pacientes.md CLAUDE.md
git commit -m "docs: checkpoint for the 'Meus pacientes' conversation filter (TASK-046 R8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
(Add `graphify-out/` paths explicitly only if this repo tracks them and they changed.)

---

# Part B — Brain-Message-Frontend (after R5 is merged)

### Task 3: console data — who is a doctor, which conversations are his

**Files:**
- Create: `lib/real/console-my-patients.ts`
- Modify: `lib/types.ts` (`Me`, `ConversationFilter`)
- Modify: `lib/console-api.ts` (interface)
- Modify: `lib/real/console-api.real.ts` (`me()`, new method)
- Modify: `lib/conversations.ts` (`applyConversationFilter`, copy)
- Test: `lib/__tests__/console-my-patients.test.ts` (create), `lib/__tests__/conversations.test.ts` (extend)

**Interfaces:**
- Consumes: Part A wire; R5 `lib/agenda/secretaria-hub-agenda.ts::getAgendaViewer(session): Promise<AgendaViewerWire>`, `lib/agenda/viewer.ts::viewerFromWire(w): AgendaViewer` (`professionalId: string | null`); `lib/real/secretaria-hub.ts::hubFetch`.
- Produces: `fetchOwnProfessionalId(s: Session): Promise<string | null>`; `fetchMyPatientConversationIds(s: Session): Promise<string[]>`; `Me.ownProfessionalId?: string | null`; `ConversationFilter.myPatients?: boolean`; `ConsoleApi.listMyPatientConversationIds?(): Promise<string[]>`; `applyConversationFilter(list, filter, currentUserId, myPatientIds?: ReadonlySet<string> | null)`; `MY_PATIENTS_COPY = { chip, emptyTitle, emptyText }`.

- [ ] **Step 1: Write the failing tests**

Create `lib/__tests__/console-my-patients.test.ts`:

```ts
// "Meus pacientes" hub calls (TASK-046 R8). fetch is stubbed and brain-session's mint is
// mocked, as in lib/__tests__/agenda-secretaria-hub.test.ts.
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Session } from "../real/brain-session";
import { fetchMyPatientConversationIds, fetchOwnProfessionalId } from "../real/console-my-patients";
import { SECRETARIA_HUB_BASE } from "../real/secretaria-hub";

vi.mock("../real/brain-session", () => ({
  mintHubToken: vi.fn(async () => ({ hubToken: "fake-hub-token", expiresIn: 3600 })),
}));

const session: Session = { token: "t", tenantId: "tenant-1", email: "d@x.com", role: "doctor", userId: "u1" };

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const row = (id: string) => ({ id, patient_wa_id: null, patient_name: "Paciente", handover_state: "BOT_ACTIVE", last_message_at: null });

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal("fetch", vi.fn());
});

describe("fetchMyPatientConversationIds", () => {
  it("GETs the hub list with mine=true and keeps only the ids, in order", async () => {
    const f = vi.mocked(fetch);
    f.mockResolvedValue(json(200, [row("c2"), row("c1")]));
    expect(await fetchMyPatientConversationIds(session)).toEqual(["c2", "c1"]);
    expect(String(f.mock.calls[0][0])).toBe(SECRETARIA_HUB_BASE + "/tenants/me/conversations?mine=true");
  });

  it("lets the hub's refusal through (422 no_own_agenda) instead of answering 'no patients'", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json(422, { detail: { code: "no_own_agenda", message: "Seu usuário não está ligado a um profissional da clínica." } }),
    );
    await expect(fetchMyPatientConversationIds(session)).rejects.toMatchObject({ status: 422, code: "no_own_agenda" });
  });
});

describe("fetchOwnProfessionalId", () => {
  it("a doctor (restricted or manager): his professional id from GET /calendar/viewer", async () => {
    const f = vi.mocked(fetch);
    f.mockResolvedValue(json(200, { agenda_scope: "own", professional_id: "pro-2", professional_name: "Dr. Beto", can_filter_own: false }));
    expect(await fetchOwnProfessionalId(session)).toBe("pro-2");
    expect(String(f.mock.calls[0][0])).toBe(SECRETARIA_HUB_BASE + "/tenants/me/calendar/viewer");
  });

  it("a secretary: null", async () => {
    vi.mocked(fetch).mockResolvedValue(json(200, { agenda_scope: "clinic", professional_id: null, professional_name: null, can_filter_own: false }));
    expect(await fetchOwnProfessionalId(session)).toBeNull();
  });

  it("an older backend or an outage: null, never a thrown error (no chip, console still opens)", async () => {
    vi.mocked(fetch).mockResolvedValue(json(404, { detail: "Not Found" }));
    expect(await fetchOwnProfessionalId(session)).toBeNull();
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await fetchOwnProfessionalId(session)).toBeNull();
  });
});
```

Append to `lib/__tests__/conversations.test.ts` (keep its existing imports; change the import line to also bring `MY_PATIENTS_COPY`):

```ts
import { MY_PATIENTS_COPY, applyConversationFilter, conversationCounts } from "../conversations";
```

and add at the end of the file:

```ts
describe("applyConversationFilter — Meus pacientes (TASK-046 R8)", () => {
  const withPrecheck = [...list, conv("precheck:s1", { product: "precheck", state: "aguardando" })];

  it("keeps only the ids the hub returned for mine=true; PreCheck threads leave the list", () => {
    const ids = new Set(["a", "c", "precheck:s1"]);
    expect(applyConversationFilter(withPrecheck, { scope: "todas", query: "", myPatients: true }, "me", ids).map((c) => c.id)).toEqual(["a", "c"]);
  });

  it("combines with the scope chips and the search", () => {
    const ids = new Set(["a", "b", "c"]);
    expect(applyConversationFilter(list, { scope: "aguardando", query: "", myPatients: true }, "me", ids).map((c) => c.id)).toEqual(["a"]);
    expect(applyConversationFilter(list, { scope: "todas", query: "beatriz", myPatients: true }, "me", ids).map((c) => c.id)).toEqual(["c"]);
  });

  it("with the chip on and no id set yet, shows nothing (the screen shows a spinner, never 'no patients')", () => {
    expect(applyConversationFilter(list, { scope: "todas", query: "", myPatients: true }, "me", null)).toEqual([]);
  });

  it("with the chip off, the id set changes nothing", () => {
    expect(applyConversationFilter(list, { scope: "todas", query: "", myPatients: false }, "me", new Set(["a"]))).toHaveLength(3);
    expect(applyConversationFilter(list, { scope: "todas", query: "" }, "me")).toHaveLength(3);
  });

  it("says what the filter means when it is empty", () => {
    expect(MY_PATIENTS_COPY.chip).toBe("Meus pacientes");
    expect(MY_PATIENTS_COPY.emptyText).toMatch(/consulta com você/);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npx vitest run lib/__tests__/console-my-patients.test.ts lib/__tests__/conversations.test.ts`
Expected: FAIL — module `../real/console-my-patients` not found; `MY_PATIENTS_COPY` undefined; `myPatients` not honoured.

- [ ] **Step 3: Create the hub calls**

Create `lib/real/console-my-patients.ts`:

```ts
// console-my-patients.ts — "Meus pacientes" (TASK-046 R8, spec 2026-10-09 §5.D).
//
// A FILTER, never a permission: every person at the clinic sees every conversation.
// A viewer who is a doctor (has a secretarIA professional of his own) may narrow the
// list to patients who have or had an appointment with him; secretarIA decides who
// those are (GET /tenants/me/conversations?mine=true) — the console only asks.

import { getAgendaViewer } from "../agenda/secretaria-hub-agenda";
import { viewerFromWire } from "../agenda/viewer";
import type { Session } from "./brain-session";
import { hubFetch } from "./secretaria-hub";

/**
 * The viewer's own secretarIA professional id (R7 GET /calendar/viewer), or null for a
 * secretary, an unlinked user, an older backend or any failure: the chip is a
 * convenience, so its absence must never stop the console from opening.
 */
export async function fetchOwnProfessionalId(s: Session): Promise<string | null> {
  try {
    return viewerFromWire(await getAgendaViewer(s)).professionalId;
  } catch {
    return null;
  }
}

/** Ids of the conversations of the viewer's patients. Refusals (422 no_own_agenda) are thrown, not hidden. */
export async function fetchMyPatientConversationIds(s: Session): Promise<string[]> {
  const rows = await hubFetch<{ id: string }[]>(s, "/tenants/me/conversations?mine=true");
  return rows.map((r) => r.id);
}
```

- [ ] **Step 4: Types and interface**

In `lib/types.ts`, inside `export type Me = { … }`, after `capabilities: Capabilities;` add:

```ts
  // The viewer's own secretarIA professional (R7 GET /calendar/viewer), when he is a
  // doctor of this clinic. Only decides whether the "Meus pacientes" chip exists — it
  // never narrows anything by itself (spec 2026-10-09 §5.D). Absent/null = no chip.
  ownProfessionalId?: string | null;
```

and replace `ConversationFilter` with:

```ts
export type ConversationFilter = {
  scope: "todas" | "aguardando" | "nao_lidas" | "minhas";
  // Free-text search over patient name; empty string = no search.
  query: string;
  // "Meus pacientes" (TASK-046 R8): an independent toggle on top of the scope and the
  // search, for a doctor. The ids come from secretarIA (GET …/conversations?mine=true).
  myPatients?: boolean;
};
```

In `lib/console-api.ts`, directly after `listConversations(filter: ConversationFilter): Promise<ConversationSummary[]>;` add:

```ts
  // "Meus pacientes" (TASK-046 R8): ids of the conversations whose patient has or had an
  // appointment with the viewer. Optional: an implementation without it (the mock) never
  // shows the chip, because its Me carries no ownProfessionalId.
  listMyPatientConversationIds?(): Promise<string[]>;
```

- [ ] **Step 5: The pure filter**

Replace `lib/conversations.ts`'s `applyConversationFilter` with the version below and add `MY_PATIENTS_COPY` above it (keep `conversationCounts` unchanged):

```ts
// "Meus pacientes" (TASK-046 R8) copy, shared by the chip and its empty state.
export const MY_PATIENTS_COPY = {
  chip: "Meus pacientes",
  emptyTitle: "Nenhuma conversa de paciente seu.",
  emptyText: "Aparecem aqui os pacientes que têm ou já tiveram consulta com você.",
} as const;

export function applyConversationFilter(
  list: ConversationSummary[],
  filter: ConversationFilter,
  currentUserId: string,
  // The ids secretarIA returned for mine=true; null = not read yet (shows nothing while
  // the chip is on — the screen shows a spinner instead of an empty list).
  myPatientIds: ReadonlySet<string> | null = null,
): ConversationSummary[] {
  const q = filter.query.trim().toLocaleLowerCase("pt-BR");
  return list.filter((c) => {
    // PreCheck threads have no appointment link: out while the chip is on, whatever the ids say.
    if (filter.myPatients && c.product !== "secretaria") return false;
    if (filter.myPatients && !myPatientIds?.has(c.id)) return false;
    if (filter.scope === "aguardando" && c.state !== "aguardando") return false;
    if (filter.scope === "nao_lidas" && c.unreadCount === 0) return false;
    if (filter.scope === "minhas" && c.assignedToUserId !== currentUserId) return false;
    if (q && !c.patient.name.toLocaleLowerCase("pt-BR").includes(q)) return false;
    return true;
  });
}
```

- [ ] **Step 6: Wire the real ConsoleApi**

In `lib/real/console-api.real.ts`:

(a) Add the import next to the other `./` imports:

```ts
import { fetchMyPatientConversationIds, fetchOwnProfessionalId } from "./console-my-patients";
```

(b) In `me()`, replace

```ts
    let config: HubConfigWire | null = null;
    if (secretaria) {
      config = await hubFetch<HubConfigWire>(s, "/tenants/me/config");
    }
```

with

```ts
    let config: HubConfigWire | null = null;
    // TASK-046 R8: whether this person is a doctor here (the "Meus pacientes" chip). Read
    // with the config, never failing me(): fetchOwnProfessionalId answers null on error.
    let ownProfessionalId: string | null = null;
    if (secretaria) {
      [config, ownProfessionalId] = await Promise.all([
        hubFetch<HubConfigWire>(s, "/tenants/me/config"),
        fetchOwnProfessionalId(s),
      ]);
    }
```

and in the `built: Me` literal add, after `capabilities: buildCapabilities(secretaria, precheck, channelOn),`:

```ts
      ownProfessionalId,
```

(c) In the returned object, directly after the `listConversations: …` entry, add:

```ts
    listMyPatientConversationIds: () =>
      guarded(async () => fetchMyPatientConversationIds(await session())),
```

- [ ] **Step 7: Run the tests**

Run: `npx vitest run lib/__tests__/console-my-patients.test.ts lib/__tests__/conversations.test.ts lib/__tests__/real-me-platform-admin.test.ts`
Expected: PASS (the platform-admin file proves `me()` still refuses an admin before any request and still reads `/auth/me` + `/entitlements` for a clinic token).

- [ ] **Step 8: Typecheck and commit**

Run: `npm run typecheck` — Expected: no errors (the mock implements no `listMyPatientConversationIds`; it is optional).

```bash
git add lib/real/console-my-patients.ts lib/types.ts lib/console-api.ts lib/real/console-api.real.ts lib/conversations.ts lib/__tests__/console-my-patients.test.ts lib/__tests__/conversations.test.ts
git commit -m "feat(console): 'Meus pacientes' data — own professional and the hub's mine=true ids (TASK-046 R8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: console UI — the "Meus pacientes" chip

**Files:**
- Modify: `components/console/ConversationList.tsx`
- Modify: `components/console/ChatScreen.tsx`
- Test: `components/console/__tests__/conversation-list.test.tsx` (create)

**Interfaces:**
- Consumes: Task 3 (`Me.ownProfessionalId`, `ConversationFilter.myPatients`, `ConsoleApi.listMyPatientConversationIds`, `applyConversationFilter(..., myPatientIds)`, `MY_PATIENTS_COPY`); `lib/mock/data.ts::MOCK_ME`.
- Produces: the chip (a `Chip` with `aria-pressed`, label "Meus pacientes") inside the existing `role="group" aria-label="Filtrar conversas"`.

- [ ] **Step 1: Write the failing test**

Create `components/console/__tests__/conversation-list.test.tsx`:

```tsx
// ConversationList — "Meus pacientes" chip (TASK-046 R8). No jsdom: renderToStaticMarkup.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { MY_PATIENTS_COPY } from "@/lib/conversations";
import { MOCK_ME } from "@/lib/mock/data";
import type { ConversationFilter, Me } from "@/lib/types";
import { ConversationList } from "../ConversationList";

const noop = () => {};

function render(me: Me, filter: ConversationFilter, conversations: [] | null = []): string {
  return renderToStaticMarkup(
    <ConversationList
      me={me}
      anchor={null}
      conversations={conversations}
      counts={{ aguardando: 0, naoLidas: 0 }}
      error={null}
      onRetry={noop}
      filter={filter}
      onFilterChange={noop}
      selectedId={null}
      onStartConversation={noop}
    />,
  );
}

const chipTag = (html: string): string | null => {
  const m = html.match(new RegExp(`<button[^>]*><span>${MY_PATIENTS_COPY.chip}</span>`));
  return m ? m[0] : null;
};

const doctor: Me = { ...MOCK_ME, ownProfessionalId: "pro-1" };
const ALL: ConversationFilter = { scope: "todas", query: "" };

describe("ConversationList — Meus pacientes", () => {
  it("a viewer who is not a doctor never sees the chip", () => {
    expect(chipTag(render(MOCK_ME, ALL))).toBeNull();
    expect(chipTag(render({ ...MOCK_ME, ownProfessionalId: null }, ALL))).toBeNull();
  });

  it("a doctor sees it inside the filter group, off by default", () => {
    const html = render(doctor, ALL);
    expect(html).toContain('role="group" aria-label="Filtrar conversas"');
    expect(chipTag(html)).toContain('aria-pressed="false"');
  });

  it("pressed when on, and the empty state explains the filter instead of 'no conversations'", () => {
    const html = render(doctor, { ...ALL, myPatients: true });
    expect(chipTag(html)).toContain('aria-pressed="true"');
    expect(html).toContain(MY_PATIENTS_COPY.emptyTitle);
    expect(html).toContain(MY_PATIENTS_COPY.emptyText);
  });

  it("the search's empty copy still wins over the filter's", () => {
    const html = render(doctor, { scope: "todas", query: "Zé", myPatients: true });
    expect(html).toContain("Nenhum paciente encontrado");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run components/console/__tests__/conversation-list.test.tsx`
Expected: FAIL — no chip rendered.

- [ ] **Step 3: The chip and the empty copy**

In `components/console/ConversationList.tsx`:

(a) Add the import:

```ts
import { MY_PATIENTS_COPY } from "@/lib/conversations";
```

(b) In `emptyCopy`, right after the `if (filter.query.trim()) { … }` block, add:

```ts
  if (filter.myPatients) {
    return { title: MY_PATIENTS_COPY.emptyTitle, text: MY_PATIENTS_COPY.emptyText };
  }
```

(c) Inside `<div className="list-chips" …>`, after the `{SCOPES.map(…)}` expression, add:

```tsx
          {me.ownProfessionalId && (
            // "Meus pacientes" (TASK-046 R8, spec §5.D): only for a doctor; a toggle on top
            // of the scope and the search. A filter, never a permission.
            <Chip
              selected={filter.myPatients === true}
              onClick={() => onFilterChange({ ...filter, myPatients: !filter.myPatients })}
            >
              {MY_PATIENTS_COPY.chip}
            </Chip>
          )}
```

- [ ] **Step 4: Fetch the ids while the chip is on**

In `components/console/ChatScreen.tsx`, inside `ChatBody`:

(a) After `const [newOpen, setNewOpen] = useState(false);` add:

```ts
  // "Meus pacientes" (TASK-046 R8): the ids secretarIA answers for mine=true, read only
  // while the chip is on and re-read on the list's polling cadence. null = not read yet.
  const [myIds, setMyIds] = useState<ReadonlySet<string> | null>(null);
  const myFilterOn = filter.myPatients === true && Boolean(me.ownProfessionalId) && Boolean(api.listMyPatientConversationIds);
```

(b) After the "List + overview, polled." `useEffect`, add:

```ts
  // The "Meus pacientes" ids, polled like the list while the chip is on.
  useEffect(() => {
    const load = api.listMyPatientConversationIds;
    if (!myFilterOn || !load) {
      setMyIds(null);
      return;
    }
    let cancelled = false;
    const run = async () => {
      try {
        const ids = await load.call(api);
        if (!cancelled) setMyIds(new Set(ids));
      } catch (error) {
        if (!cancelled) fail(error);
      }
    };
    void run();
    const id = window.setInterval(() => { if (!document.hidden) void run(); }, LIST_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [api, fail, epoch, myFilterOn]);
```

(c) Replace

```ts
  const visible = all ? applyConversationFilter(all, filter, me.user.id) : null;
```

with

```ts
  const effectiveFilter: ConversationFilter = { ...filter, myPatients: myFilterOn };
  // While the chip waits for its ids, the list shows the spinner, never "nobody is yours".
  const visible = all && (!myFilterOn || myIds) ? applyConversationFilter(all, effectiveFilter, me.user.id, myIds) : null;
```

(`retryAll` already bumps `epoch`, so "Tentar novamente" after a failed id read re-runs this effect.)

- [ ] **Step 5: Run tests and typecheck**

Run: `npx vitest run components/console lib/__tests__/conversations.test.ts` then `npm run typecheck`
Expected: PASS / no errors.

- [ ] **Step 6: Commit**

```bash
git add components/console/ConversationList.tsx components/console/ChatScreen.tsx components/console/__tests__/conversation-list.test.tsx
git commit -m "feat(console): 'Meus pacientes' chip for doctors — a filter, never a permission (TASK-046 R8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: agenda — pure doctor-picker logic

**Files:**
- Create: `lib/agenda/create-professional.ts`
- Test: `lib/agenda/__tests__/create-professional.test.ts`

**Interfaces:**
- Consumes: `lib/real/secretaria-hub.ts::ProfessionalWire` (`id`, `name`, `is_active`).
- Produces:
  - `type CreatePro = { id: string; name: string }`
  - `type CreateProChoices = { status: "idle" } | { status: "loading" } | { status: "loaded"; list: CreatePro[] } | { status: "failed" }`
  - `CREATE_PROS_IDLE: CreateProChoices`, `NO_PROFESSIONAL = "none"`, `CREATE_PRO_COPY`
  - `createProChoicesFrom(pros: readonly Pick<ProfessionalWire, "id" | "name" | "is_active">[]): CreateProChoices`
  - `initialProChoice(choices: CreateProChoices, ownProfessionalId: string | null): string`
  - `proSelectOptions(choices: CreateProChoices): { value: string; label: string }[]`
  - `proChoiceReady(choice: string): boolean`
  - `professionalIdOf(choice: string): string | undefined`

- [ ] **Step 1: Write the failing test**

Create `lib/agenda/__tests__/create-professional.test.ts`:

```ts
// Who a new consultation/block belongs to (TASK-046 R8, spec 2026-10-09 §5.E). Pure.
import { describe, expect, it } from "vitest";

import {
  CREATE_PRO_COPY,
  NO_PROFESSIONAL,
  createProChoicesFrom,
  initialProChoice,
  proChoiceReady,
  proSelectOptions,
  professionalIdOf,
  type CreateProChoices,
} from "../create-professional";

const loaded = (...names: [string, string][]): CreateProChoices => ({
  status: "loaded",
  list: names.map(([id, name]) => ({ id, name })),
});

describe("createProChoicesFrom", () => {
  it("keeps only active professionals (R7 refuses the others), sorted by name in pt-BR", () => {
    expect(
      createProChoicesFrom([
        { id: "p3", name: "Dr. Zeca", is_active: true },
        { id: "p1", name: "Dra. Ána", is_active: true },
        { id: "p2", name: "Dr. Antigo", is_active: false },
      ]),
    ).toEqual(loaded(["p1", "Dra. Ána"], ["p3", "Dr. Zeca"]));
  });
});

describe("initialProChoice", () => {
  const two = loaded(["p1", "Dra. Ana"], ["p2", "Dr. Beto"]);

  it("a receptionist in a clinic with several doctors must choose (nothing picked)", () => {
    expect(initialProChoice(two, null)).toBe("");
  });

  it("a manager who is also a doctor starts on himself", () => {
    expect(initialProChoice(two, "p2")).toBe("p2");
  });

  it("his own id not in the active list (deactivated) is not picked for him", () => {
    expect(initialProChoice(two, "p9")).toBe("");
  });

  it("a one-doctor clinic starts on that doctor", () => {
    expect(initialProChoice(loaded(["p1", "Dra. Ana"]), null)).toBe("p1");
  });

  it("no active doctor, or the list failed: 'Sem profissional definido' (R7 accepts it)", () => {
    expect(initialProChoice(loaded(), null)).toBe(NO_PROFESSIONAL);
    expect(initialProChoice({ status: "failed" }, "p1")).toBe(NO_PROFESSIONAL);
  });

  it("while loading, nothing is picked", () => {
    expect(initialProChoice({ status: "loading" }, "p1")).toBe("");
    expect(initialProChoice({ status: "idle" }, null)).toBe("");
  });
});

describe("proSelectOptions", () => {
  it("placeholder first, the doctors, then 'Sem profissional definido'", () => {
    expect(proSelectOptions(loaded(["p1", "Dra. Ana"]))).toEqual([
      { value: "", label: CREATE_PRO_COPY.choose },
      { value: "p1", label: "Dra. Ana" },
      { value: NO_PROFESSIONAL, label: CREATE_PRO_COPY.none },
    ]);
  });

  it("loading shows one disabled-looking row; failed offers only 'Sem profissional definido'", () => {
    expect(proSelectOptions({ status: "loading" })).toEqual([{ value: "", label: CREATE_PRO_COPY.loading }]);
    expect(proSelectOptions({ status: "failed" })).toEqual([{ value: NO_PROFESSIONAL, label: CREATE_PRO_COPY.none }]);
  });
});

describe("choice → payload", () => {
  it("'' blocks the submit; a doctor or 'none' allows it", () => {
    expect(proChoiceReady("")).toBe(false);
    expect(proChoiceReady("p1")).toBe(true);
    expect(proChoiceReady(NO_PROFESSIONAL)).toBe(true);
  });

  it("a doctor is sent; 'none' and '' send no professional_id", () => {
    expect(professionalIdOf("p1")).toBe("p1");
    expect(professionalIdOf(NO_PROFESSIONAL)).toBeUndefined();
    expect(professionalIdOf("")).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run lib/agenda/__tests__/create-professional.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `lib/agenda/create-professional.ts`:

```ts
// lib/agenda/create-professional.ts — WHO a new consultation/block belongs to (TASK-046
// R8, spec 2026-10-09 §5.E). A clinic-wide viewer picks the doctor from the clinic's
// active professionals, or "Sem profissional definido" (R7 stores NULL, as today). A
// doctor restricted to his own agenda never sees this list: he is locked to himself
// (viewer.ts::lockedProfessional) and the server enforces it (R7 _creation_professional).
// Pure.

import type { ProfessionalWire } from "../real/secretaria-hub";

export type CreatePro = { id: string; name: string };

export type CreateProChoices =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; list: CreatePro[] }
  | { status: "failed" };

/** One shared object, so a sheet without a list keeps a stable prop identity. */
export const CREATE_PROS_IDLE: CreateProChoices = { status: "idle" };

/** The Select value of "Sem profissional definido" — never a real id (ids are UUIDs). */
export const NO_PROFESSIONAL = "none";

export const CREATE_PRO_COPY = {
  label: "Profissional",
  choose: "Escolha o profissional",
  none: "Sem profissional definido",
  loading: "Carregando profissionais…",
  failed: "Não foi possível carregar a lista de profissionais. Você pode marcar sem profissional definido ou fechar e abrir de novo.",
  unknown: "Este profissional não está mais ativo na clínica. Escolha outro.",
} as const;

const byName = (a: CreatePro, b: CreatePro) => a.name.localeCompare(b.name, "pt-BR");

/** Active professionals only (R7 answers 422 unknown_professional for the others), by name. */
export function createProChoicesFrom(pros: readonly Pick<ProfessionalWire, "id" | "name" | "is_active">[]): CreateProChoices {
  return {
    status: "loaded",
    list: pros.filter((p) => p.is_active).map((p) => ({ id: p.id, name: p.name })).sort(byName),
  };
}

/**
 * The picker's starting value. "" = nothing picked (the sheet's submit stays disabled):
 * with several doctors the choice is deliberate, because a doctor-less booking is
 * invisible to every restricted doctor (R7 §5.A). A manager-doctor starts on himself; a
 * one-doctor clinic on that doctor; no doctor / failed list on "Sem profissional".
 */
export function initialProChoice(choices: CreateProChoices, ownProfessionalId: string | null): string {
  if (choices.status === "failed") return NO_PROFESSIONAL;
  if (choices.status !== "loaded") return "";
  const { list } = choices;
  if (list.length === 0) return NO_PROFESSIONAL;
  if (ownProfessionalId && list.some((p) => p.id === ownProfessionalId)) return ownProfessionalId;
  if (list.length === 1) return list[0].id;
  return "";
}

export function proSelectOptions(choices: CreateProChoices): { value: string; label: string }[] {
  if (choices.status === "idle" || choices.status === "loading") return [{ value: "", label: CREATE_PRO_COPY.loading }];
  const none = { value: NO_PROFESSIONAL, label: CREATE_PRO_COPY.none };
  if (choices.status === "failed") return [none];
  return [{ value: "", label: CREATE_PRO_COPY.choose }, ...choices.list.map((p) => ({ value: p.id, label: p.name })), none];
}

/** Whether the sheet may submit with this choice ("" = not picked yet). */
export function proChoiceReady(choice: string): boolean {
  return choice !== "";
}

/** The `professional_id` to send; undefined = send none (R7 keeps it NULL). */
export function professionalIdOf(choice: string): string | undefined {
  return choice === "" || choice === NO_PROFESSIONAL ? undefined : choice;
}
```

- [ ] **Step 4: Run and commit**

Run: `npx vitest run lib/agenda/__tests__/create-professional.test.ts` — Expected: PASS.

```bash
git add lib/agenda/create-professional.ts lib/agenda/__tests__/create-professional.test.ts
git commit -m "feat(agenda): pure doctor-picker rules for Nova consulta/Bloquear (TASK-046 R8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: agenda — load the list, show the picker, send the choice

**Files:**
- Modify: `lib/agenda/secretaria-hub-agenda.ts` (refusal code)
- Modify: `lib/agenda/screen.ts` (modal shape, scoped action, reducer case, `openNew`/`openBlock`, `loadCreatePros`, `createAppt`/`createBlock`, `createErrorText`, `BlockInput`)
- Modify: `components/agenda/Modals.tsx` (picker in both sheets)
- Modify: `components/agenda/AgendaView.tsx` (pass the list and the viewer's id)
- Test: `lib/agenda/__tests__/screen.test.ts`, `components/agenda/__tests__/modals.test.tsx`, `components/agenda/__tests__/agenda-view.test.tsx`

**Interfaces:**
- Consumes: Task 5 (all exports); R5 `AgendaHub.getProfessionals(s)`, `lockedProfessional(viewer)`, `canCreate(viewer)`, `fresh()`, `noticeSwitch(e)`, `errorFacts(e)`, `onWriteError(s, e, msg)`, `scoped(tenantId, action)`; `Appt.professionalId?: string`.
- Produces: `AgendaModal` `new`/`block` carry `pros?: CreateProChoices`; `TenantScopedAction` `{ type: "create_pros_loaded"; modal: "new" | "block"; pros: CreateProChoices }`; `BlockInput.professionalId?: string`; `BlockData.professionalId?: string`; `NewApptModalProps`/`BlockModalProps` gain `pros?: CreateProChoices` and `ownProfessionalId?: string | null`; `UNKNOWN_PROFESSIONAL_CODE = "unknown_professional"`.

- [ ] **Step 1: Write the failing controller tests**

In `lib/agenda/__tests__/screen.test.ts`, add `UNKNOWN_PROFESSIONAL_CODE` to the existing import from `../secretaria-hub-agenda` (the one that already brings `PROFESSIONAL_NOT_ALLOWED_CODE` / `NO_OWN_AGENDA_CODE`), add

```ts
import { CREATE_PRO_COPY } from "../create-professional";
```

and append after the describe "a restricted doctor books and blocks only on his own agenda (R7, owner 2026-10-09)":

```ts
describe("a clinic-wide viewer picks the doctor of Nova consulta / Bloquear (TASK-046 R8, spec §5.E)", () => {
  const createCalls = (s: { hub: AgendaHub }) => (s.hub.createAppointment as ReturnType<typeof vi.fn>).mock.calls;
  const blockCalls = (s: { hub: AgendaHub }) => (s.hub.createBlock as ReturnType<typeof vi.fn>).mock.calls;
  // A fresh mock per test: call counts must not leak between tests.
  const pros = () =>
    vi.fn(async () => [
      { id: "pro-3", name: "Dr. Zeca", is_active: true },
      { id: "pro-1", name: "Dra. Ana", is_active: true },
      { id: "pro-9", name: "Dr. Antigo", is_active: false },
    ]);

  it("opening either sheet reads the clinic's active doctors, sorted", async () => {
    const s = asViewer(VIEWERS.secretary, { getProfessionals: pros() });
    await s.controller.start();
    s.controller.openNew();
    expect(s.state().modal).toMatchObject({ type: "new", pros: { status: "loading" } });
    await flush();
    expect(s.state().modal).toMatchObject({
      type: "new",
      pros: { status: "loaded", list: [{ id: "pro-1", name: "Dra. Ana" }, { id: "pro-3", name: "Dr. Zeca" }] },
    });
    s.controller.closeModal();
    s.controller.openBlock();
    await flush();
    expect(s.state().modal).toMatchObject({ type: "block", pros: { status: "loaded" } });
  });

  it("the chosen doctor is sent on both; no choice sends none (R7 keeps NULL)", async () => {
    const s = asViewer(VIEWERS.manager, { getProfessionals: pros() });
    await s.controller.start();
    s.controller.openNew();
    await s.controller.createAppt({ ...NEW_APPT, professionalId: "pro-3" });
    expect(createCalls(s)[0][1].professional_id).toBe("pro-3");
    s.controller.openBlock();
    await s.controller.createBlock({ ...BLOCK, professionalId: "pro-3" });
    expect(blockCalls(s)[0][1].professional_id).toBe("pro-3");
    s.controller.openNew();
    await s.controller.createAppt(NEW_APPT);
    expect("professional_id" in createCalls(s)[1][1]).toBe(false);
  });

  it("a restricted doctor never reads the list, and his own id wins over anything in the form", async () => {
    const getProfessionals = pros();
    const s = asViewer(VIEWERS.doctor, { getProfessionals });
    await s.controller.start();
    s.controller.openNew();
    await flush();
    expect(getProfessionals).not.toHaveBeenCalled();
    expect(s.state().modal).toMatchObject({ type: "new", pros: { status: "idle" } });
    await s.controller.createAppt({ ...NEW_APPT, professionalId: "pro-1" });
    expect(createCalls(s)[0][1].professional_id).toBe("pro-2");
  });

  it("a failed list still opens the sheet (the picker offers 'Sem profissional definido')", async () => {
    const s = asViewer(VIEWERS.secretary, { getProfessionals: vi.fn(async () => Promise.reject(new HubApiError(500, "x"))) });
    await s.controller.start();
    s.controller.openBlock();
    await flush();
    expect(s.state().modal).toMatchObject({ type: "block", pros: { status: "failed" } });
  });

  it("422 unknown_professional: the backend's sentence (or ours) in the sheet, and the list is read again", async () => {
    const getProfessionals = vi.fn(async () => [{ id: "pro-1", name: "Dra. Ana", is_active: true }]);
    const s = asViewer(VIEWERS.secretary, {
      getProfessionals,
      createAppointment: vi.fn(async () => Promise.reject(new HubApiError(422, "", UNKNOWN_PROFESSIONAL_CODE))),
    });
    await s.controller.start();
    s.controller.openNew();
    await flush();
    await s.controller.createAppt({ ...NEW_APPT, professionalId: "pro-7" });
    await flush();
    expect(s.state().modal).toMatchObject({ type: "new", pending: false, error: CREATE_PRO_COPY.unknown, pros: { status: "loaded" } });
    expect(getProfessionals).toHaveBeenCalledTimes(2);
    expect(s.state().toast).toBeNull();
  });
});
```

(`asViewer`, `NEW_APPT`, `BLOCK`, `VIEWERS`, `flush`, `HubApiError` already exist in this file from R5.)

- [ ] **Step 2: Write the failing sheet tests**

In `components/agenda/__tests__/modals.test.tsx` add the import

```ts
import { CREATE_PRO_COPY, type CreateProChoices } from "../../../lib/agenda/create-professional";
```

and append:

```ts
describe("doctor picker (TASK-046 R8, spec §5.E)", () => {
  const two: CreateProChoices = { status: "loaded", list: [{ id: "p1", name: "Dra. Ana" }, { id: "p2", name: "Dr. Beto" }] };
  const submitDisabled = (html: string) => /btn--primary"[^>]*disabled/.test(html);
  const label = `<label for="[^"]+" class="field-label">${CREATE_PRO_COPY.label}</label>`;

  it("a receptionist must choose before blocking: placeholder selected, submit disabled", () => {
    const html = renderToStaticMarkup(<BlockModal open onClose={noop} onCreate={noop} days={days} pros={two} />);
    expect(html).toMatch(new RegExp(label));
    expect(html).toContain(`<option value="" selected="">${CREATE_PRO_COPY.choose}</option>`);
    expect(html).toContain('<option value="p1">Dra. Ana</option>');
    expect(html).toContain(`<option value="none">${CREATE_PRO_COPY.none}</option>`);
    expect(submitDisabled(html)).toBe(true);
  });

  it("a manager-doctor starts on himself and may submit", () => {
    const html = renderToStaticMarkup(<BlockModal open onClose={noop} onCreate={noop} days={days} pros={two} ownProfessionalId="p2" />);
    expect(html).toContain('<option value="p2" selected="">Dr. Beto</option>');
    expect(submitDisabled(html)).toBe(false);
  });

  it("a failed list offers only 'Sem profissional definido', says why, and allows the submit", () => {
    const html = renderToStaticMarkup(<BlockModal open onClose={noop} onCreate={noop} days={days} pros={{ status: "failed" }} />);
    expect(html).toContain(`<option value="none" selected="">${CREATE_PRO_COPY.none}</option>`);
    expect(html).toContain(CREATE_PRO_COPY.failed);
    expect(submitDisabled(html)).toBe(false);
  });

  it("while loading, the picker is disabled and so is the submit", () => {
    const html = renderToStaticMarkup(<BlockModal open onClose={noop} onCreate={noop} days={days} pros={{ status: "loading" }} />);
    expect(html).toContain(CREATE_PRO_COPY.loading);
    expect(html).toMatch(/<select[^>]*disabled/);
    expect(submitDisabled(html)).toBe(true);
  });

  it("a restricted doctor sees his locked line and no picker", () => {
    const html = renderToStaticMarkup(
      <NewApptModal open onClose={noop} onCreate={noop} days={days} pros={two} lockedProfessional={{ id: "p2", name: "Dr. Beto" }} />,
    );
    expect(html).not.toContain(CREATE_PRO_COPY.choose);
    expect(html).toContain("Dr. Beto");
  });

  it("NewApptModal shows the same picker for a clinic-wide viewer", () => {
    const html = renderToStaticMarkup(<NewApptModal open onClose={noop} onCreate={noop} days={days} pros={two} />);
    expect(html).toMatch(new RegExp(label));
  });
});
```

In `components/agenda/__tests__/agenda-view.test.tsx`, inside `describe("AgendaView — who is looking (R7)", …)` add:

```ts
  it("a clinic-wide viewer's sheet shows the doctor picker with the loaded list (TASK-046 R8)", () => {
    const html = render({
      ...as(manager),
      modal: { type: "new", pending: false, error: null, pros: { status: "loaded", list: [{ id: "pro-1", name: "Dra. Ana" }, { id: "pro-3", name: "Dr. Zeca" }] } },
    });
    expect(html).toContain('<option value="pro-1" selected="">Dra. Ana</option>'); // starts on himself
    expect(html).toContain('<option value="pro-3">Dr. Zeca</option>');
    expect(html).not.toContain(VIEWER_COPY.lockedHint);
  });
```

- [ ] **Step 3: Run to verify they fail**

Run: `npx vitest run lib/agenda/__tests__/screen.test.ts components/agenda/__tests__/modals.test.tsx components/agenda/__tests__/agenda-view.test.tsx`
Expected: FAIL — `UNKNOWN_PROFESSIONAL_CODE` not exported, no `pros` on the modal, no picker rendered.

- [ ] **Step 4: Refusal code**

In `lib/agenda/secretaria-hub-agenda.ts`, after `export const NO_OWN_AGENDA_CODE = …;` add:

```ts
export const UNKNOWN_PROFESSIONAL_CODE = "unknown_professional"; // not an active doctor of this clinic (422)
```

and in the doc comments of `AppointmentCreatePayload.professional_id` and `BlockCreatePayload.professional_id` replace "Sent only for a viewer restricted to his own (lib/agenda/viewer.ts::lockedProfessional)." with "A restricted viewer's own id (viewer.ts::lockedProfessional), or the doctor a clinic-wide viewer picked (create-professional.ts); absent = no doctor."

- [ ] **Step 5: Controller and state**

In `lib/agenda/screen.ts`:

(a) Imports — add `UNKNOWN_PROFESSIONAL_CODE` to the existing import list from `./secretaria-hub-agenda`, and add:

```ts
import { CREATE_PRO_COPY, createProChoicesFrom, type CreateProChoices } from "./create-professional";
```

(b) `AgendaModal` — replace the two lines

```ts
  | { type: "new"; pending: boolean; error: string | null }
  | { type: "block"; pending: boolean; error: string | null }
```

with

```ts
  /** `pros`: the doctor list for a clinic-wide viewer's picker (TASK-046 R8); absent/idle for a locked doctor. */
  | { type: "new"; pending: boolean; error: string | null; pros?: CreateProChoices }
  | { type: "block"; pending: boolean; error: string | null; pros?: CreateProChoices }
```

(c) `TenantScopedAction` — before the final `| { type: "toast"; toast: Toast | null };` add:

```ts
  /** The doctor list for the open Nova consulta / Bloquear sheet (TASK-046 R8). */
  | { type: "create_pros_loaded"; modal: "new" | "block"; pros: CreateProChoices }
```

(d) `applyScoped` — before `case "toast":` add:

```ts
    case "create_pros_loaded": {
      const m = state.modal;
      if (!m || (m.type !== "new" && m.type !== "block") || m.type !== action.modal) return state; // sheet closed / another one
      return { ...state, modal: { ...m, pros: action.pros } };
    }
```

(e) `BlockInput` — replace with:

```ts
export type BlockInput = { date: string; start: number; dur: number; reason: string; professionalId?: string };
```

(f) `createErrorText` — before `return fallback;` add:

```ts
  if (e instanceof HubApiError && e.status === 422 && e.code === UNKNOWN_PROFESSIONAL_CODE) return e.message || CREATE_PRO_COPY.unknown;
```

and right below the function add:

```ts
/** R7 422 unknown_professional: the picked doctor left the clinic's active list. */
function isUnknownProfessional(e: unknown): boolean {
  return e instanceof HubApiError && e.status === 422 && e.code === UNKNOWN_PROFESSIONAL_CODE;
}
```

(g) Replace `openNew` and `openBlock` with:

```ts
  function openNew(): void {
    openCreate("new");
  }

  function openBlock(): void {
    openCreate("block");
  }

  /** Nova consulta / Bloquear. A clinic-wide viewer also gets the doctor list (spec §5.E); a locked doctor does not. */
  function openCreate(type: "new" | "block"): void {
    if (!canWrite(getState()) || !canCreate(getState().viewer)) return;
    const locked = lockedProfessional(getState().viewer) !== null;
    const pros: CreateProChoices = locked ? { status: "idle" } : { status: "loading" };
    dispatch({
      type: "open_modal",
      modal: type === "new" ? { type: "new", pending: false, error: null, pros } : { type: "block", pending: false, error: null, pros },
    });
    if (!locked) void loadCreatePros(type);
  }

  async function loadCreatePros(modal: "new" | "block"): Promise<void> {
    const s = await fresh();
    if (!s) return;
    try {
      const pros = await hub.getProfessionals(s);
      dispatch(scoped(s.tenantId, { type: "create_pros_loaded", modal, pros: createProChoicesFrom(pros) }));
    } catch (e) {
      console.error("agenda: failed to load professionals for a new appointment", errorFacts(e));
      noticeSwitch(e);
      dispatch(scoped(s.tenantId, { type: "create_pros_loaded", modal, pros: { status: "failed" } }));
    }
  }
```

(h) In `createAppt`, replace

```ts
    const locked = lockedProfessional(getState().viewer);
```

with

```ts
    const locked = lockedProfessional(getState().viewer);
    // A locked doctor's own id always wins (R7 would refuse another); else the picked doctor.
    const professionalId = locked ? locked.id : data.professionalId;
```

replace `...(locked ? { professional_id: locked.id } : {}),` with

```ts
        ...(professionalId ? { professional_id: professionalId } : {}),
```

and in its `catch (e)` block, after `await onWriteError(s, e, createErrorText(e, CREATE_FAILED));` add:

```ts
      if (isUnknownProfessional(e)) void loadCreatePros("new");
```

(i) In `createBlock`: after `const locked = lockedProfessional(getState().viewer);` add

```ts
    const professionalId = locked ? locked.id : data.professionalId;
```

replace `...(locked ? { professional_id: locked.id } : {}),` with

```ts
        ...(professionalId ? { professional_id: professionalId } : {}),
```

and after `await onWriteError(s, e, createErrorText(e, BLOCK_FAILED));` add

```ts
      if (isUnknownProfessional(e)) void loadCreatePros("block");
```

- [ ] **Step 6: The sheets**

In `components/agenda/Modals.tsx`:

(a) Add the import:

```ts
import {
  CREATE_PRO_COPY,
  initialProChoice,
  proChoiceReady,
  proSelectOptions,
  professionalIdOf,
  type CreateProChoices,
} from "../../lib/agenda/create-professional";
```

(b) After the `LockedProfessional` component add:

```tsx
/** A clinic-wide viewer picks the doctor (TASK-046 R8, spec §5.E). Disabled while the list loads. */
function ProfessionalPicker({ pros, value, onChange, disabled }: { pros: CreateProChoices; value: string; onChange: (v: string) => void; disabled: boolean }) {
  const loading = pros.status === "idle" || pros.status === "loading";
  return (
    <>
      <Select label={CREATE_PRO_COPY.label} value={value} onChange={onChange} options={proSelectOptions(pros)} disabled={disabled || loading} />
      {pros.status === "failed" && <p className="agm-hint" role="status">{CREATE_PRO_COPY.failed}</p>}
    </>
  );
}

/** The picker's value: initial from the list and the viewer, reset when the sheet opens or the list changes. */
function useProChoice(open: boolean, pros: CreateProChoices | undefined, ownProfessionalId: string | null | undefined) {
  const [pro, setPro] = useState(() => (pros ? initialProChoice(pros, ownProfessionalId ?? null) : ""));
  useEffect(() => {
    if (open && pros) setPro(initialProChoice(pros, ownProfessionalId ?? null));
  }, [open, pros, ownProfessionalId]);
  return [pro, setPro] as const;
}
```

(c) `NewApptModalProps` and `BlockModalProps` — add to each, after `lockedProfessional?: …;`:

```ts
  /** The doctor list for a clinic-wide viewer (TASK-046 R8). Absent = no picker (locked doctor, or a caller without one). */
  pros?: CreateProChoices;
  /** The viewer's own professional, so a manager-doctor starts on himself. */
  ownProfessionalId?: string | null;
```

(d) `NewApptModal` — add `pros, ownProfessionalId` to the destructured props; after `const [notes, setNotes] = useState("");` add:

```ts
  const [pro, setPro] = useProChoice(open, lockedProfessional ? undefined : pros, ownProfessionalId);
  const picking = !lockedProfessional && pros !== undefined;
```

replace `const valid = newApptValid(form) && !pending;` with

```ts
  const valid = newApptValid(form) && !pending && (!picking || proChoiceReady(pro));
  const pid = picking ? professionalIdOf(pro) : undefined;
```

replace the submit's `onClick={valid ? () => onCreate(buildNewAppt(form)) : undefined}` with

```tsx
onClick={valid ? () => onCreate({ ...buildNewAppt(form), ...(pid ? { professionalId: pid } : {}) }) : undefined}
```

and replace `<LockedProfessional pro={lockedProfessional} />` with

```tsx
      {lockedProfessional ? (
        <LockedProfessional pro={lockedProfessional} />
      ) : (
        picking && pros && <ProfessionalPicker pros={pros} value={pro} onChange={setPro} disabled={pending} />
      )}
```

(e) `BlockData` — replace with:

```ts
export type BlockData = { date: string; start: number; dur: number; reason: string; professionalId?: string };
```

(f) `BlockModal` — add `pros, ownProfessionalId` to the destructured props; after `const [reason, setReason] = useState<string>(BLOCK_REASONS[0]);` add:

```ts
  const [pro, setPro] = useProChoice(open, lockedProfessional ? undefined : pros, ownProfessionalId);
  const picking = !lockedProfessional && pros !== undefined;
```

replace `const valid = date !== "" && !pending;` with

```ts
  const valid = date !== "" && !pending && (!picking || proChoiceReady(pro));
  const pid = picking ? professionalIdOf(pro) : undefined;
```

replace `onClick={valid ? () => onCreate({ ...slot, reason }) : undefined}` with

```tsx
onClick={valid ? () => onCreate({ ...slot, reason, ...(pid ? { professionalId: pid } : {}) }) : undefined}
```

and replace `<LockedProfessional pro={lockedProfessional} />` with:

```tsx
      {lockedProfessional ? (
        <LockedProfessional pro={lockedProfessional} />
      ) : (
        picking && pros && <ProfessionalPicker pros={pros} value={pro} onChange={setPro} disabled={pending} />
      )}
```

- [ ] **Step 7: Pass the list from the view**

In `components/agenda/AgendaView.tsx`, add the import

```ts
import { CREATE_PROS_IDLE } from "@/lib/agenda/create-professional";
```

and add to `<NewApptModal …>`:

```tsx
        pros={locked ? undefined : modal?.type === "new" ? modal.pros ?? CREATE_PROS_IDLE : CREATE_PROS_IDLE}
        ownProfessionalId={state.viewer.professionalId}
```

and to `<BlockModal …>`:

```tsx
        pros={locked ? undefined : modal?.type === "block" ? modal.pros ?? CREATE_PROS_IDLE : CREATE_PROS_IDLE}
        ownProfessionalId={state.viewer.professionalId}
```

- [ ] **Step 8: Run the tests**

Run: `npx vitest run lib/agenda components/agenda`
Expected: PASS — including R5's "a clinic-wide viewer's create sends no doctor (today's behaviour)" (its `NEW_APPT` has no `professionalId`) and the existing BlockModal tests (no `pros` prop = no picker, submit enabled as before).

- [ ] **Step 9: Typecheck and commit**

Run: `npm run typecheck` — Expected: no errors.

```bash
git add lib/agenda/secretaria-hub-agenda.ts lib/agenda/screen.ts components/agenda/Modals.tsx components/agenda/AgendaView.tsx lib/agenda/__tests__/screen.test.ts components/agenda/__tests__/modals.test.tsx components/agenda/__tests__/agenda-view.test.tsx
git commit -m "feat(agenda): clinic-wide viewers pick the doctor in Nova consulta/Bloquear (TASK-046 R8)

Active professionals or 'Sem profissional definido'; must choose when several doctors;
a manager-doctor starts on himself; a restricted doctor stays locked (R5). 422
unknown_professional shows the backend's sentence and re-reads the list.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7 (Part B close): full validation and checkpoint

**Files:**
- Create: `docs/CHECKPOINT_meus_pacientes_seletor_medico.md`
- Modify: `CLAUDE.md` (one pointer line at the top block of status lines)

**Interfaces:**
- Consumes: Tasks 3–6; Part A's checkpoint.
- Produces: the record the integrator and deployer read.

- [ ] **Step 1: Full validation**

Run, in order: `npm test`, `npm run typecheck`, `npm run build`. Record the test count. A failure that also fails on the base commit (`main` after R5) is pre-existing — prove it by running the same command in a throwaway checkout of that commit; anything else is a regression to fix now. Do **not** run `npm run lint`.

- [ ] **Step 2: Manual check in the mock (optional, no backend needed)**

`npm run dev` with the mock api: the console shows no "Meus pacientes" chip (the mock `Me` has no `ownProfessionalId`) — confirms the chip never appears for a non-doctor. Real-data proof is a post-deploy step (record it as pending).

- [ ] **Step 3: Write the checkpoint**

Create `docs/CHECKPOINT_meus_pacientes_seletor_medico.md`:

```markdown
# CHECKPOINT — "Meus pacientes" no console e escolha do médico na agenda (TASK-046 R8, parte B)

Spec: `secretarIA/docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.D e §5.E.
Plano: `secretarIA/docs/superpowers/plans/2026-10-09-lembretes-r8-filtro-meus-pacientes-e-seletor-medico.md`.
Backend: `secretarIA/docs/CHECKPOINT_conversas_meus_pacientes.md`.

## Estado
- Local, commitado na branch `task/TASK-046-meus-pacientes-front` (worktree `BRAIN-worktrees/TASK-046/Brain-Message-Frontend`),
  em cima do R5 mesclado. Não mesclado, não pushado, não deployado.
- `npm test`: <N> passed; `npm run typecheck` e `npm run build` verdes (<falhas pré-existentes com prova, se houver>).
- Prova com dados reais: pendente (depois do deploy da secretarIA e deste front).

## Console — "Meus pacientes"
- Botão de filtro só para quem é médico (`Me.ownProfessionalId`, lido de `GET /tenants/me/calendar/viewer` em
  `lib/real/console-api.real.ts::me` via `lib/real/console-my-patients.ts::fetchOwnProfessionalId`; falha = sem botão).
- Ligado: `ChatScreen` busca `GET /tenants/me/conversations?mine=true` (só os ids) no mesmo ritmo da lista e
  `lib/conversations.ts::applyConversationFilter` filtra; combina com Todas/Aguardando/Não lidas/Minhas e com a busca.
  Conversas do PreCheck saem enquanto ligado. Enquanto os ids não chegam: spinner, nunca "nenhum paciente".
- Filtro, nunca permissão: desligado, todo mundo vê a clínica inteira.

## Agenda — escolha do médico
- "Nova consulta" e "Bloquear horário": quem vê a clínica toda escolhe entre os médicos ativos
  (`AgendaHub.getProfessionals`, o mesmo do Editar/Remarcar) ou "Sem profissional definido".
  Regras puras em `lib/agenda/create-professional.ts`; carga em `lib/agenda/screen.ts::openCreate/loadCreatePros`.
- Com 2+ médicos é preciso escolher; gestor-médico começa nele mesmo; clínica de 1 médico começa nele; lista
  vazia/falhou → "Sem profissional definido".
- Médico restrito: continua travado nele (R5), a lista nem é pedida.
- 422 `unknown_professional`: frase do servidor no formulário e a lista é relida.

## Deploy (não executado)
`secretaria_api` (parte A) antes deste front. Front antes da API = "Meus pacientes" mostraria todas as conversas.

## Observação
- O chip "Minhas" (atribuídas a mim) segue como antes; nos dados reais `assignedToUserId` é sempre nulo,
  então ele fica vazio — fora do escopo do R8.
```

Replace `<N>` and the failures line with the real values from Step 1.

- [ ] **Step 4: Pointer in CLAUDE.md**

At the top of `CLAUDE.md`, next to the other TASK status lines (e.g. after the TASK-039 line), add:

```markdown
TASK-046 R8: "Meus pacientes" no console (só para médicos; filtro, não permissão) e escolha do médico em Nova consulta/Bloquear — [CHECKPOINT_meus_pacientes_seletor_medico](docs/CHECKPOINT_meus_pacientes_seletor_medico.md). Local, não deployado; secretarIA antes.
```

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_meus_pacientes_seletor_medico.md CLAUDE.md
git commit -m "docs: checkpoint for 'Meus pacientes' and the agenda doctor picker (TASK-046 R8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review (done while writing)

- **Spec coverage:** §5.D everyone sees all (Task 1 default-list pin, no other route touched); chip only for doctors (Tasks 3–4); status rule decided and pinned (Task 1); PreCheck behaviour (Task 3). §5.E picker for clinic-wide viewers with active doctors + "sem profissional" (Tasks 5–6); restricted doctor locked (Task 6 test); backend contract verified in code (`_creation_professional`, existing R7 tests) — no backend change needed for §5.E.
- **Placeholders:** only the checkpoint `<N>`/failures values, which Steps instruct to fill from the real run.
- **Type consistency:** `CreateProChoices`, `initialProChoice(choices, ownProfessionalId: string | null)`, `professionalIdOf`, `proChoiceReady`, `CREATE_PROS_IDLE`, `create_pros_loaded { modal, pros }`, `BlockInput/BlockData.professionalId?`, `Me.ownProfessionalId?`, `ConversationFilter.myPatients?`, `listMyPatientConversationIds?`, `applyConversationFilter(list, filter, currentUserId, myPatientIds?)` are spelled the same in every task.
