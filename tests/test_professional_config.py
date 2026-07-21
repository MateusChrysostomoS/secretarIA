"""Professional-level encrypted credentials + per-professional completeness.

Covers the additions to services/tenant_config.py from the onboarding /
multi-professional data-layer round (cross-service contract v1 §3/§4):

1. `professional_credentials` round-trips through Fernet ciphertext, the same
   single-decrypt-seam guarantee `tenant_credentials` has
   (tenant-secrets-encryption skill) - and is independent of the tenant's own
   credential row.
2. `professional_business_hours` / `professional_appointment_types`: the
   professional's own JSON when set, else the tenant's (legacy fallback) -
   same active-filtering semantics as `active_business_hours` /
   `active_appointment_types`.
3. `professional_completeness` / `can_activate_professional_aware`: the
   partial-activation threshold rule (`PARTIAL_ACTIVATION_THRESHOLD`).
4. `tenants.phone_number_id`'s new partial unique index: many NULLs coexist,
   a connected (non-null) value still can't collide.

Uses in-memory aiosqlite; ENCRYPTION_KEY comes from conftest's deterministic env.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.config import get_settings
from secretaria.core.database import Base
from secretaria.models import Professional, ProfessionalCredentials, Tenant
from secretaria.services.tenant_config import (
    can_activate_professional_aware,
    clear_professional_google_refresh_token,
    get_professional_google_refresh_token,
    has_google_refresh_token,
    has_professional_google_refresh_token,
    load_tenant_config,
    professional_appointment_types,
    professional_business_hours,
    professional_completeness,
    professional_completeness_item,
    set_google_refresh_token,
    set_professional_google_refresh_token,
)

SECRET = "1//0g-super-secret-refresh-token"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_tenant(session, **overrides) -> Tenant:
    fields = dict(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    fields.update(overrides)
    tenant = Tenant(**fields)
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_professional(session, tenant: Tenant, **overrides) -> Professional:
    fields = dict(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    fields.update(overrides)
    professional = Professional(**fields)
    session.add(professional)
    await session.flush()
    return professional


_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
_TYPES = [{"name": "Consulta", "duration_min": 30, "is_active": True}]


# --------------------------------------------------------------------------
# professional_credentials round trip
# --------------------------------------------------------------------------


def test_professional_credentials_model_has_only_the_encrypted_column():
    """The load-bearing guarantee: no plaintext token/secret column on the table."""
    cols = ProfessionalCredentials.__table__.columns.keys()
    assert "google_refresh_token_encrypted" in cols
    secretish = [
        c for c in cols if ("token" in c or "secret" in c) and not c.endswith("_encrypted")
    ]
    assert secretish == []


async def test_professional_refresh_token_roundtrip_is_ciphertext_at_rest(session):
    tenant = await _make_tenant(session)
    professional = await _make_professional(session, tenant)

    assert await has_professional_google_refresh_token(session, professional.id) is False
    assert await get_professional_google_refresh_token(session, professional.id) is None

    await set_professional_google_refresh_token(session, professional.id, SECRET)
    await session.commit()

    # What PERSISTS is Fernet ciphertext, never the plaintext.
    cred = await session.get(ProfessionalCredentials, professional.id)
    assert cred.google_refresh_token_encrypted != SECRET
    assert SECRET not in cred.google_refresh_token_encrypted
    # The single seam returns the original value.
    assert await has_professional_google_refresh_token(session, professional.id) is True
    assert await get_professional_google_refresh_token(session, professional.id) == SECRET

    await clear_professional_google_refresh_token(session, professional.id)
    await session.commit()
    assert await get_professional_google_refresh_token(session, professional.id) is None


async def test_professional_credential_is_independent_of_tenant_credential(session):
    """A professional's own token must not leak into, or read from, the tenant's row."""
    tenant = await _make_tenant(session)
    professional = await _make_professional(session, tenant)

    await set_professional_google_refresh_token(session, professional.id, SECRET)
    await session.commit()

    assert await has_google_refresh_token(session, tenant.id) is False
    assert await has_professional_google_refresh_token(session, professional.id) is True


async def test_two_professionals_credentials_do_not_collide(session):
    tenant = await _make_tenant(session)
    a = await _make_professional(session, tenant, name="A")
    b = await _make_professional(session, tenant, name="B")

    await set_professional_google_refresh_token(session, a.id, "token-a")
    await session.commit()

    assert await get_professional_google_refresh_token(session, a.id) == "token-a"
    assert await get_professional_google_refresh_token(session, b.id) is None


# --------------------------------------------------------------------------
# professional_business_hours / professional_appointment_types fallback
# --------------------------------------------------------------------------


async def test_professional_hours_and_types_fall_back_to_tenant_when_null(session):
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    professional = await _make_professional(session, tenant)  # both columns NULL

    assert professional_business_hours(professional, tenant) == _HOURS
    assert professional_appointment_types(professional, tenant) == _TYPES


async def test_professional_hours_and_types_use_own_when_set(session):
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    own_hours = {"tuesday": [{"start": "09:00", "end": "17:00"}]}
    own_types = [{"name": "Retorno", "duration_min": 20, "is_active": True}]
    professional = await _make_professional(
        session, tenant, business_hours=own_hours, appointment_types=own_types
    )

    assert professional_business_hours(professional, tenant) == own_hours
    assert professional_appointment_types(professional, tenant) == own_types


async def test_professional_types_filters_inactive_and_sorts_like_tenant_helper(session):
    tenant = await _make_tenant(session)
    professional = await _make_professional(
        session,
        tenant,
        appointment_types=[
            {"name": "B", "sort_order": 2, "is_active": True},
            {"name": "A", "sort_order": 1, "is_active": True},
            {"name": "Hidden", "sort_order": 0, "is_active": False},
        ],
    )
    types = professional_appointment_types(professional, tenant)
    assert [t["name"] for t in types] == ["A", "B"]


async def test_professional_hours_drops_empty_weekday_lists(session):
    tenant = await _make_tenant(session)
    professional = await _make_professional(
        session,
        tenant,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}], "sunday": []},
    )
    assert professional_business_hours(professional, tenant) == {
        "monday": [{"start": "08:00", "end": "12:00"}]
    }


# --------------------------------------------------------------------------
# professional_completeness / can_activate_professional_aware
# --------------------------------------------------------------------------


async def test_completeness_zero_professionals_is_never_complete(session):
    tenant = await _make_tenant(session)

    result = await professional_completeness(session, tenant)

    assert result.total_active == 0
    assert result.complete_count == 0
    assert result.config_complete is False
    assert "At least one active professional is required." in result.reasons

    ok, reasons = await can_activate_professional_aware(session, tenant)
    assert ok is False
    assert reasons


async def test_completeness_single_complete_professional_via_tenant_calendar(session):
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    professional = await _make_professional(session, tenant)
    await set_google_refresh_token(session, tenant.id, SECRET)  # tenant-level covers it
    await session.commit()

    result = await professional_completeness(session, tenant)

    assert result.total_active == 1
    assert result.complete_count == 1
    assert result.config_complete is True
    assert result.reasons == []
    item = result.professionals[0]
    assert item.id == professional.id
    assert item.name == professional.name
    assert item.is_active is True
    assert item.has_calendar is True
    assert item.has_hours is True
    assert item.has_services is True
    assert item.complete is True

    ok, reasons = await can_activate_professional_aware(session, tenant)
    assert ok is True
    assert reasons == []


async def test_completeness_single_complete_professional_via_own_calendar(session):
    """Tenant-level token absent - the professional's OWN token is enough."""
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    professional = await _make_professional(session, tenant)
    await set_professional_google_refresh_token(session, professional.id, SECRET)
    await session.commit()

    result = await professional_completeness(session, tenant)
    assert result.config_complete is True
    assert result.professionals[0].has_calendar is True


