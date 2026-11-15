"""TASK-082 S1 - every catalogue list the router builds carries its Portal pointer.

Two promises per builder:
  1. WhatsApp is untouched: `SlotsBubble.rows` are byte-for-byte what they were
     (titles cut at 24, nine catalogue rows + the fixed ones);
  2. the attached `OptionSource` lists the WHOLE catalogue as Portal rows, and
     `list_options.resolve(kind, scope, ...)` rebuilds exactly those rows from the
     stored scope - so page 1 of the card is page 1 of the options route.
Covers the booking lists (`flow_router`) and the "alterar consulta" lists
(`appointment_edit_flow`, CONTRACT.md D5).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.core.whatsapp_limits import MAX_LIST_ROW_TITLE_CHARS
from secretaria.services import appointment_edit_flow as aef, flow_router as fr, list_options as lo
from tests import _edit_flow_support as ef

LONG = "Consulta oftalmológica completa com mapeamento de retina"


def _services(n: int, *, long_first: bool = False) -> list[dict]:
    names = [f"Serviço {i:02d}" for i in range(n)]
    if long_first:
        names[0] = LONG
    return [
        {"name": name, "duration_min": 30, "is_active": True, "sort_order": i}
        for i, name in enumerate(names)
    ]


def _tenant(*, services: list[dict] | None = None, plans: list[dict] | None = None):
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=services if services is not None else _services(14, long_first=True),
        appointment_duration_min=30,
        business_hours={},
        timezone="America/Sao_Paulo",
        insurance_plans=plans,
        insurances=None,
        insurance_mode="shared" if plans else None,
        insurance_accepted_by={},
        collect_insurance=bool(plans),
    )


def _doctors(n: int) -> list:
    return [
        SimpleNamespace(
            id=uuid4(),
            name=f"Dra. Profissional Número {i:02d} da Clínica",
            specialty="Retina" if i % 2 else None,
            about=None,
            context_doctor_message=None,
            appointment_types=_services(12) if i == 0 else None,
            business_hours=None,
        )
        for i in range(n)
    ]


def _plans(n: int) -> list[dict]:
    return [{"id": str(uuid4()), "name": f"Plano de Saúde Número {i:02d}"} for i in range(n)]


def _assert_rebuilds(bubble, tenant, professionals) -> None:
    source = bubble.source
    assert source is not None
    assert lo.resolve(source.kind, source.scope, tenant, professionals) == source.items
    assert len(source.items) > lo.PAGE_SIZE  # the fixtures are long enough to page
    assert all(len(row[1]) > 0 for row in source.items)


def _whatsapp_titles_still_cut(bubble) -> None:
    assert all(len(row[1]) <= MAX_LIST_ROW_TITLE_CHARS for row in bubble.rows)
    assert len(bubble.rows) <= 10


# --------------------------------------------------------------------------
# Booking lists (flow_router)
# --------------------------------------------------------------------------


async def test_single_doctor_service_list_points_at_the_tenant_catalogue() -> None:
    tenant = _tenant()
    result = await fr._enter_menu_choice(0, tenant, None)
    [bubble] = result.bubbles
    assert bubble.source.kind == "service"
    assert bubble.source.scope == {"list": "tenant"}
    assert bubble.source.pinned == [fr.SERVICE_HELP_ROW]
    _assert_rebuilds(bubble, tenant, [])
    # Full title on the Portal, the cut on WhatsApp.
    assert (f"svc|{LONG}", LONG, None) in bubble.source.items
    _whatsapp_titles_still_cut(bubble)
    assert [r[0] for r in bubble.rows][-1] == "svchelp|0"
    assert len(bubble.rows) == fr.MAX_CATALOG_OPTION_ROWS + 1


def test_doctor_service_list_points_at_that_doctors_catalogue() -> None:
    tenant, doctors = _tenant(), _doctors(2)
    services = fr.professional_appointment_types(doctors[0], tenant)
    bubble = fr._service_list_bubble(tenant, services, scope=fr.service_scope(doctors[0].id))
    assert bubble.source.scope == {"list": "doctor", "professional_id": str(doctors[0].id)}
    _assert_rebuilds(bubble, tenant, doctors)


def test_the_doctor_tap_turn_sends_that_doctors_list_with_its_pointer() -> None:
    """S7 browser find: the REAL entry after a doctor tap (also the LLM hand-back,
    `select_professional_and_continue`) must carry the pointer, or the Portal gets
    the WhatsApp page (8 + "Ver mais serviços", no search) for every multi-doctor clinic."""
    tenant, doctors = _tenant(), _doctors(2)
    result = fr._enter_professional_services(doctors[0], tenant)
    [bubble] = result.bubbles
    assert bubble.source is not None
    assert bubble.source.kind == "service"
    assert bubble.source.scope == {"list": "doctor", "professional_id": str(doctors[0].id)}
    assert bubble.source.pinned == [fr.SERVICE_HELP_ROW]
    _assert_rebuilds(bubble, tenant, doctors)
    # The doctor's presentation still heads the card; WhatsApp rows unchanged.
    assert bubble.body.endswith("Qual serviço você gostaria de agendar?")
    _whatsapp_titles_still_cut(bubble)


def test_a_service_list_without_a_scope_stays_a_plain_list() -> None:
    bubble = fr._service_list_bubble(_tenant(), _services(3))
    assert bubble.source is None


def test_clinic_wide_service_list_points_at_the_union() -> None:
    tenant, doctors = _tenant(), _doctors(3)
    result = fr._enter_clinic_service_catalog(tenant, doctors)
    [bubble] = result.bubbles
    assert bubble.source.scope == {"list": "clinic"}
    assert bubble.source.pinned == []
    _assert_rebuilds(bubble, tenant, doctors)


def test_doctor_list_points_at_the_roster_with_the_patients_plan() -> None:
    plans = _plans(2)
    tenant, doctors = _tenant(plans=plans), _doctors(13)
    result = fr._enter_professional_list(tenant, doctors, insurance=plans[0]["name"])
    [bubble] = result.bubbles
    assert bubble.source.kind == "professional"
    assert bubble.source.scope == {"insurance": plans[0]["name"]}
    assert bubble.source.pinned == [fr.PROFESSIONAL_HELP_ROW]
    _assert_rebuilds(bubble, tenant, doctors)
    # Full "🥼 <name>" on the Portal; the 24-char WhatsApp title is untouched.
    assert bubble.source.items[0][1] == f"🥼 {doctors[0].name}"
    _whatsapp_titles_still_cut(bubble)
    assert bubble.rows[0][1] == fr._professional_row_title(doctors[0].name)


def test_rebooking_pool_keeps_its_own_doctors_only() -> None:
    tenant, doctors = _tenant(), _doctors(14)
    pool = list(reversed(doctors[2:13]))
    result = fr._enter_professional_list(tenant, pool, pool=True)
    [bubble] = result.bubbles
    assert bubble.source.scope["only_ids"] == [str(p.id) for p in pool]
    _assert_rebuilds(bubble, tenant, doctors)


def test_service_first_doctor_list_has_no_pinned_row() -> None:
    tenant, doctors = _tenant(), _doctors(12)
    result = fr._enter_service_professional_list(tenant, doctors, "Serviço 01")
    [bubble] = result.bubbles
    assert bubble.source.scope == {"insurance": None}
    assert bubble.source.pinned == []
    _assert_rebuilds(bubble, tenant, doctors)


@pytest.mark.parametrize("page", [0, 1])
def test_insurance_list_points_at_every_plan_on_any_whatsapp_page(page: int) -> None:
    tenant = _tenant(plans=_plans(23))
    result = fr._enter_insurance(tenant, page=page)
    [bubble] = result.bubbles
    assert bubble.source.kind == "insurance"
    assert bubble.source.scope == {}
    assert [r[0] for r in bubble.source.pinned] == ["ins|particular", "ins|outro"]
    _assert_rebuilds(bubble, tenant, [])
    assert bubble.source.items[0][1] == "Plano de Saúde Número 00"
    # WhatsApp keeps its own paging row; the Portal never gets it.
    assert any(r[0].startswith(fr.ROW_INSURANCE_MORE_PREFIX) for r in bubble.rows)
    assert not any(
        r[0].startswith(fr.ROW_INSURANCE_MORE_PREFIX)
        for r in bubble.source.items + bubble.source.pinned
    )


# --------------------------------------------------------------------------
# "Alterar consulta" lists (appointment_edit_flow, CONTRACT.md D5)
# --------------------------------------------------------------------------


def _edit_doctors(n: int) -> list:
    doctors = ef.professionals()
    for i in range(n - len(doctors)):
        doctors.append(
            SimpleNamespace(
                id=uuid4(),
                name=f"Dr. Extra Número {i:02d} da Clínica",
                specialty=None,
                about=None,
                context_doctor_message=None,
                appointment_types=None,
                business_hours=None,
            )
        )
    return doctors


def test_edit_service_list_points_at_the_drafts_doctor() -> None:
    tenant = ef.tenant(appointment_types=_services(3))
    doctors = _edit_doctors(2)
    doctors[0].appointment_types = _services(13)
    draft = ef.draft_for()
    result = aef._service_list(draft, tenant, doctors, ef.context())
    [bubble] = result.bubbles
    assert bubble.source.scope == {"list": "doctor", "professional_id": str(ef.DOCTOR_A)}
    assert bubble.source.pinned == []
    _assert_rebuilds(bubble, tenant, doctors)


async def test_edit_doctor_list_points_at_the_roster_and_marks_page_one_live() -> None:
    tenant = ef.tenant()
    doctors = _edit_doctors(13)
    draft = ef.draft_for()
    calendar = ef.EditCalendar(free=True)
    ctx = ef.context(calendars={str(p.id): calendar for p in doctors})
    result = await aef._doctor_list(draft, tenant, ef.appointment(), doctors, ctx)
    [bubble] = result.bubbles
    source = bubble.source
    assert source.kind == "professional"
    assert source.scope == {"insurance": "Unimed", "edit": True}
    rebuilt = lo.resolve(source.kind, source.scope, tenant, doctors)
    # Same rows, same order; page 1 alone carries the live free/busy mark.
    assert [r[0] for r in rebuilt] == [r[0] for r in source.items]
    assert [r[1] for r in rebuilt] == [r[1] for r in source.items]
    assert rebuilt[lo.PAGE_SIZE :] == source.items[lo.PAGE_SIZE :]
    assert all("Livre" in (r[2] or "") for r in source.items[: lo.PAGE_SIZE])
    assert not any("Livre" in (r[2] or "") for r in rebuilt)
    _whatsapp_titles_still_cut(bubble)


def test_edit_insurance_list_points_at_every_plan_marked_for_the_doctor() -> None:
    tenant = ef.tenant(insurance_plans=_plans(15), insurance_mode="shared")
    doctors = _edit_doctors(2)
    result = aef._insurance_list(ef.draft_for(), tenant, doctors)
    [bubble] = result.bubbles
    assert bubble.source.kind == "insurance"
    assert bubble.source.scope == {"edit_doctor_id": str(ef.DOCTOR_A)}
    assert [r[0] for r in bubble.source.pinned] == ["ins|particular", "ins|outro"]
    _assert_rebuilds(bubble, tenant, doctors)
    assert "aceito pelo médico" in bubble.source.items[0][2]
