"""set_booking_draft: validação da ferramenta (MVP Portal, Task 4)."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tools import BookingDraftRequested, set_booking_draft  # noqa: E402
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
)
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)


def _config(services):
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="C",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[
            RuntimeAppointmentType(name=n, description=None, duration_min=30) for n in services
        ],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )


@pytest.fixture
def sole(monkeypatch):
    tokens = [
        ai_tools._tenant_id_ctx.set(uuid4()),
        ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE),
        ai_tools._tenant_config_ctx.set(_config(["Limpeza", "Clareamento"])),
    ]
    yield
    ai_tools._tenant_config_ctx.reset(tokens[2])
    ai_tools._booking_topology_ctx.reset(tokens[1])
    ai_tools._tenant_id_ctx.reset(tokens[0])


async def test_sole_clinic_needs_a_service(sole):
    out = await set_booking_draft.ainvoke({"professional": "Dra. Ana"})
    assert "error" in out


async def test_sole_clinic_resolves_canonical_service(sole):
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft.ainvoke({"service": "limpeza", "insurance": "Unimed"})
    assert exc.value.appointment_type == "Limpeza"
    assert exc.value.professional_id is None
    assert exc.value.insurance == "Unimed"


async def test_sole_clinic_rejects_unknown_service(sole):
    out = await set_booking_draft.ainvoke({"service": "Cirurgia de Marte"})
    assert "error" in out


@pytest.fixture
def multi(monkeypatch):
    ana = SimpleNamespace(id=uuid4(), name="Dra. Ana")
    beto = SimpleNamespace(id=uuid4(), name="Dr. Beto")
    offers = {
        ana.id: [RuntimeAppointmentType(name="Limpeza", description=None, duration_min=30)],
        beto.id: [
            RuntimeAppointmentType(name="Limpeza", description=None, duration_min=30),
            RuntimeAppointmentType(name="Ortodontia", description=None, duration_min=30),
        ],
    }

    async def _pros(_tenant_id):
        return [ana, beto]

    async def _services(_tenant_id, professional):
        return offers[professional.id]

    monkeypatch.setattr(mp, "_active_professionals", _pros)
    monkeypatch.setattr(mp, "_professional_services", _services)
    tokens = [
        ai_tools._tenant_id_ctx.set(uuid4()),
        ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_MULTI),
        ai_tools._tenant_config_ctx.set(_config([])),
    ]
    yield ana, beto
    ai_tools._tenant_config_ctx.reset(tokens[2])
    ai_tools._booking_topology_ctx.reset(tokens[1])
    ai_tools._tenant_id_ctx.reset(tokens[0])


async def test_multi_service_offered_by_one_doctor_resolves_the_doctor(multi):
    _ana, beto = multi
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft.ainvoke({"service": "Ortodontia"})
    assert exc.value.professional_id == beto.id
    assert exc.value.appointment_type == "Ortodontia"


async def test_multi_service_offered_by_several_doctors_is_a_recoverable_error(multi):
    out = await set_booking_draft.ainvoke({"service": "Limpeza"})
    assert "error" in out
    assert "Dra. Ana" in out["error"] and "Dr. Beto" in out["error"]


async def test_multi_unknown_doctor_is_a_recoverable_error(multi):
    out = await set_booking_draft.ainvoke({"professional": "Dr. Fantasma", "service": "Limpeza"})
    assert "error" in out


async def test_multi_doctor_alone_returns_no_service(multi):
    ana, _beto = multi
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft.ainvoke({"professional": "Dra. Ana"})
    assert exc.value.professional_id == ana.id
    assert exc.value.appointment_type is None


async def test_multi_nothing_given_is_an_error(multi):
    out = await set_booking_draft.ainvoke({})
    assert "error" in out


async def test_multi_unknown_service_is_a_recoverable_error(multi):
    out = await set_booking_draft.ainvoke({"service": "Inexistente"})
    assert "error" in out


async def test_multi_duplicate_professional_name_is_a_recoverable_error(multi):
    ana, beto = multi
    beto.name = ana.name
    out = await set_booking_draft.ainvoke({"professional": ana.name, "service": "Limpeza"})
    assert "error" in out


async def test_multi_rejects_service_not_offered_by_chosen_professional(multi):
    out = await set_booking_draft.ainvoke({"professional": "Dra. Ana", "service": "Ortodontia"})
    assert "error" in out
