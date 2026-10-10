"""The extra reminder on the hub configuration (TASK-044 R7, spec §5.B; R5 Tasks 9-10 shape)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402

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


async def _stored(db, tenant_id) -> tuple[bool, int | None]:  # noqa: F811
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        return row.reminders_v2_enabled, row.reminder_extra_lead_minutes


async def test_get_config_exposes_the_switch_and_the_lead(client: AsyncClient, tenant):  # noqa: F811
    body = (await client.get(CONFIG)).json()

    assert body["reminders_v2_enabled"] is True
    assert body["reminder_extra_lead_minutes"] == 7200


async def test_the_configuration_save_changes_the_lead_and_echoes_it(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2880}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["reminder_extra_lead_minutes"] == 2880
    assert await _stored(db, tenant.id) == (True, 2880)


async def test_null_switches_it_off_on_the_legacy_save_too(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(CONFIG, json={"reminder_extra_lead_minutes": None})

    assert response.status_code == 200
    assert response.json()["reminder_extra_lead_minutes"] is None
    assert await _stored(db, tenant.id) == (True, None)


@pytest.mark.parametrize("value", [1440, 1499, 20161, 0, -5, "dois dias"])
async def test_out_of_range_is_422_and_nothing_changes(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
    value,  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": value}}
    )

    assert response.status_code == 422
    assert await _stored(db, tenant.id) == (True, 7200)


@pytest.mark.parametrize("value", [1500, 20160])
async def test_the_bounds_themselves_are_accepted(client: AsyncClient, db, tenant, value):  # noqa: F811
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": value}}
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, value)


async def test_the_switch_is_never_written_by_the_hub(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminders_v2_enabled": False, "persona_notes": "Gentil"}},
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, 7200)


async def test_a_saved_lead_moves_the_planned_extra_reminder_in_the_same_save(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    now = datetime.now(UTC)
    start = (now + timedelta(days=10)).replace(second=0, microsecond=0)
    appointment = await make_appointment(db, tenant, start_at=start)
    async with db() as session:
        clinic = await session.get(Tenant, tenant.id)
        row = await session.get(Appointment, appointment.id)
        await reminder_schedule.schedule_reminders(session, row, clinic, now=now)
        await session.commit()

    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2880}}
    )

    assert response.status_code == 200
    async with db() as session:
        custom = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appointment.id,
                AppointmentReminder.kind == "custom",
            )
        )
    assert reminder_schedule._as_utc(custom.due_at) == start - timedelta(minutes=2880)


async def test_a_save_that_keeps_the_lead_does_not_replan(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []

    async def _spy(*args, **kwargs):
        calls.append(1)
        return reminder_schedule.CustomReplan()

    monkeypatch.setattr(reminder_schedule, "replan_custom_reminders", _spy)

    await client.put(CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 7200}})
    await client.put(CONFIGURATION, json={"tenant": {"persona_notes": "Gentil"}})

    assert calls == []