async def test_completeness_single_professional_missing_calendar_only(session):
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    await _make_professional(session, tenant)
    await session.commit()
    # No tenant-level AND no professional-level calendar token.

    result = await professional_completeness(session, tenant)

    assert result.config_complete is False
    assert result.complete_count == 0
    item = result.professionals[0]
    assert item.has_calendar is False
    assert item.has_hours is True
    assert item.has_services is True
    assert any("Google Calendar" in r and item.name in r for r in result.reasons)

    ok, reasons = await can_activate_professional_aware(session, tenant)
    assert ok is False
    assert reasons


async def test_completeness_single_professional_missing_hours_and_services(session):
    tenant = await _make_tenant(session)  # tenant business_hours/appointment_types default empty
    professional = await _make_professional(session, tenant)
    await set_professional_google_refresh_token(session, professional.id, SECRET)
    await session.commit()

    result = await professional_completeness(session, tenant)

    assert result.config_complete is False
    item = result.professionals[0]
    assert item.has_calendar is True
    assert item.has_hours is False
    assert item.has_services is False
    assert any("appointment type" in r for r in result.reasons)
    assert any("availability window" in r for r in result.reasons)


async def test_completeness_ignores_inactive_professionals(session):
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    await set_google_refresh_token(session, tenant.id, SECRET)
    await _make_professional(session, tenant, name="Inactive", is_active=False)
    await session.commit()

    result = await professional_completeness(session, tenant)

    # The only professional is inactive, so it's as if there were none.
    assert result.total_active == 0
    assert result.config_complete is False


