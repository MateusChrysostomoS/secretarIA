"""TASK-085 R U1b - "Ver regras de pagamento dos convênios" only when the clinic left a note.

Owner (2026-11-18, 4th round): "Se não tiver observações da clínica em 'Ver regras de pagamento dos
convênios', não exiba esse botão; basta uma observação da clínica para ele aparecer na tela da
lista do paciente."

TASK-090 (owner, 2026-11-22): the "Outro" convênio no longer has a payment text at all, so no
convênio ever carries a clinic note and the Portal never offers the sheet. A patient who asks how
payment works with a convênio is handed off to a person (tests/test_handoff_insurance_payment.py).

- The Portal convênio card still carries `source.has_clinic_notes` (the wire shape does not
  change for the Portal), and it is always False - even for a snapshot taken before the deploy
  that still had a `note` in a plan dict. Both convênio cards carry it: the booking's and
  "alterar consulta"'s.
- The flag is insurance-only: a service or doctor card's pointer is unchanged.
- WhatsApp is unchanged (its rows never had a button).
- The patient's sheet (`insurance-overview`) keeps the clinic's one sinal line and lists no plan.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402

from secretaria.ai.formatter import OptionSource  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.schemas.conversation import InteractiveRead  # noqa: E402
from secretaria.services import (  # noqa: E402
    appointment_edit as ae,
    flow_router as fr,
    list_options,
)
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
)
from secretaria.services.patient_insurance_overview import (  # noqa: E402
    patient_insurance_overview,
)
from secretaria.services.whatsapp import interactive_portal_list_record  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    appointment,
    context,
    conversation,
    draft_for,
    professionals,
    tenant as edit_tenant,
)
from tests.payments_support import make_sessionmaker  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")

PLANS = [
    {"id": "p1", "name": "Unimed", "mechanism": "desconhecido"},
    {"id": "p3", "name": "GEAP", "mechanism": "desconhecido"},
]
# A snapshot built before the TASK-090 deploy still had the "Outro" note in its plan dict.
STALE_WITH_NOTE = [
    {"id": "p1", "name": "Unimed", "note": None},
    {"id": "p3", "name": "GEAP", "note": "Pagamento na recepção."},
]


def _snapshot(plans) -> SimpleNamespace:
    return SimpleNamespace(insurance_plans=plans, insurances=None)


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def test_no_plan_ever_counts_as_carrying_a_clinic_note():
    assert fr.insurance_plans_have_clinic_notes(PLANS) is False
    assert fr.insurance_plans_have_clinic_notes(STALE_WITH_NOTE) is False
    assert fr.insurance_plans_have_clinic_notes([]) is False


# --------------------------------------------------------------------------
# The booking's convênio card
# --------------------------------------------------------------------------


def test_the_booking_card_never_offers_the_payment_rules_sheet():
    for plans in (PLANS, STALE_WITH_NOTE):
        assert fr._enter_insurance(_snapshot(plans)).bubbles[0].source.has_clinic_notes is False


def test_a_legacy_name_only_clinic_has_no_note():
    bare = SimpleNamespace(insurances=["Plano Antigo"])  # no catalog snapshot at all
    assert fr._enter_insurance(bare).bubbles[0].source.has_clinic_notes is False


def test_whatsapp_rows_are_just_the_names():
    rows = fr._enter_insurance(_snapshot(STALE_WITH_NOTE)).bubbles[0].rows
    assert rows == [
        ("ins|Unimed", "Unimed"),
        ("ins|GEAP", "GEAP"),
        ("ins|particular", fr.LABEL_INSURANCE_PARTICULAR),
        ("ins|outro", fr.LABEL_INSURANCE_OTHER),
    ]
    assert "Pagamento na recepção." not in repr(rows)


# --------------------------------------------------------------------------
# "Alterar consulta"'s convênio card
# --------------------------------------------------------------------------


async def _edit_insurance_card(plans):
    appt = appointment()
    conv = conversation(draft_for(appt), fr.STEP_EDIT_MORE)
    result = await edit_step(
        conv,
        edit_tenant(insurance_plans=plans),
        ae.LABEL_EDIT_INSURANCE,
        [appt],
        professionals(),
        context(),
    )
    assert result.flow_step == fr.STEP_EDIT_INSURANCE
    return result.bubbles[-1].source


async def test_the_edit_card_never_offers_the_payment_rules_sheet():
    assert (await _edit_insurance_card(PLANS)).has_clinic_notes is False
    assert (await _edit_insurance_card(STALE_WITH_NOTE)).has_clinic_notes is False


# --------------------------------------------------------------------------
# The stored pointer and its wire projection
# --------------------------------------------------------------------------


def _wire(source: OptionSource) -> dict:
    record = interactive_portal_list_record(
        "Você vai usar convênio?",
        "Ver convênios",
        list_options.first_page_rows(source),
        "Convênios",
        source=list_options.card_source(source),
    )
    return InteractiveRead.model_validate(record).model_dump()


def test_the_flag_reaches_the_card_on_the_wire_as_false():
    source = fr._enter_insurance(_snapshot(STALE_WITH_NOTE)).bubbles[0].source
    assert list_options.card_source(source)["has_clinic_notes"] is False
    assert _wire(source)["source"]["has_clinic_notes"] is False


def test_a_service_card_pointer_is_unchanged():
    source = OptionSource(kind="service", scope={}, items=[("svc|A", "A", None)], pinned=[])
    assert "has_clinic_notes" not in list_options.card_source(source)
    assert "has_clinic_notes" not in _wire(source)["source"]


def test_a_card_stored_before_u1b_still_projects():
    stored = {
        "kind": "list",
        "body": "b",
        "options": [{"id": "ins|Unimed", "title": "Unimed", "description": None}],
        "source": {
            "v": 1,
            "kind": "insurance",
            "scope": {},
            "pinned_ids": [],
            "total": 1,
            "next_cursor": None,
        },
    }
    source = InteractiveRead.model_validate(stored).model_dump()["source"]
    assert source["kind"] == "insurance"
    assert "has_clinic_notes" not in source


# --------------------------------------------------------------------------
# The patient's sheet: the sinal line, no plan
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


async def _clinic(session, *, mode="shared", sinal=True) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=sinal,
        pix_deposit_percent=30,
        insurance_mode=mode,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def test_a_clinic_outro_is_not_listed_on_the_sheet(db):
    async with db() as session:
        tenant = await _clinic(session)
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP"))
        await session.commit()
        overview = await patient_insurance_overview(session, tenant.id)
        snapshot = await load_tenant_insurance(session, tenant.id)
    assert overview == {"deposit": {"enabled": True, "percent": 30}, "plans": []}
    # The card and the sheet agree: no plan, no button.
    assert all("note" not in plan for plan in snapshot.plans)
    assert fr.insurance_plans_have_clinic_notes(snapshot.plans) is False


async def test_without_any_outro_the_overview_keeps_the_sinal_line(db):
    async with db() as session:
        tenant = await _clinic(session, sinal=False)
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        await session.commit()
        overview = await patient_insurance_overview(session, tenant.id)
        snapshot = await load_tenant_insurance(session, tenant.id)
    assert overview == {"deposit": {"enabled": False, "percent": None}, "plans": []}
    assert fr.insurance_plans_have_clinic_notes(snapshot.plans) is False


async def test_a_doctors_own_outro_is_not_listed_in_independent_mode(db):
    async with db() as session:
        tenant = await _clinic(session, mode="independent")
        ana = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(ana)
        await session.flush()
        session.add_all(
            [
                ProfessionalInsurancePlan(
                    professional_id=ana.id, tenant_id=tenant.id, catalog_id=AMIL
                ),
                ProfessionalInsurancePlan(
                    professional_id=ana.id, tenant_id=tenant.id, custom_name="Plano da Ana"
                ),
            ]
        )
        await session.commit()
        overview = await patient_insurance_overview(session, tenant.id)
        snapshot = await load_tenant_insurance(session, tenant.id)
    assert overview["plans"] == []
    assert fr.insurance_plans_have_clinic_notes(snapshot.plans) is False


async def test_the_overview_route_lists_no_plan(db, client):
    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async with db() as session:
        tenant = await _clinic(session)
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP"))
        await session.commit()
    app.dependency_overrides[get_session] = _session
    try:
        response = await client.get(
            "/internal/brain-message/insurance-overview",
            params={"tenant_id": str(tenant.id)},
            headers={"X-Internal-Api-Key": os.environ["INTERNAL_API_KEY"]},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 200, response.text
    assert response.json() == {"deposit": {"enabled": True, "percent": 30}, "plans": []}
