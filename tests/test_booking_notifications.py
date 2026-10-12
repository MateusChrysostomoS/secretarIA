"""Hook post_booking: confirmações ao paciente e à clínica (MVP Portal, Task 7B)."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.core.database import Base  # noqa: E402
from secretaria.plugins import booking_notifications as bn  # noqa: E402
from secretaria.services.brain_patients import PatientEmailResult  # noqa: E402
from secretaria.services.email import EmailOutcome, is_known_template  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bn, "async_session_factory", maker)
    yield
    await engine.dispose()


def _ctx(channel=None, external_id="ext1", email="clinica@x.com", stored_email=None):
    tenant = SimpleNamespace(
        id=uuid4(), clinic_name="Clínica Teste", contact_email=email, timezone="America/Sao_Paulo",
        insurances=["Unimed"]
    )
    patient = SimpleNamespace(
        id=uuid4(),
        name="Maria",
        channel=channel or bn.CHANNEL_BRAIN_MESSAGE,
        external_id=external_id,
        email=stored_email,
    )
    appointment = SimpleNamespace(
        id=uuid4(),
        appointment_type="Limpeza",
        insurance="Unimed",
        start_at=datetime(2026, 10, 5, 17, 0, tzinfo=UTC),
        end_at=datetime(2026, 10, 5, 17, 30, tzinfo=UTC),
        google_event_id="evt1",
        professional_id=None,
        attendee_name=None,
    )
    return SimpleNamespace(
        tenant=tenant, patient=patient, appointment=appointment, waba_token=None,
        source="flow", redis=None,
    )


@pytest.fixture
def world(monkeypatch):
    sent, invited = [], []

    async def _send(to, template, variables):
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    async def _email(_tenant_id, _external_id):
        return PatientEmailResult(available=True, email="paciente@x.com")

    async def _invite(_tenant, _appointment, email):
        invited.append(email)

    monkeypatch.setattr(bn, "send_transactional_email_result", _send)
    monkeypatch.setattr(bn, "fetch_patient_email_result", _email)
    monkeypatch.setattr(bn, "_invite_patient", _invite)
    return sent, invited


def test_templates_registered():
    assert is_known_template("appointment_booked_patient")
    assert is_known_template("appointment_booked_clinic")


async def test_portal_booking_mails_clinic_and_patient_and_invites(world):
    sent, invited = world
    await bn._post_booking(_ctx())
    assert {(to, t) for to, t, _v in sent} == {
        ("clinica@x.com", "appointment_booked_clinic"),
        ("paciente@x.com", "appointment_booked_patient"),
    }
    assert invited == ["paciente@x.com"]
    clinic_vars = next(v for to, t, v in sent if t == "appointment_booked_clinic")
    assert "Convênio informado" in clinic_vars["insurance_line"]
    assert "Unimed" not in clinic_vars["insurance_line"]


async def test_rerun_does_not_duplicate_anything(world):
    sent, invited = world
    ctx = _ctx()
    await bn._post_booking(ctx)
    await bn._post_booking(ctx)
    assert len(sent) == 2 and invited == ["paciente@x.com"]


async def test_whatsapp_patient_gets_no_portal_email(world):
    sent, invited = world
    await bn._post_booking(_ctx(channel="whatsapp"))
    assert [t for _to, t, _v in sent] == ["appointment_booked_clinic"]
    assert invited == []


async def test_missing_patient_email_still_mails_the_clinic(world, monkeypatch):
    sent, invited = world

    async def _none(_tenant_id, _external_id):
        return PatientEmailResult()

    monkeypatch.setattr(bn, "fetch_patient_email_result", _none)
    await bn._post_booking(_ctx())
    assert [t for _to, t, _v in sent] == ["appointment_booked_clinic"]
    assert invited == []


async def test_stored_email_is_used_when_brain_api_is_down(world, monkeypatch):
    sent, invited = world

    async def _none(_tenant_id, _external_id):
        return PatientEmailResult()

    monkeypatch.setattr(bn, "fetch_patient_email_result", _none)
    await bn._post_booking(_ctx(stored_email="guardado@x.com"))
    assert invited == ["guardado@x.com"]
    assert ("guardado@x.com", "appointment_booked_patient") in {(to, t) for to, t, _v in sent}


async def test_brain_api_answer_wins_and_refreshes_the_stored_copy(world, monkeypatch):
    sent, invited = world
    remembered = []

    async def _remember(_patient, email):
        remembered.append(email)

    monkeypatch.setattr(bn, "_remember_email", _remember)
    await bn._post_booking(_ctx(stored_email="velho@x.com"))
    assert invited == ["paciente@x.com"]
    assert remembered == ["paciente@x.com"]


async def test_unchanged_email_is_not_rewritten(world, monkeypatch):
    remembered = []

    async def _remember(_patient, email):
        remembered.append(email)

    monkeypatch.setattr(bn, "_remember_email", _remember)
    await bn._post_booking(_ctx(stored_email="paciente@x.com"))
    assert remembered == []


async def test_invite_failure_never_breaks_the_hook(world, monkeypatch):
    async def _boom(_tenant, _appointment, _email):
        raise RuntimeError("google says no")

    monkeypatch.setattr(bn, "_invite_patient", _boom)
    await bn._post_booking(_ctx())  # must not raise


async def test_invite_failure_still_sends_patient_confirmation(world, monkeypatch):
    sent, _ = world
    async def fail(*args):
        raise RuntimeError("private@secret.example")
    monkeypatch.setattr(bn, "_invite_patient", fail)
    await bn._post_booking(_ctx())
    assert len(sent) == 2


async def test_sender_exception_releases_only_failed_recipient(world, monkeypatch):
    sent, invited = world
    attempts = []
    async def send(to, template, variables):
        attempts.append(template)
        if template == bn.PATIENT_TEMPLATE and attempts.count(template) == 1:
            raise RuntimeError("private@secret.example")
        sent.append((to, template, variables))
        return EmailOutcome.SENT
    monkeypatch.setattr(bn, "send_transactional_email_result", send)
    ctx = _ctx()
    await bn._post_booking(ctx)
    await bn._post_booking(ctx)
    assert len(sent) == 2
    assert attempts.count(bn.PATIENT_TEMPLATE) == 2
    assert attempts.count(bn.CLINIC_TEMPLATE) == 1
    assert len(invited) == 1


async def test_naive_database_timestamps_still_render_utc_calendar_link(world):
    from urllib.parse import parse_qs, urlparse
    sent, _ = world
    ctx = _ctx()
    ctx.appointment.start_at = ctx.appointment.start_at.replace(tzinfo=None)
    ctx.appointment.end_at = ctx.appointment.end_at.replace(tzinfo=None)
    await bn._post_booking(ctx)
    variables = next(v for _, t, v in sent if t == bn.PATIENT_TEMPLATE)
    link = variables["calendar_line"].splitlines()[1]
    assert parse_qs(urlparse(link).query)["dates"] == ["20261005T170000Z/20261005T173000Z"]


async def test_bad_timezone_does_not_suppress_patient_confirmation(world):
    sent, _ = world
    ctx = _ctx()
    ctx.tenant.timezone = "invalid/timezone"
    await bn._post_booking(ctx)
    assert len(sent) == 2


async def test_brain_answer_refreshes_real_patient_row(world):
    from secretaria.models import Patient, Tenant
    ctx = _ctx(stored_email="old@example.test")
    async with bn.async_session_factory() as session:
        session.add(Tenant(id=ctx.tenant.id, clinic_name="Clinic"))
        session.add(Patient(id=ctx.patient.id, tenant_id=ctx.tenant.id,
                            channel="brain_message", external_id="ext1",
                            name="Maria", email="old@example.test"))
        await session.commit()
    await bn._post_booking(ctx)
    async with bn.async_session_factory() as session:
        patient = await session.get(Patient, ctx.patient.id)
        assert patient.email == "paciente@x.com"


async def test_failed_outcome_allows_only_failed_recipient_to_send_again(world, monkeypatch):
    sent, invited = world
    attempts = []
    async def send(to, template, variables):
        attempts.append(template)
        if template == bn.PATIENT_TEMPLATE and attempts.count(template) == 1:
            return EmailOutcome.DISABLED
        sent.append((to, template, variables))
        return EmailOutcome.SENT
    monkeypatch.setattr(bn, "send_transactional_email_result", send)
    ctx = _ctx()
    await bn._post_booking(ctx)
    await bn._post_booking(ctx)
    assert len(sent) == 2
    assert len(invited) == 1


async def test_invite_failure_is_retried_without_resending_emails(world, monkeypatch):
    sent, _ = world
    attempts = []
    async def invite(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("refused")
    monkeypatch.setattr(bn, "_invite_patient", invite)
    ctx = _ctx()
    await bn._post_booking(ctx)
    await bn._post_booking(ctx)
    await bn._post_booking(ctx)
    assert len(attempts) == 2
    assert len(sent) == 2


async def test_invite_resolves_professionals_calendar_not_clinics(monkeypatch):
    from unittest.mock import AsyncMock

    from secretaria.models import Professional, Tenant
    ctx = _ctx()
    ctx.appointment.professional_id = uuid4()
    async with bn.async_session_factory() as session:
        session.add(Tenant(id=ctx.tenant.id, clinic_name="Clinic"))
        session.add(Professional(id=ctx.appointment.professional_id,
                                 tenant_id=ctx.tenant.id, name="Doctor",
                                 google_calendar_id="professional-calendar"))
        await session.commit()
    config = SimpleNamespace(google_calendar_mode="shared_account")
    monkeypatch.setattr(bn, "load_tenant_config", AsyncMock(return_value=config))
    calendar = SimpleNamespace(add_attendee=AsyncMock())
    resolved = []
    async def resolve(session, tenant, professional, *, tenant_config):
        resolved.append((tenant.id, professional.google_calendar_id, tenant_config))
        return calendar
    monkeypatch.setattr(bn, "resolve_professional_calendar", resolve)
    await bn._invite_patient(ctx.tenant, ctx.appointment, "patient@example.test")
    assert resolved == [(ctx.tenant.id, "professional-calendar", config)]
    calendar.add_attendee.assert_awaited_once_with("evt1", "patient@example.test")


def test_worker_import_registers_booking_hook_in_a_fresh_process():
    import subprocess
    import sys
    code = (
        "import secretaria.workers.arq_worker; "
        "from secretaria.plugins.registry import REGISTRY; "
        "s = REGISTRY['booking_notifications']; "
        "assert s.entitlement_keys == (); assert callable(s.post_booking)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("channel", ["brain_message", "whatsapp"])
async def test_clinic_email_has_booking_reference_without_patient_or_attendee_names(world, channel):
    from secretaria.services.email import _TEMPLATES
    sent, _ = world
    ctx = _ctx(channel=channel)
    ctx.patient.name = "Account Private Name"
    ctx.appointment.attendee_name = "Attendee Private Name"
    await bn._post_booking(ctx)
    variables = next(v for _, t, v in sent if t == bn.CLINIC_TEMPLATE)
    template = _TEMPLATES[bn.CLINIC_TEMPLATE]
    rendered = template.subject.format_map(variables) + template.body.format_map(variables)
    assert ctx.patient.name not in rendered
    assert ctx.appointment.attendee_name not in rendered
    assert str(ctx.appointment.id) in rendered
    assert "pelo Portal" not in rendered
    assert "patient_name" not in variables


@pytest.mark.parametrize("available", [True, False])
async def test_authoritative_null_clears_stored_email_but_outage_uses_it(
    world, monkeypatch, available
):
    import httpx

    from secretaria.models import Patient, Tenant
    from secretaria.services import brain_patients

    sent, invited = world
    ctx = _ctx(stored_email="stale@example.test")
    async with bn.async_session_factory() as session:
        session.add(Tenant(id=ctx.tenant.id, clinic_name="Clinic"))
        session.add(Patient(id=ctx.patient.id, tenant_id=ctx.tenant.id,
                            channel="brain_message", external_id="ext1",
                            email="stale@example.test"))
        await session.commit()
    settings = SimpleNamespace(BRAIN_API_BASE_URL="http://brain.test",
                               INTERNAL_API_KEY="test-key", BRAIN_API_TIMEOUT_SECONDS=2)
    monkeypatch.setattr(brain_patients, "get_settings", lambda: settings)
    real_client = httpx.AsyncClient
    def response(request):
        if not available:
            raise httpx.ConnectError("unavailable")
        return httpx.Response(200, json={"email": None})
    transport = httpx.MockTransport(response)
    monkeypatch.setattr(brain_patients.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=transport, **kwargs))
    monkeypatch.setattr(bn, "fetch_patient_email_result", brain_patients.fetch_patient_email_result)
    await bn._post_booking(ctx)
    async with bn.async_session_factory() as session:
        row = await session.get(Patient, ctx.patient.id)
        assert row.email == (None if available else "stale@example.test")
    assert invited == ([] if available else ["stale@example.test"])
    assert len(sent) == (1 if available else 2)


@pytest.mark.parametrize(
    "insurance", ["Private Patient Name", "private@example.test", "+55 11 99999-1234"]
)
async def test_operational_email_never_renders_free_text_insurance(world, insurance):
    from secretaria.services.email import _TEMPLATES
    sent, _ = world
    ctx = _ctx()
    ctx.appointment.insurance = insurance
    await bn._post_booking(ctx)
    variables = next(v for _, t, v in sent if t == bn.CLINIC_TEMPLATE)
    template = _TEMPLATES[bn.CLINIC_TEMPLATE]
    rendered = template.subject.format_map(variables) + template.body.format_map(variables)
    assert insurance not in rendered
    assert str(ctx.appointment.id) in rendered


@pytest.mark.parametrize("insurance", ["Legacy Private Name", "legacy@example.test"])
async def test_legacy_insurance_catalog_never_exposes_patient_text_in_either_email(
    world, insurance
):
    from secretaria.services.email import _TEMPLATES

    sent, _ = world
    ctx = _ctx()
    ctx.tenant.insurances = [insurance]
    ctx.appointment.insurance = insurance
    await bn._post_booking(ctx)
    assert len(sent) == 2
    for _, template_name, variables in sent:
        template = _TEMPLATES[template_name]
        rendered = template.subject.format_map(variables) + template.body.format_map(variables)
        assert insurance not in rendered
        assert "Convênio informado" in rendered


async def _persist_plan(ctx, kind, *, tenant_id=None):
    from secretaria.models import Professional, Tenant
    from secretaria.models.insurance import (
        InsuranceCatalog,
        ProfessionalInsurancePlan,
        TenantInsurancePlan,
    )

    owner_id = tenant_id or ctx.tenant.id
    plan_id = uuid4()
    async with bn.async_session_factory() as session:
        session.add(Tenant(id=owner_id, clinic_name="Clinic"))
        await session.flush()
        if kind == "catalog":
            catalog_id = uuid4()
            session.add(InsuranceCatalog(
                id=catalog_id, slug="unimed", name="Unimed", normalized_name="unimed",
                mechanism="desconhecido", note="PRIVATE METADATA",
            ))
            await session.flush()
            session.add(TenantInsurancePlan(
                id=plan_id, tenant_id=owner_id, catalog_id=catalog_id,
            ))
            ctx.appointment.insurance_plan_id = plan_id
            name = "Unimed"
        elif kind == "clinic_custom":
            session.add(TenantInsurancePlan(
                id=plan_id, tenant_id=owner_id, custom_name="Plano Clínica Especial",
                custom_payment_note="PRIVATE METADATA",
            ))
            ctx.appointment.insurance_plan_id = plan_id
            name = "Plano Clínica Especial"
        else:
            doctor_id = uuid4()
            session.add(Professional(id=doctor_id, tenant_id=owner_id, name="Doctor"))
            await session.flush()
            session.add(ProfessionalInsurancePlan(
                id=plan_id, tenant_id=owner_id, professional_id=doctor_id,
                custom_name="Plano Médico Especial", custom_payment_note="PRIVATE METADATA",
            ))
            ctx.appointment.insurance_professional_plan_id = plan_id
            name = "Plano Médico Especial"
        await session.commit()
    return name


@pytest.mark.parametrize("kind", ["catalog", "clinic_custom", "professional_custom"])
async def test_persisted_configured_plan_appears_in_both_confirmation_emails(world, kind):
    sent, _ = world
    ctx = _ctx()
    name = await _persist_plan(ctx, kind)
    ctx.appointment.insurance = "UNTRUSTED PRIVATE TEXT"
    await bn._post_booking(ctx)
    assert len(sent) == 2
    for _, _, variables in sent:
        assert variables["insurance_line"] == f"Convênio: {name}\n"
        assert "UNTRUSTED PRIVATE TEXT" not in str(variables)
        assert "PRIVATE METADATA" not in str(variables)


@pytest.mark.parametrize("kind", ["catalog", "clinic_custom", "professional_custom"])
async def test_other_tenants_plan_is_never_disclosed_in_confirmation(world, kind):
    sent, _ = world
    ctx = _ctx()
    name = await _persist_plan(ctx, kind, tenant_id=uuid4())
    await bn._post_booking(ctx)
    assert len(sent) == 2
    for _, _, variables in sent:
        assert name not in variables["insurance_line"]
        assert "Convênio informado" in variables["insurance_line"]


async def test_unresolved_professional_plan_does_not_fall_back_to_clinic_plan(world):
    sent, _ = world
    ctx = _ctx()
    await _persist_plan(ctx, "catalog")
    ctx.appointment.insurance_professional_plan_id = uuid4()
    await bn._post_booking(ctx)
    assert all("Convênio informado" in v["insurance_line"] for _, _, v in sent)


@pytest.mark.parametrize("insurance", [None, "", "   "])
async def test_absent_insurance_omits_insurance_line(world, insurance):
    sent, _ = world
    ctx = _ctx()
    ctx.appointment.insurance = insurance
    await bn._post_booking(ctx)
    assert all(v["insurance_line"] == "" for _, _, v in sent)


async def test_plan_lookup_outage_still_sends_both_emails_without_private_error(
    world, monkeypatch
):
    sent, _ = world
    ctx = _ctx()
    ctx.appointment.insurance_plan_id = uuid4()
    warnings = []

    async def fail(*args, **kwargs):
        raise RuntimeError("private@example.test")

    monkeypatch.setattr(bn, "load_appointment_plans", fail)
    monkeypatch.setattr(bn, "logger", SimpleNamespace(
        warning=lambda event, **kwargs: warnings.append((event, kwargs))
    ))
    await bn._post_booking(ctx)
    assert len(sent) == 2
    assert all("Convênio informado" in v["insurance_line"] for _, _, v in sent)
    assert warnings == [
        ("booking_notification_plan_lookup_failed", {"error_type": "RuntimeError"})
    ]
