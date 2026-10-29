"""The agenda mark of a booking confirmed WITHOUT the deposit (TASK-071, Plan 3, task 6).

`appointments.deposit_waived_reason` holds the closed `FailureReason` of the technical failure
that made the booking go ahead without its deposit (the booking paths are pinned in
`tests/test_booking_gate_deposit_fallback.py`). Here: the agenda read the Portal shows
(`GET /tenants/me/calendar/events`, two TOP-LEVEL keys next to `deposit` - ruling R9), the
closed-vocabulary helpers and the additive migration.
"""

import contextlib
import io
import pathlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant, require_clinic_clinician
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Tenant
from secretaria.services.payments import failure_reason
from secretaria.services.payments.failure_reason import FailureReason, user_text
from secretaria.services.tenant_config import set_google_refresh_token
from tests.before_support import make_world
from tests.payments_support import FakeArqPool, make_sessionmaker

REPO = pathlib.Path(__file__).resolve().parent.parent
REVISION = "a7c3e9f1d5b8"
PREVIOUS_HEAD = "d4f7a1c9e2b6"
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(require_clinic_clinician, None)
    app.state.arq_pool = None


def _wire(db, tenant_id):
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    app.dependency_overrides[require_clinic_clinician] = _tenant
    app.state.arq_pool = FakeArqPool()


class Calendar:
    events: list[dict] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def check_availability(self, start, end, max_results=50):
        return list(Calendar.events)

    async def list_events(self, start, end):
        return list(Calendar.events)


async def test_agenda_events_carry_the_waived_reason_and_its_text(
    client: AsyncClient, db, monkeypatch
):
    async with db() as session:
        world = await make_world(session)
        await set_google_refresh_token(session, world.tenant_id, "fake")
        rows = {
            "evt-waived": "pix_key_missing",
            "evt-stray": "not_a_reason",  # never written by the code; read safely as `unknown`
            "evt-plain": None,
        }
        for event_id, reason in rows.items():
            session.add(
                Appointment(
                    id=uuid4(), tenant_id=world.tenant_id, google_event_id=event_id,
                    appointment_type="Consulta", status=AppointmentStatus.SCHEDULED,
                    deposit_waived_reason=reason,
                )
            )
        await session.commit()
    _wire(db, world.tenant_id)
    Calendar.events = [
        {"id": event_id, "summary": "A", "start": NOW.isoformat(), "end": NOW.isoformat()}
        for event_id in ("evt-waived", "evt-stray", "evt-plain", "evt-typed-in-google")
    ]
    monkeypatch.setattr(hub_calendar, "CalendarService", Calendar)

    response = await client.get(
        "/tenants/me/calendar/events",
        params={"start": NOW.isoformat(), "end": (NOW + timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 200
    by_id = {
        e["id"]: (e["deposit_waived_reason"], e["deposit_waived_text"]) for e in response.json()
    }
    assert by_id["evt-waived"] == (
        "pix_key_missing",
        user_text(FailureReason.pix_key_missing),
    )
    assert by_id["evt-stray"] == ("unknown", user_text(FailureReason.unknown))
    # An ordinary booking and an event with no local appointment: both keys present, null.
    assert by_id["evt-plain"] == (None, None)
    assert by_id["evt-typed-in-google"] == (None, None)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("qr_unavailable", FailureReason.qr_unavailable),
        (FailureReason.no_account, FailureReason.no_account),
        ("Asaas said: something", FailureReason.unknown),
    ],
)
def test_coerce_keeps_the_vocabulary_closed(value, expected):
    assert failure_reason.coerce(value) == expected


def test_text_for_is_the_user_text_or_none():
    assert failure_reason.text_for(None) is None
    assert failure_reason.text_for("rate_limited") == user_text(FailureReason.rate_limited)
    assert failure_reason.text_for("bogus") == user_text(FailureReason.unknown)


def test_the_model_column_is_a_short_nullable_string():
    column = Appointment.__table__.c["deposit_waived_reason"]
    assert column.type.length == 32 and column.nullable is True
    # Every FailureReason value fits.
    assert max(len(reason.value) for reason in FailureReason) <= 32


# --- migration -------------------------------------------------------------------------------


def _config() -> Config:
    # No ini FILE on purpose: `migrations/env.py` runs `logging.config.fileConfig` for one, which
    # disables every logger created so far - and this module sorts before the tests that read
    # the worker's log output (`test_arq_log_secret_redaction.py`).
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline(direction: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if direction == "up":
            command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
        else:
            command.downgrade(_config(), f"{REVISION}:{PREVIOUS_HEAD}", sql=True)
    return out.getvalue()


def test_the_revision_is_chained_after_the_charge_health_one():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in script.walk_revisions(head=heads[0])}


def test_upgrade_only_adds_a_nullable_column_and_downgrade_drops_it():
    up = _offline("up").lower()
    assert "alter table appointments add column deposit_waived_reason varchar(32)" in up
    assert "drop" not in up and "not null" not in up
    assert "update appointments" not in up  # no backfill
    down = _offline("down").lower()
    assert "alter table appointments drop column deposit_waived_reason" in down
