"""Tests for plugins/ehr.py (provider selection) and services/ehr/iclinic.py (the stub)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

from secretaria.models import Appointment, Patient, Tenant  # noqa: E402
from secretaria.plugins import ehr  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.services.ehr.iclinic import IClinicProvider  # noqa: E402


def _ctx(ehr_provider: str | None) -> PostBookingContext:
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], ehr_provider=ehr_provider
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
    appointment = Appointment(
        id=uuid4(), tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-1"
    )
    return PostBookingContext(
        tenant=tenant, patient=patient, appointment=appointment, waba_token=None, source="agent"
    )


async def test_no_provider_configured_is_noop(monkeypatch):
    calls: list = []

    class _SpyProvider:
        async def push_appointment(self, tenant, patient, appointment):
            calls.append(1)
            return "should-not-be-called"

    monkeypatch.setitem(ehr.PROVIDERS, "iclinic", _SpyProvider())
    await ehr._post_booking(_ctx(ehr_provider=None))

    assert calls == []


async def test_unknown_provider_is_noop(monkeypatch):
    calls: list = []

    class _SpyProvider:
        async def push_appointment(self, tenant, patient, appointment):
            calls.append(1)

    monkeypatch.setitem(ehr.PROVIDERS, "iclinic", _SpyProvider())
    await ehr._post_booking(_ctx(ehr_provider="some_unregistered_ehr"))

    assert calls == []


async def test_configured_provider_is_invoked(monkeypatch):
    calls: list[tuple] = []

    class _SpyProvider:
        async def push_appointment(self, tenant, patient, appointment):
            calls.append((tenant.id, patient.id, appointment.id))
            return "external-123"

    monkeypatch.setitem(ehr.PROVIDERS, "iclinic", _SpyProvider())
    ctx = _ctx(ehr_provider="iclinic")

    await ehr._post_booking(ctx)

    assert calls == [(ctx.tenant.id, ctx.patient.id, ctx.appointment.id)]


async def test_provider_push_failure_propagates_to_registry_not_swallowed_here():
    """plugins/ehr.py itself does not try/except — registry.run_post_booking does.

    A raising provider must propagate out of `_post_booking` unchanged, so the
    registry's own fail-open wrapper (tested in test_post_booking_plugin.py) is
    the only place isolating one plugin's failure from another's.
    """

    class _BoomProvider:
        async def push_appointment(self, tenant, patient, appointment):
            raise RuntimeError("iClinic API down")

    ctx = _ctx(ehr_provider="iclinic")
    original = ehr.PROVIDERS["iclinic"]
    ehr.PROVIDERS["iclinic"] = _BoomProvider()
    try:
        raised = False
        try:
            await ehr._post_booking(ctx)
        except RuntimeError:
            raised = True
        assert raised
    finally:
        ehr.PROVIDERS["iclinic"] = original


# --------------------------------------------------------------------------
# services/ehr/iclinic.py — the stub provider itself
# --------------------------------------------------------------------------


async def test_iclinic_stub_returns_deterministic_fake_id_and_logs(monkeypatch):
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    appointment = Appointment(id=uuid4(), tenant_id=tenant.id, google_event_id="evt-1")

    import secretaria.services.ehr.iclinic as iclinic_module

    logged: list[tuple] = []

    def _capture(event, **kwargs):
        if event == "ehr_push_stub":
            logged.append((event, kwargs))

    monkeypatch.setattr(iclinic_module.logger, "info", _capture)

    provider = IClinicProvider()
    result = await provider.push_appointment(tenant, None, appointment)

    assert result == f"iclinic-stub-{appointment.id}"
    assert len(logged) == 1
    assert logged[0][1]["tenant_id"] == str(tenant.id)
    assert logged[0][1]["appointment_id"] == str(appointment.id)
