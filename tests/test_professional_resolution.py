"""Tests for the shared professional-resolution service (PROMPT 1, multi-doctor flow).

Covers the pieces PROMPT 2/3 build on:
  - the new nullable columns default to NULL on freshly-created rows
    (conversations.flow_selected_professional_id / flow_selected_insurance,
    appointments.insurance);
  - services/tenant_config.py::resolve_professional_calendar resolves the
    professional's OWN token/hours/duration and falls back to the tenant's,
    exactly like plugins/multi_professional.py's `_professional_calendar`
    always did (that function now delegates here — its own tests in
    test_multi_professional_plugin.py keep covering the LLM-tool path).

In-memory-sqlite pattern from test_multi_professional_plugin.py.
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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    Conversation,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import tenant_config as tc  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    TenantRuntimeConfig,
    list_active_professionals,
    resolve_professional_calendar,
    set_professional_google_refresh_token,
)

_TENANT_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}], "tuesday": []}
_TENANT_TYPES = [{"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}]
_OWN_HOURS = {"tuesday": [{"start": "09:00", "end": "17:00"}]}
_OWN_TYPES = [{"name": "Retorno", "duration_min": 45, "is_active": True, "sort_order": 0}]


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


class _FakeCalendarService:
    """Records the for_professional overrides the resolver passes through."""

    last_call: dict | None = None

    @classmethod
    def for_professional(cls, tenant_config, **overrides):
        cls.last_call = {"tenant_config": tenant_config, **overrides}
        return cls()


@pytest.fixture(autouse=True)
def _fake_calendar(monkeypatch: pytest.MonkeyPatch):
    _FakeCalendarService.last_call = None
    monkeypatch.setattr(tc, "CalendarService", _FakeCalendarService)
    yield


def _tenant_config(tenant_id, refresh_token="tenant-refresh-token") -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinic",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="tenant-calendar",
        google_refresh_token=refresh_token,
    )


async def _seed(db, *, own_config: bool, mode: str = "per_professional"):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            business_hours=_TENANT_HOURS,
            appointment_types=_TENANT_TYPES,
            google_calendar_mode=mode,
        )
        session.add(tenant)
        await session.flush()
        professional = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            google_calendar_id="ana-calendar" if own_config else None,
            business_hours=_OWN_HOURS if own_config else None,
            appointment_types=_OWN_TYPES if own_config else None,
            is_active=True,
        )
        session.add(professional)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(professional)
        return tenant, professional


# --------------------------------------------------------------------------
# New columns: NULL by default on freshly-created rows
# --------------------------------------------------------------------------


async def test_new_columns_default_null(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="555000111")
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        appointment = Appointment(
            tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-1"
        )
        session.add_all([conversation, appointment])
        await session.commit()
        await session.refresh(conversation)
        await session.refresh(appointment)

        assert conversation.flow_selected_professional_id is None
        assert conversation.flow_selected_insurance is None
        assert appointment.insurance is None


async def test_google_calendar_mode_defaults_to_per_professional(db):
    """A tenant row created without specifying google_calendar_mode (every
    pre-existing tenant, and any new one the hub creates without touching
    this field) reads back as "per_professional" - the migration's
    server_default and the model's Python-side default must agree."""
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="555000222")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        assert tenant.google_calendar_mode == "per_professional"


# --------------------------------------------------------------------------
# list_active_professionals
# --------------------------------------------------------------------------


async def test_list_active_professionals_filters_and_orders(db):
    tenant, ana = await _seed(db, own_config=False)
    async with db() as session:
        session.add_all(
            [
                Professional(tenant_id=tenant.id, name="Dr. Bruno", is_active=True),
                Professional(tenant_id=tenant.id, name="Dr. Inativo", is_active=False),
            ]
        )
        await session.commit()

    async with db() as session:
        rows = await list_active_professionals(session, tenant.id)
    assert [p.name for p in rows] == ["Dr. Bruno", "Dra. Ana"]


# --------------------------------------------------------------------------
# resolve_professional_calendar: own config vs tenant fallback
# --------------------------------------------------------------------------


async def test_resolver_uses_professionals_own_config(db):
    tenant, ana = await _seed(db, own_config=True)
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-own-refresh-token")
        await session.commit()

    config = _tenant_config(tenant.id)
    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana, tenant_config=config)

    call = _FakeCalendarService.last_call
    assert call is not None
    assert call["tenant_config"] is config
    assert call["google_calendar_id"] == "ana-calendar"
    assert call["google_refresh_token"] == "ana-own-refresh-token"
    assert call["business_hours"] == _OWN_HOURS
    # First active own service (45 min) drives the default slot duration.
    assert call["appointment_duration_min"] == 45