async def _make_complete_professional(session, tenant, name: str) -> Professional:
    professional = await _make_professional(
        session, tenant, name=name, business_hours=_HOURS, appointment_types=_TYPES
    )
    await set_professional_google_refresh_token(session, professional.id, SECRET)
    return professional


async def test_completeness_at_threshold_requires_every_professional_complete(session):
    """total_active == PARTIAL_ACTIVATION_THRESHOLD (10): ALL must be complete."""
    threshold = get_settings().PARTIAL_ACTIVATION_THRESHOLD
    assert threshold == 10  # the fixture below assumes the shipped default

    tenant = await _make_tenant(session)
    await _make_complete_professional(session, tenant, "Complete")
    for i in range(threshold - 1):  # 9 bare/incomplete ones -> total_active == 10
        await _make_professional(session, tenant, name=f"Bare {i}")
    await session.commit()

    result = await professional_completeness(session, tenant)

    assert result.total_active == threshold
    assert result.complete_count == 1
    assert result.config_complete is False  # <=threshold: everyone must be complete

    ok, _reasons = await can_activate_professional_aware(session, tenant)
    assert ok is False


async def test_completeness_above_threshold_only_needs_one_complete(session):
    """total_active == PARTIAL_ACTIVATION_THRESHOLD + 1 (11): ONE complete is enough."""
    threshold = get_settings().PARTIAL_ACTIVATION_THRESHOLD

    tenant = await _make_tenant(session)
    await _make_complete_professional(session, tenant, "Complete")
    for i in range(threshold):  # 10 bare ones -> total_active == 11
        await _make_professional(session, tenant, name=f"Bare {i}")
    await session.commit()

    result = await professional_completeness(session, tenant)

    assert result.total_active == threshold + 1
    assert result.complete_count == 1
    assert result.config_complete is True  # >threshold: one complete professional suffices
    assert result.reasons == []

    ok, reasons = await can_activate_professional_aware(session, tenant)
    assert ok is True
    assert reasons == []


# --------------------------------------------------------------------------
# tenants.phone_number_id: nullable + partial unique index
# --------------------------------------------------------------------------


async def test_phone_number_id_allows_multiple_null_tenants(session):
    await _make_tenant(session, clinic_name="A", phone_number_id=None)
    await _make_tenant(session, clinic_name="B", phone_number_id=None)
    await session.commit()  # must not raise


async def test_phone_number_id_still_unique_once_connected(session):
    await _make_tenant(session, clinic_name="A", phone_number_id="dup-number")
    await session.commit()

    # `_make_tenant` flushes immediately, so the constraint violation surfaces
    # there rather than at a later explicit commit.
    with pytest.raises(IntegrityError):
        await _make_tenant(session, clinic_name="B", phone_number_id="dup-number")


# --------------------------------------------------------------------------
# professional_completeness_item — single-row completeness (any is_active)
# --------------------------------------------------------------------------


async def test_completeness_item_for_inactive_professional(session):
    """Unlike `professional_completeness` (active-only aggregate), this must
    work for an INACTIVE row too - the hub list view shows every professional."""
    tenant = await _make_tenant(session, business_hours=_HOURS, appointment_types=_TYPES)
    await set_google_refresh_token(session, tenant.id, SECRET)
    professional = await _make_professional(session, tenant, is_active=False)
    await session.commit()

    item = await professional_completeness_item(session, professional, tenant)

    assert item.is_active is False
    assert item.has_calendar is True
    assert item.has_hours is True
    assert item.has_services is True
    assert item.complete is True


async def test_completeness_item_matches_aggregate_for_active_row(session):
    tenant = await _make_tenant(session)
    professional = await _make_professional(session, tenant)
    await session.commit()

    item = await professional_completeness_item(session, professional, tenant)
    aggregate = await professional_completeness(session, tenant)

    assert item == aggregate.professionals[0]


# --------------------------------------------------------------------------
# load_tenant_config — single-active-professional resolution (contract v1 §10 item D)
# --------------------------------------------------------------------------

