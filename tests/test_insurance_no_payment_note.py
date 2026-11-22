"""TASK-090 (owner, 2026-11-22) - the "Outro" convênio has no payment text any more.

Owner (SPEC §3 item 2): "Texto de pagamento do convênio 'Outro': removido por completo, inclusive
do banco. Quando o paciente perguntar a regra de pagamento do convênio, a secretária chama uma
pessoa por e-mail." This file pins the removal half (the hand-off half is
tests/test_handoff_insurance_payment.py):

- the ORM no longer maps the column (it stays nullable in the database until its own DROP
  migration, applied after this code is deployed, so an insert that never names it works);
- creating an "Outro" (clinic or doctor) needs only the name: an older Portal that still sends the
  note is NOT refused (the key is ignored) and the note is never echoed back;
- no plan read carries a payment note;
- neither the booking confirmation nor the Pix ask carries "Sobre o pagamento do convênio".
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

import inspect  # noqa: E402
import pathlib  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import FlowState, Professional, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.schemas import insurance as insurance_schemas  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.flow_router import STEP_AWAITING_CONFIRMATION, route  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
)
from secretaria.services.payments import deposit_lifecycle  # noqa: E402
from secretaria.workers.shared import deposit_request  # noqa: E402
from tests.payments_support import make_sessionmaker  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "secretaria"
BLOCK = "Sobre o pagamento do convênio"
OLD_NOTE = "NOTA-ANTIGA-NAO-VAZAR paga na recepção"


# --------------------------------------------------------------------------
# Static: the column is not mapped and nothing in the code names it
# --------------------------------------------------------------------------


def test_the_orm_no_longer_maps_the_payment_note():
    for model in (TenantInsurancePlan, ProfessionalInsurancePlan):
        assert "custom_payment_note" not in model.__table__.columns
        assert not hasattr(model, "custom_payment_note")


def test_no_source_file_names_the_payment_note():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "custom_payment_note" in path.read_text(encoding="utf-8")
        or "payment_note" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_pix_texts_take_no_payment_note():
    assert "payment_note" not in inspect.signature(deposit_lifecycle.before_request_text).parameters
    assert "payment_note" not in inspect.signature(
        deposit_lifecycle._deposit_request_text
    ).parameters


# --------------------------------------------------------------------------
# Schemas: the note is ignored on input and absent on output
# --------------------------------------------------------------------------


def test_plan_read_has_no_payment_note():
    assert "custom_payment_note" not in insurance_schemas.TenantInsurancePlanRead.model_fields
    for schema in (
        insurance_schemas.TenantInsurancePlanCustomCreate,
        insurance_schemas.ProfessionalInsuranceCustomCreate,
    ):
        assert set(schema.model_fields) == {"custom_name"}
        parsed = schema.model_validate({"custom_name": "GEAP", "custom_payment_note": "x"})
        assert parsed.model_dump() == {"custom_name": "GEAP"}


# --------------------------------------------------------------------------
# The hub over HTTP
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def hub(db):
    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from secretaria.main import app

    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            insurance_mode="shared",
        )
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    yield tenant_id
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


@pytest.mark.parametrize("old_portal", [False, True])
async def test_create_clinic_custom_plan_without_note_201(
    client: AsyncClient, hub, db, old_portal
):
    body = {"custom_name": "GEAP"}
    if old_portal:
        body["custom_payment_note"] = OLD_NOTE  # an older Portal: ignored, never a 422
    created = await client.post("/tenants/me/insurance-plans/custom", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "GEAP" and created.json()["is_custom"] is True
    assert "custom_payment_note" not in created.json()
    listed = await client.get("/tenants/me/insurance-plans")
    assert listed.status_code == 200
    assert all("custom_payment_note" not in plan for plan in listed.json())
    assert OLD_NOTE not in created.text + listed.text
    async with db() as session:
        names = (
            await session.scalars(
                select(TenantInsurancePlan.custom_name).where(TenantInsurancePlan.tenant_id == hub)
            )
        ).all()
    assert names == ["GEAP"]


@pytest.mark.parametrize("old_portal", [False, True])
async def test_create_professional_custom_plan_without_note_201(
    client: AsyncClient, hub, db, old_portal
):
    async with db() as session:
        tenant = await session.get(Tenant, hub)
        tenant.insurance_mode = "independent"
        doctor = Professional(id=uuid4(), tenant_id=hub, name="Dra. Ana", is_active=True)
        session.add(doctor)
        await session.commit()
    body = {"custom_name": "Plano da Ana"}
    if old_portal:
        body["custom_payment_note"] = OLD_NOTE
    created = await client.post(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans/custom", json=body
    )
    assert created.status_code in (200, 201), created.text
    assert "custom_payment_note" not in created.json()
    got = await client.get(f"/tenants/me/professionals/{doctor.id}/insurance-plans")
    assert got.status_code == 200, got.text
    selectable = got.json()["selectable"]
    assert any(plan["name"] == "Plano da Ana" for plan in selectable)
    assert all("custom_payment_note" not in plan for plan in selectable)
    assert OLD_NOTE not in created.text + got.text


async def test_the_flow_snapshot_plans_carry_no_note(db):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id="p1", insurance_mode="shared"
        )
        session.add(tenant)
        await session.flush()
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP"))
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)
    assert sorted(plan["name"] for plan in insurance.plans) == ["GEAP", "Unimed"]
    assert all("note" not in plan for plan in insurance.plans)


# --------------------------------------------------------------------------
# What the patient reads: the confirmation and the Pix ask
# --------------------------------------------------------------------------

TZ = ZoneInfo("America/Sao_Paulo")
SLOT_DAY = "2099-01-05"  # a Monday
SLOT = f"{SLOT_DAY}T08:00"


class _Agenda(CalendarService):
    """A CalendarService with no Google: an empty agenda that records the booking."""

    def __init__(self) -> None:
        self._tz = TZ
        self._calendar_id = "agenda"
        self._business_hours = {}
        self._default_slot_minutes = 30
        self.created: list = []

    async def check_availability(self, start, end, max_results: int = 50) -> list[dict]:
        return []

    async def create_event(self, start, end, summary, description="", reminders=None) -> dict:
        self.created.append((start, end, summary))
        return {"id": "evt-new", "htmlLink": "https://calendar.google.com/evt-new"}


def _tenant_with_a_stale_note():
    """A snapshot as one built BEFORE this change looked: the "Outro" plan dict had its note."""
    return SimpleNamespace(
        initial_flows={},
        appointment_types=[
            {"name": "Primeira Consulta", "duration_min": 40, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        insurance_plans=[{"id": "p-geap", "name": "GEAP", "note": OLD_NOTE}],
        insurances=None,
    )


def _confirming() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_day=SLOT_DAY,
        flow_selected_slot=SLOT,
        flow_selected_professional_id=None,
        flow_selected_insurance="GEAP",
        flow_managing_appointment_id=None,
        flow_attendee_name="",
        patient_id=None,
    )


async def test_booking_confirmation_has_no_convenio_payment_block():
    agenda = _Agenda()
    res = await route(
        _confirming(), _tenant_with_a_stale_note(), agenda, "Confirmar", patient_name="João"
    )
    assert agenda.created, res
    body = res.bubbles[0].body
    assert body.startswith("Pronto! Seu agendamento está confirmado.")
    assert BLOCK not in body and OLD_NOTE not in body and "💳" not in body
    assert res.appointment["insurance"] == "GEAP"


def test_the_outro_pix_ask_has_no_convenio_payment_block():
    """Both Pix asks (`before`: the reservation; `after`: the booked appointment)."""
    before = deposit_lifecycle.before_request_text(
        "Clinica",
        service="Consulta",
        when_text="05/01 às 08:00",
        amount_cents=6000,
        minutes=30,
        until_text="08:30",
        copy_paste="00020126-PIX",
    )
    after = deposit_lifecycle._deposit_request_text(
        SimpleNamespace(clinic_name="Clinica"),
        SimpleNamespace(appointment_type="Consulta"),
        6000,
        "00020126-PIX",
    )
    for text in (before, after):
        assert BLOCK not in text and "💳" not in text


async def test_the_before_pix_request_ignores_a_stale_note_in_a_queued_request(monkeypatch):
    """A deposit request built by the old code still carries `payment_note`: never sent."""
    sent: list[str] = []

    class _Sender:
        async def send_text_message(self, to, body):
            sent.append(body)
            return {"messages": [{"id": "wamid.1"}]}

        async def send_buttons(self, to, body, buttons):
            sent.append(body)
            return {"messages": [{"id": "wamid.2"}]}

    start = datetime(2099, 1, 5, 8, 0, tzinfo=TZ)
    request = {
        "deposit_id": uuid4(),
        "amount_cents": 6000,
        "copy_paste": "00020126-PIX",
        "expires_at": start + timedelta(minutes=30),
        "start_at": start,
        "end_at": start + timedelta(minutes=40),
        "service": "Consulta",
        "minutes": 30,
        "payment_note": OLD_NOTE,
    }
    # No conversation id: nothing is recorded, only what goes out is read.
    reply = SimpleNamespace(conversation_id=None, patient_ref="5511999999999", channel=None)
    tenant = SimpleNamespace(id=uuid4(), clinic_name="Clinica", timezone="America/Sao_Paulo")
    monkeypatch.setattr(deposit_request, "_reply_sender", lambda *_a, **_k: _Sender())
    ok = await deposit_request._send_deposit_request_notice(
        reply, request, tenant=tenant, waba_token=None
    )
    assert ok is True and len(sent) == 2
    assert all(BLOCK not in body and OLD_NOTE not in body for body in sent)


def test_the_flow_never_reads_a_plan_note():
    """Static guard: nothing in the flow router looks up a plan's `note` any more."""
    source = inspect.getsource(flow_router)
    assert 'plan["note"]' not in source and 'plan.get("note")' not in source
    assert BLOCK not in source