async def test_resolver_falls_back_to_tenant(db):
    tenant, ana = await _seed(db, own_config=False)

    config = _tenant_config(tenant.id)
    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana, tenant_config=config)

    call = _FakeCalendarService.last_call
    assert call is not None
    # No own calendar id -> None, so for_professional keeps the tenant's own.
    assert call["google_calendar_id"] is None
    # No own credential -> the tenant's decrypted token from the config.
    assert call["google_refresh_token"] == "tenant-refresh-token"
    # No own hours/services -> the tenant's, active-filtered (empty days dropped),
    # and the tenant's first active service drives the duration.
    assert call["business_hours"] == {"monday": [{"start": "08:00", "end": "12:00"}]}
    assert call["appointment_duration_min"] == 30


async def test_resolver_loads_tenant_config_when_not_passed(db):
    tenant, ana = await _seed(db, own_config=True)

    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana)

    call = _FakeCalendarService.last_call
    assert call is not None
    # Loaded via load_tenant_config: no tenant credential row -> token None,
    # so the professional's (also absent) own token resolves to None overall.
    assert call["tenant_config"].tenant_id == tenant.id
    assert call["google_refresh_token"] is None
    assert call["google_calendar_id"] == "ana-calendar"



# --------------------------------------------------------------------------
# google_calendar_mode routing rule (docs/CHECKPOINT_google_calendar_modes.md
# item 4): shared_account ALWAYS uses the clinic's own token for a
# professional-scoped Calendar operation, even when the professional also
# has their own connected token - a shared_account google_calendar_id only
# exists under the clinic's account, so pairing it with the professional's
# own token would fail.
# --------------------------------------------------------------------------


async def test_shared_account_mode_uses_clinic_token_even_with_own_token(db):
    tenant, ana = await _seed(db, own_config=True, mode="shared_account")
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-own-refresh-token")
        await session.commit()

    config = _tenant_config(tenant.id, refresh_token="clinic-refresh-token")
    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana, tenant_config=config)

    call = _FakeCalendarService.last_call
    assert call is not None
    # The calendar id still passes through unchanged (it's the one created
    # under the clinic's account)...
    assert call["google_calendar_id"] == "ana-calendar"
    # ...but paired with the CLINIC's token, never Ana's own.
    assert call["google_refresh_token"] == "clinic-refresh-token"


async def test_shared_account_mode_without_calendar_id_yet_still_uses_clinic_token(db):
    """Before a secondary calendar has been created for this professional
    (google_calendar_id still None), shared_account mode already stops using
    any leftover own token - booking must land under the clinic's own
    account (its own calendar_id, via for_professional's None-keeps-tenant's-
    own substitution), never the professional's own account."""
    tenant, ana = await _seed(db, own_config=False, mode="shared_account")
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-own-refresh-token")
        await session.commit()

    config = _tenant_config(tenant.id, refresh_token="clinic-refresh-token")
    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana, tenant_config=config)

    call = _FakeCalendarService.last_call
    assert call is not None
    assert call["google_calendar_id"] is None  # not created yet -> tenant's own kept
    assert call["google_refresh_token"] == "clinic-refresh-token"


async def test_per_professional_mode_explicit_still_uses_own_token(db):
    """Regression guard, explicit rather than relying on the default: with
    google_calendar_mode="per_professional" set, the combination this
    feature had to NOT break (own token + own, independently-configured
    calendar_id) keeps behaving exactly as it did before shared_account mode
    existed - same assertions as test_resolver_uses_professionals_own_config."""
    tenant, ana = await _seed(db, own_config=True, mode="per_professional")
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-own-refresh-token")
        await session.commit()

    config = _tenant_config(tenant.id, refresh_token="clinic-refresh-token")
    async with db() as session:
        await resolve_professional_calendar(session, tenant, ana, tenant_config=config)

    call = _FakeCalendarService.last_call
    assert call is not None
    assert call["google_calendar_id"] == "ana-calendar"
    assert call["google_refresh_token"] == "ana-own-refresh-token"


async def test_resolver_calendar_factory_seam(db):
    """A caller-supplied factory receives the resolved overrides verbatim."""
    tenant, ana = await _seed(db, own_config=True)
    captured: dict = {}
    sentinel = object()

    def factory(**overrides):
        captured.update(overrides)
        return sentinel

    config = _tenant_config(tenant.id)
    async with db() as session:
        result = await resolve_professional_calendar(
            session, tenant, ana, tenant_config=config, calendar_factory=factory
        )

    assert result is sentinel
    assert captured["google_calendar_id"] == "ana-calendar"
    assert captured["business_hours"] == _OWN_HOURS
    assert captured["appointment_duration_min"] == 45
    # The factory path never constructs through CalendarService itself.
    assert _FakeCalendarService.last_call is None