_TENANT_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
_TENANT_TYPES = [{"name": "Consulta Geral", "duration_min": 30, "is_active": True}]
_PROF_HOURS = {"tuesday": [{"start": "09:00", "end": "17:00"}]}
_PROF_TYPES = [{"name": "Retorno", "duration_min": 45, "is_active": True, "price": "R$ 150"}]


async def test_load_tenant_config_zero_professionals_stays_tenant_level(session):
    tenant = await _make_tenant(
        session,
        business_hours=_TENANT_HOURS,
        appointment_types=_TENANT_TYPES,
        google_calendar_id="tenant-cal",
        post_consult_knowledge="Retorno em 7 dias.",
    )
    await session.commit()

    config = await load_tenant_config(session, tenant)

    assert config.professional_id is None
    assert config.context_doctor_message is None
    assert config.business_hours == _TENANT_HOURS
    assert config.google_calendar_id == "tenant-cal"
    # Straight passthrough from the tenant row - no professional resolution.
    assert config.post_consult_knowledge == "Retorno em 7 dias."


async def test_load_tenant_config_multiple_active_professionals_stays_tenant_level(session):
    """Two-plus active professionals: the base config stays tenant-level (the
    multi_professional plugin tools resolve each one individually instead)."""
    tenant = await _make_tenant(
        session, business_hours=_TENANT_HOURS, appointment_types=_TENANT_TYPES
    )
    await _make_professional(
        session, tenant, name="Dr. A", business_hours=_PROF_HOURS, context_doctor_message="A's note"
    )
    await _make_professional(session, tenant, name="Dr. B")
    await session.commit()

    config = await load_tenant_config(session, tenant)

    assert config.professional_id is None
    assert config.context_doctor_message is None
    assert config.business_hours == _TENANT_HOURS  # NOT Dr. A's own hours


async def test_load_tenant_config_single_active_professional_resolves_through_it(session):
    tenant = await _make_tenant(
        session,
        business_hours=_TENANT_HOURS,
        appointment_types=_TENANT_TYPES,
        google_calendar_id="tenant-cal",
    )
    professional = await _make_professional(
        session,
        tenant,
        business_hours=_PROF_HOURS,
        appointment_types=_PROF_TYPES,
        google_calendar_id="professional-own-cal",
        context_doctor_message="Prefere retornos pela manhã.",
        specialty="Cardiologia",
        about="15 anos de experiência.",
    )
    await set_professional_google_refresh_token(session, professional.id, SECRET)
    await session.commit()

    config = await load_tenant_config(session, tenant)

    assert config.professional_id == professional.id
    assert config.business_hours == _PROF_HOURS
    assert [t.name for t in config.appointment_types] == ["Retorno"]
    assert config.appointment_types[0].duration_min == 45
    assert config.google_calendar_id == "professional-own-cal"
    assert config.google_refresh_token == SECRET
    assert config.context_doctor_message == "Prefere retornos pela manhã."
    assert config.specialty == "Cardiologia"
    assert config.about == "15 anos de experiência."


async def test_load_tenant_config_single_professional_falls_back_to_tenant_values(session):
    """The professional has NO own hours/types/calendar/credential set - every
    field falls back to the tenant's (S1 helpers' existing fallback chain),
    but professional_id/specialty/about/context still come from the row."""
    tenant = await _make_tenant(
        session,
        business_hours=_TENANT_HOURS,
        appointment_types=_TENANT_TYPES,
        google_calendar_id="tenant-cal",
    )
    await set_google_refresh_token(session, tenant.id, "tenant-token")
    professional = await _make_professional(session, tenant, specialty="Pediatria")
    await session.commit()

    config = await load_tenant_config(session, tenant)

    assert config.professional_id == professional.id
    assert config.business_hours == _TENANT_HOURS
    assert config.google_calendar_id == "tenant-cal"
    assert config.google_refresh_token == "tenant-token"
    assert config.specialty == "Pediatria"


async def test_load_tenant_config_inactive_professional_ignored(session):
    """A single INACTIVE professional (no active ones at all) must not be
    picked up - zero active professionals stays tenant-level, same as the
    zero-professionals case."""
    tenant = await _make_tenant(
        session, business_hours=_TENANT_HOURS, appointment_types=_TENANT_TYPES
    )
    await _make_professional(session, tenant, is_active=False, business_hours=_PROF_HOURS)
    await session.commit()

    config = await load_tenant_config(session, tenant)

    assert config.professional_id is None
    assert config.business_hours == _TENANT_HOURS
