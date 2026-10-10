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
    assert (echoed["reminder_extra_days_before"], echoed["reminder_extra_send_time"]) == (
        3,
        "08:15",
    )
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
        json={
            "tenant": {"reminder_extra_days_before": days, "reminder_extra_send_time": send_time}
        },
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, days, send_time, days * 1440)


async def test_a_legacy_lead_from_an_older_screen_is_translated_keeping_the_hour(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    await _set(db, tenant.id, reminder_extra_send_time="07:30")

    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2881}}
    )

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
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
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
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    appointment, _ = await _planned_custom_due(db, tenant, start)

    response = await client.put(CONFIGURATION, json={"tenant": {"timezone": "America/Manaus"}})

    assert response.status_code == 200, response.text
    local = (await _custom_due(db, appointment.id)).astimezone(MANAUS)
    assert local.time() == time(9, 0)


async def test_saves_that_change_nothing_do_not_replan(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db,  # noqa: F811
    tenant,  # noqa: F811
):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    await _planned_custom_due(db, tenant, start)
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


@pytest.mark.parametrize("legacy", [1440, 999999, "old-invalid-value"])
async def test_the_new_pair_ignores_even_an_invalid_legacy_key(client: AsyncClient, legacy):
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {
                "reminder_extra_days_before": 4,
                "reminder_extra_send_time": "10:00",
                "reminder_extra_lead_minutes": legacy,
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["tenant"]["reminder_extra_lead_minutes"] == 5760


@pytest.mark.parametrize(
    "patch",
    [
        {"reminder_extra_days_before": 5, "reminder_extra_send_time": "09:00"},
        {"reminder_extra_lead_minutes": 7200},
    ],
)
async def test_saving_migrated_settings_moves_pending_legacy_due_even_when_the_pair_is_unchanged(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,  # noqa: F811
    patch,
):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=20, minute=30, second=0, microsecond=0
    )
    appointment, _ = await _planned_custom_due(db, tenant, start)
    legacy_due = start - timedelta(days=5)  # 17:30 local, as R7 planned it.
    async with db() as session:
        row = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appointment.id,
                AppointmentReminder.kind == "custom",
            )
        )
        row.due_at = legacy_due
        row.warn_due_at = legacy_due + timedelta(hours=2)
        reminder_id = row.id
        await session.commit()

    response = await client.put(CONFIGURATION, json={"tenant": patch})

    assert response.status_code == 200
    expected_due = start.replace(hour=12, minute=0) - timedelta(days=5)  # 09:00 local.
    async with db() as session:
        row = await session.get(AppointmentReminder, reminder_id)
        assert reminder_schedule._as_utc(row.due_at) == expected_due
        assert reminder_schedule._as_utc(row.warn_due_at) == expected_due + timedelta(hours=2)
        assert row.status == "pending"
    assert await _stored(db, tenant.id) == (True, 5, "09:00", 7200)
