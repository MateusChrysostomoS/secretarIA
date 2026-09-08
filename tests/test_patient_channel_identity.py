"""Patient identity by channel - the additive half.

This pins the property that makes the `channel` / `external_id` migration safe
to ship on its own: the WhatsApp path does not change. Not "still works" -
does not change. `workers/tasks.py` was not touched, still builds a Patient
from `tenant_id`/`wa_id`/`name` alone, and must still produce byte-for-byte the
same row it produced before, plus two columns that fill themselves.

The other half is the new channel proving it has room: a Brain-Message patient
has NO phone number, so its row carries `wa_id=None` and must not collide with
anything - including the legacy `(tenant_id, wa_id)` constraint, which this
migration deliberately KEEPS. NULLs compare distinct, so it does not bind such
rows; that is load-bearing, not incidental, and is tested here.

DB layer follows tests/test_lgpd_consent_gate.py: a real aiosqlite engine on
StaticPool monkeypatched over `workers.tasks.async_session_factory`, then the
actual call site is invoked. A model-only test would pass straight through a
broken call site.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Patient, Tenant  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"

# Every column `patients` had BEFORE this migration. The point of listing them
# by hand is that the test breaks if a future change quietly alters one of them
# while claiming to be additive.
LEGACY_COLUMNS = (
    "wa_id",
    "name",
    "reminder_opt_out",
    "asaas_customer_id",
    "lgpd_accepted_at",
)


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


@pytest.fixture(autouse=True)
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    yield


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _inbound(tenant: Tenant, body: str, wam_id: str):
    return await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=wam_id,
        body=body,
    )


# --------------------------------------------------------------------------
# The WhatsApp path is unchanged
# --------------------------------------------------------------------------


async def test_whatsapp_upsert_fills_channel_and_external_id(db) -> None:
    """The untouched call site produces a complete channel-addressable row."""
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "Oi", "wamid.1")

    async with db() as session:
        patients = (await session.scalars(select(Patient))).all()

    assert len(patients) == 1
    patient = patients[0]
    # The two new columns, neither of them passed by workers/tasks.py.
    assert patient.channel == "whatsapp"
    assert patient.external_id == WA_ID
    # ... and the legacy columns exactly as before: same wa_id, same captured
    # name, and every optional field still at its pre-migration default.
    legacy = {column: getattr(patient, column) for column in LEGACY_COLUMNS}
    assert legacy == {
        "wa_id": WA_ID,
        "name": "Maria",
        "reminder_opt_out": False,
        "asaas_customer_id": None,
        "lgpd_accepted_at": None,
    }


async def test_second_message_reuses_the_same_patient_row(db) -> None:
    """Identity by wa_id still resolves to ONE row, not a new one per message.

    The migration changes what the DB considers a patient's identity. If the
    lookup and the constraint ever disagreed, this is where it would show: a
    second message from the same number would either duplicate the patient or
    raise. It must do neither.
    """
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "Oi", "wamid.1")

    async with db() as session:
        first_id = (await session.scalars(select(Patient))).one().id

    await _inbound(tenant, "Ainda eu", "wamid.2")

    async with db() as session:
        rows = (await session.scalars(select(Patient))).all()

    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].external_id == WA_ID


async def test_same_number_in_two_tenants_stays_two_patients(db) -> None:
    """Tenant isolation survives the new key: the same phone, two clinics."""
    tenant_a = await _seed_tenant(db)
    async with db() as session:
        tenant_b = Tenant(
            id=uuid4(),
            clinic_name="Clinic B",
            phone_number_id="9999999999",
            is_active=True,
            clinic_description="Dermatologia.",
            initial_flows={},
        )
        session.add(tenant_b)
        await session.commit()
        await session.refresh(tenant_b)

    await _inbound(tenant_a, "Oi", "wamid.a")
    await _inbound(tenant_b, "Oi", "wamid.b")

    async with db() as session:
        total = await session.scalar(select(func.count()).select_from(Patient))
    assert total == 2


# --------------------------------------------------------------------------
# The new channel has room
# --------------------------------------------------------------------------


async def test_brain_message_patient_needs_no_wa_id(db) -> None:
    """A patient with no phone number gets a row - the case that was impossible."""
    tenant = await _seed_tenant(db)

    async with db() as session:
        session.add(
            Patient(
                tenant_id=tenant.id,
                wa_id=None,
                channel="brain_message",
                external_id="qualquer-coisa",
                name="Ana",
            )
        )
        await session.commit()

    async with db() as session:
        patient = (await session.scalars(select(Patient))).one()

    assert patient.wa_id is None
    assert patient.channel == "brain_message"
    assert patient.external_id == "qualquer-coisa"


async def test_two_wa_id_less_patients_do_not_collide(db) -> None:
    """The RETAINED legacy constraint must not bind rows with a NULL wa_id.

    `uq_patients_tenant_wa_id` is kept on purpose (it guards the window where
    the arq worker still runs the pre-channel model). Keeping it would be a
    silent cap of one Brain-Message patient per clinic if NULLs collided - so
    this asserts the SQL semantics the retention depends on.
    """
    tenant = await _seed_tenant(db)

    async with db() as session:
        session.add_all(
            [
                Patient(tenant_id=tenant.id, channel="brain_message", external_id="sess-a"),
                Patient(tenant_id=tenant.id, channel="brain_message", external_id="sess-b"),
            ]
        )
        await session.commit()

    async with db() as session:
        total = await session.scalar(select(func.count()).select_from(Patient))
    assert total == 2


async def test_duplicate_external_id_in_one_channel_is_rejected(db) -> None:
    """The new constraint bites - otherwise it is decoration, not identity."""
    tenant = await _seed_tenant(db)

    async with db() as session:
        session.add(Patient(tenant_id=tenant.id, channel="brain_message", external_id="sess-a"))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with db() as session:
            session.add(Patient(tenant_id=tenant.id, channel="brain_message", external_id="sess-a"))
            await session.commit()


async def test_channel_separates_identical_external_ids(db) -> None:
    """Same identifier string, different channels: two distinct people.

    Nothing forces a Brain-Message external_id to avoid looking like a phone
    number, so the channel has to be part of the key rather than decoration on
    the side of it.
    """
    tenant = await _seed_tenant(db)

    async with db() as session:
        session.add_all(
            [
                Patient(tenant_id=tenant.id, wa_id=WA_ID),
                Patient(tenant_id=tenant.id, channel="brain_message", external_id=WA_ID),
            ]
        )
        await session.commit()

    async with db() as session:
        rows = (await session.scalars(select(Patient).order_by(Patient.channel))).all()

    assert [row.channel for row in rows] == ["brain_message", "whatsapp"]
    assert all(row.external_id == WA_ID for row in rows)
