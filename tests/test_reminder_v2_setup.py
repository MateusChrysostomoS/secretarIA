"""R2 settings and the shared helpers (TASK-032 R2)."""

from datetime import timedelta

from secretaria.config import get_settings
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, add_reminder, get_reminder, seed_world


def test_r2_settings_defaults():
    settings = get_settings()
    assert settings.REMINDER_V2_TEMPLATE_NAME == "lembrete_consulta_v2"
    assert settings.REMINDER_V2_TEMPLATE_APPROVED is False
    assert settings.REMINDER_V2_BATCH_SIZE == 200
    assert settings.BRAIN_MESSAGE_PORTAL_URL == ""


async def test_seed_world_builds_a_v2_clinic_with_a_planned_row(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, due_at=NOW - timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert world.tenant.reminders_v2_enabled is True
    assert world.appointment.confirmation_count == 0
    assert row.status == "pending" and row.kind == "day" and row.attempts == 0
