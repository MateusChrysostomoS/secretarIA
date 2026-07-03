"""Tests for plugins/pix_whatsapp.py and services/payments/pix.py."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import Appointment, Patient, Tenant  # noqa: E402
from secretaria.plugins import pix_whatsapp  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.services.payments.pix import create_deposit_charge  # noqa: E402


class _FakeWhatsAppClient:
    created: list["_FakeWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self.sent: list[tuple] = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.test"}]}


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(pix_whatsapp, "WhatsAppClient", _FakeWhatsAppClient)
    yield


def _ctx(*, pix_key: str | None, with_patient: bool = True, appointment_type="Primeira consulta"):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinica Boa Saude",
        phone_number_id=str(uuid4())[:12],
        pix_key=pix_key,
        appointment_types=[{"name": appointment_type, "price": "R$ 150,00"}],
    )
    patient = None
    patient_id = None
    if with_patient:
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
        patient_id = patient.id
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient_id,
        google_event_id="evt-1",
        appointment_type=appointment_type,
        start_at=datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
    )
    return PostBookingContext(
        tenant=tenant, patient=patient, appointment=appointment, waba_token="tok", source="agent"
    )


# --------------------------------------------------------------------------
# Hook behavior
# --------------------------------------------------------------------------


async def test_missing_pix_key_is_silent_noop():
    await pix_whatsapp._post_booking(_ctx(pix_key=None))
    assert _FakeWhatsAppClient.created == []


async def test_no_patient_is_silent_noop():
    await pix_whatsapp._post_booking(_ctx(pix_key="clinica@pix.com", with_patient=False))
    assert _FakeWhatsAppClient.created == []


async def test_pix_key_present_sends_message_with_key_and_appointment_type():
    ctx = _ctx(pix_key="clinica@pix.com", appointment_type="Primeira consulta")

    await pix_whatsapp._post_booking(ctx)

    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client._access_token == "tok"
    assert client._phone_number_id == ctx.tenant.phone_number_id
    assert len(client.sent) == 1
    to, body = client.sent[0]
    assert to == ctx.patient.wa_id
    assert "clinica@pix.com" in body
    assert "Primeira consulta" in body


async def test_send_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    class _BoomClient(_FakeWhatsAppClient):
        async def send_text_message(self, to, body):
            raise RuntimeError("whatsapp down")

    monkeypatch.setattr(pix_whatsapp, "WhatsAppClient", _BoomClient)

    # Must not raise.
    await pix_whatsapp._post_booking(_ctx(pix_key="clinica@pix.com"))


# --------------------------------------------------------------------------
# services/payments/pix.py — message rendering
# --------------------------------------------------------------------------


def test_create_deposit_charge_includes_price_when_configured():
    ctx = _ctx(pix_key="clinica@pix.com", appointment_type="Primeira consulta")
    charge = create_deposit_charge(ctx.tenant, ctx.appointment)

    assert "clinica@pix.com" in charge.message
    assert "Primeira consulta" in charge.message
    assert "R$ 150,00" in charge.message
    assert charge.code == f"pix-stub-{ctx.appointment.id}"


def test_create_deposit_charge_falls_back_to_generic_wording_without_price():
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinica",
        phone_number_id=str(uuid4())[:12],
        pix_key="clinica@pix.com",
        appointment_types=[],
    )
    appointment = Appointment(
        id=uuid4(), tenant_id=tenant.id, google_event_id="evt-1", appointment_type="Retorno"
    )
    charge = create_deposit_charge(tenant, appointment)

    assert "garantir sua vaga" in charge.message
    assert "Retorno" in charge.message
