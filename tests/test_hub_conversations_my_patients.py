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
