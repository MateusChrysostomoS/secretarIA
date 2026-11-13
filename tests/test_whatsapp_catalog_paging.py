"""TASK-082 S4 - WhatsApp catalogue lists page and a typed fragment searches them.

Before: services and doctors past the 9th were dropped in silence
(`MAX_CATALOG_OPTION_ROWS`, `flow_*_list_truncated`), and a typed name had to match
exactly or the turn went to the LLM.

Now, on WhatsApp:
  * a list that fits keeps its single page, exactly as before;
  * a longer one shows CATALOG_PAGE_SIZE (8) items + "Ver mais ..." + the fixed rows
    ("Não sei"), never more than MAX_LIST_ROWS (10); the page rides in the tap
    (`svcmore|<n>` / `profmore|<n>`, schemas/webhook.py), like "Ver mais convênios";
  * typed text that is not an exact name is searched (accent/case/emoji-insensitive,
    "contains"): one hit is selected, several give a short filtered list, none says so
    and offers the list again - a sentence or a question still goes to the model.
The Portal pointer (`SlotsBubble.source`, S1) is untouched by all of this.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_LABEL_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    MAX_LIST_ROWS,
)
from secretaria.models import FlowState
from secretaria.schemas.webhook import _PAYLOAD_ROW_PREFIXES, inbound_routing_text
from secretaria.services import (
    appointment_edit as ae,
    catalog_search as cs,
    flow_router as fr,
    list_options as lo,
)
from secretaria.services.appointment_edit_flow import edit_step
from tests import _edit_flow_support as ef
from tests.test_flow_router_insurance import _FakeCalendar

LONG_A = "Consulta de rotina adulto com retorno incluso"
LONG_B = "Consulta de rotina infantil com retorno incluso"


def _services(n: int, *, names: list[str] | None = None) -> list[dict]:
    names = names or [f"Serviço {i:02d}" for i in range(n)]
    return [
        {"name": name, "duration_min": 30, "is_active": True, "sort_order": i}
        for i, name in enumerate(names)
    ]


def _tenant(services: list[dict], *, plans: list[dict] | None = None):
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=services,
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        timezone="America/Sao_Paulo",
        insurance_plans=plans,
        insurances=None,
        insurance_mode="shared" if plans else None,
        insurance_accepted_by={},
        collect_insurance=bool(plans),
    )


def _doctors(n: int, *, names: list[str] | None = None) -> list:
    names = names or [f"Dra. Médica {i:02d}" for i in range(n)]
    return [
        SimpleNamespace(
            id=uuid4(),
            name=name,
            specialty=None,
            about=None,
            context_doctor_message=None,
            appointment_types=None,
            business_hours=None,
        )
        for name in names
    ]


def _conversation(step: str, **kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tap(bubble, title: str) -> str:
    """The routing text the router reads when the patient taps the row titled `title`."""
    row_id = next(row[0] for row in bubble.rows if row[1] == title)
    return inbound_routing_text(title, row_id)


def _more_title(bubble) -> str | None:
    more = {fr.LABEL_MORE_SERVICES, fr.LABEL_MORE_PROFESSIONALS}
    return next((row[1] for row in bubble.rows if row[1] in more), None)


_FIXED = {
    fr.LABEL_MORE_SERVICES,
    fr.LABEL_MORE_PROFESSIONALS,
    fr.LABEL_DONT_KNOW,
}


def _item_ids(bubble) -> list[str]:
    return [row[0] for row in bubble.rows if row[1] not in _FIXED]


def _assert_whatsapp_caps(bubble) -> None:
    assert len(bubble.rows) <= MAX_LIST_ROWS
    assert all(len(row[1]) <= MAX_LIST_ROW_TITLE_CHARS for row in bubble.rows)
    assert len(bubble.button_label) <= MAX_BUTTON_LABEL_CHARS


async def _walk(first_bubble, step: str, tenant, doctors, *, conv_kw=None) -> list:
    """Follow "Ver mais" from `first_bubble` to the last page; every page's bubble."""
    pages = [first_bubble]
    bubble = first_bubble
    for _ in range(50):  # bounded: a cursor bug must fail, not hang
        more = _more_title(bubble)
        if more is None:
            return pages
        result = await fr.route(
            _conversation(step, **(conv_kw or {})),
            tenant,
            _FakeCalendar(),
            _tap(bubble, more),
            professionals=doctors,
        )
        assert result.action == "reply"
        assert result.flow_step == step
        bubble = result.bubbles[-1]
        pages.append(bubble)
    raise AssertionError("paging never ended")


# --------------------------------------------------------------------------
# The tap contract
# --------------------------------------------------------------------------


def test_more_rows_carry_their_page_in_the_tap():
    assert "svcmore|" in _PAYLOAD_ROW_PREFIXES
    assert "profmore|" in _PAYLOAD_ROW_PREFIXES
    assert inbound_routing_text(fr.LABEL_MORE_SERVICES, "svcmore|2") == "Ver mais serviços (2)"
    assert fr.catalog_more_cursor("Ver mais serviços (2)", fr.LABEL_MORE_SERVICES) == (2, "")
    assert fr.catalog_more_cursor("Ver mais serviços (1|cardio)", fr.LABEL_MORE_SERVICES) == (
        1,
        "cardio",
    )
    # Typed: the first page. Anything else: not this control.
    assert fr.catalog_more_cursor("ver mais serviços", fr.LABEL_MORE_SERVICES) == (0, "")
    assert fr.catalog_more_cursor("Serviço 01", fr.LABEL_MORE_SERVICES) is None
    # "prof|" and "svc|" ids are never mistaken for the paging rows (and back).
    assert not "profmore|1".startswith("prof|")
    assert lo.kind_for_reply_id("svcmore|1") is None
    assert lo.kind_for_reply_id("profmore|1") is None


def test_fixed_labels_fit_whatsapp():
    for label in (fr.LABEL_MORE_SERVICES, fr.LABEL_MORE_PROFESSIONALS):
        assert len(label) <= MAX_LIST_ROW_TITLE_CHARS


@pytest.mark.parametrize(
    ("count", "fixed", "pages"),
    [(9, 1, 1), (10, 1, 2), (17, 1, 3), (40, 1, 5), (10, 0, 1), (11, 0, 2)],
)
def test_catalog_page_sizes(count, fixed, pages):
    items = list(range(count))
    seen, page, seen_pages = [], 0, 0
    while True:
        shown, next_page = fr.catalog_page(items, page, fixed_rows=fixed)
        seen_pages += 1
        assert len(shown) + fixed + (next_page is not None) <= MAX_LIST_ROWS
        if pages > 1:
            assert len(shown) <= fr.CATALOG_PAGE_SIZE
        seen += shown
        if next_page is None:
            break
        page = next_page
    assert seen == items
    assert seen_pages == pages


def test_a_cursor_past_the_end_restarts_at_the_first_page():
    items = list(range(40))
    assert fr.catalog_page(items, 99, fixed_rows=1) == fr.catalog_page(items, 0, fixed_rows=1)
    assert fr.catalog_page(items, -3, fixed_rows=1) == fr.catalog_page(items, 0, fixed_rows=1)


# --------------------------------------------------------------------------
# Services (doctor-first / single-doctor list, "Não sei" pinned)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [9, 10, 17, 40])
async def test_every_service_is_reachable_and_nao_sei_is_on_every_page(count):
    tenant = _tenant(_services(count))
    first = await fr._enter_menu_choice(0, tenant, None)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_SERVICE, tenant, [])
    seen: list[str] = []
    for bubble in pages:
        _assert_whatsapp_caps(bubble)
        assert bubble.rows[-1] == ("svchelp|0", fr.LABEL_DONT_KNOW)
        seen += _item_ids(bubble)
    assert seen == [f"svc|Serviço {i:02d}" for i in range(count)]
    assert len(pages) == (1 if count <= 9 else -(-count // fr.CATALOG_PAGE_SIZE))
    hint = fr.CATALOG_SEARCH_HINT in pages[0].body
    assert hint is (count > 9)


async def test_a_short_list_is_byte_for_byte_what_it_was():
    tenant = _tenant(_services(9))
    first = await fr._enter_menu_choice(0, tenant, None)
    bubble = first.bubbles[-1]
    assert [row[0] for row in bubble.rows] == [
        *(f"svc|Serviço {i:02d}" for i in range(9)),
        "svchelp|0",
    ]
    assert bubble.body == "Qual serviço você gostaria de agendar?"


async def test_a_service_on_page_three_books_from_its_tap():
    tenant = _tenant(_services(40))
    first = await fr._enter_menu_choice(0, tenant, None)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_SERVICE, tenant, [])
    third = pages[2]
    title = next(row[1] for row in third.rows if row[0] == "svc|Serviço 17")
    picked = await fr.route(
        _conversation(fr.STEP_AWAITING_SERVICE),
        tenant,
        _FakeCalendar(),
        _tap(third, title),
        professionals=[],
    )
    assert picked.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert picked.flow_selected_type == "Serviço 17"


async def test_a_24_char_cut_title_still_resolves_on_a_later_page():
    names = [f"Serviço {i:02d}" for i in range(30)] + [LONG_A, LONG_B]
    tenant = _tenant(_services(0, names=names))
    first = await fr._enter_menu_choice(0, tenant, None)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_SERVICE, tenant, [])
    last = pages[-1]
    for full in (LONG_A, LONG_B):
        title = next(row[1] for row in last.rows if row[0] == f"svc|{full}")
        assert len(title) == MAX_LIST_ROW_TITLE_CHARS  # really cut
        picked = await fr.route(
            _conversation(fr.STEP_AWAITING_SERVICE),
            tenant,
            _FakeCalendar(),
            _tap(last, title),
            professionals=[],
        )
        assert picked.flow_selected_type == full


async def test_the_nao_sei_row_of_page_two_opens_service_help():
    tenant = _tenant(_services(17))
    first = await fr._enter_menu_choice(0, tenant, None)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_SERVICE, tenant, [])
    result = await fr.route(
        _conversation(fr.STEP_AWAITING_SERVICE),
        tenant,
        _FakeCalendar(),
        _tap(pages[1], fr.LABEL_DONT_KNOW),
        professionals=[],
    )
    assert result.flow_step == fr.STEP_SERVICE_HELP


async def test_paging_keeps_the_selected_doctor_and_their_header_only_on_page_one():
    tenant = _tenant(_services(3))
    doctors = _doctors(2)
    doctors[0].appointment_types = _services(20)
    doctors[0].specialty = "Retina"
    first = fr._enter_professional_services(doctors[0], tenant)
    assert doctors[0].name in first.bubbles[-1].body
    second = await fr.route(
        _conversation(fr.STEP_AWAITING_SERVICE, flow_selected_professional_id=doctors[0].id),
        tenant,
        _FakeCalendar(),
        _tap(first.bubbles[-1], fr.LABEL_MORE_SERVICES),
        professionals=doctors,
    )
    assert second.flow_selected_professional_id == doctors[0].id
    assert doctors[0].name not in second.bubbles[-1].body
    assert _item_ids(second.bubbles[-1])[0] == "svc|Serviço 08"


async def test_the_portal_pointer_is_the_same_on_every_whatsapp_page():
    tenant = _tenant(_services(40))
    first = await fr._enter_menu_choice(0, tenant, None)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_SERVICE, tenant, [])
    for bubble in pages:
        source = bubble.source
        assert source is not None
        assert source.scope == {"list": "tenant"}
        assert [r[0] for r in source.items] == [f"svc|Serviço {i:02d}" for i in range(40)]
        assert source.pinned == [fr.SERVICE_HELP_ROW]
        # The Portal never gets a WhatsApp paging row.
        assert not any(r[0].startswith("svcmore|") for r in source.items + source.pinned)
        assert lo.first_page_rows(source) == [*source.items[: lo.PAGE_SIZE], fr.SERVICE_HELP_ROW]


# --------------------------------------------------------------------------
# Typed partial search - services
# --------------------------------------------------------------------------

SEARCH_NAMES = [
    "Consulta Cardiológica",
    "Retorno Cardiológico",
    "Exame de Vista",
    "Mapeamento de Retina",
    *(f"Procedimento {i:02d}" for i in range(12)),
]


async def _type(tenant, text: str, step=fr.STEP_AWAITING_SERVICE, doctors=None, **kw):
    return await fr.route(
        _conversation(step, **kw), tenant, _FakeCalendar(), text, professionals=doctors or []
    )


@pytest.mark.parametrize("typed", ["retina", "RETINA", "Retína", "🏥 retina", "  mapeamento de  "])
async def test_one_partial_hit_is_selected(typed):
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, typed)
    assert result.action == "reply"
    assert result.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert result.flow_selected_type == "Mapeamento de Retina"


async def test_several_hits_give_a_short_filtered_list():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, "cardio")
    assert result.action == "reply"
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    bubble = result.bubbles[-1]
    _assert_whatsapp_caps(bubble)
    assert _item_ids(bubble) == ["svc|Consulta Cardiológica", "svc|Retorno Cardiológico"]
    assert bubble.rows[-1][1] == fr.LABEL_DONT_KNOW
    # A search result is not the catalogue: no Portal pointer to a list it is not.
    assert bubble.source is None
    # Tapping a hit books it like any row.
    picked = await _type(tenant, _tap(bubble, bubble.rows[1][1]))
    assert picked.flow_selected_type == "Retorno Cardiológico"


async def test_many_hits_page_with_the_search_kept_in_the_tap():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    first = await _type(tenant, "procedimento")
    bubble = first.bubbles[-1]
    assert len(_item_ids(bubble)) == fr.CATALOG_PAGE_SIZE
    more = _more_title(bubble)
    assert more == fr.LABEL_MORE_SERVICES
    second = await _type(tenant, _tap(bubble, more))
    assert _item_ids(second.bubbles[-1]) == [f"svc|Procedimento {i:02d}" for i in range(8, 12)]
    assert _more_title(second.bubbles[-1]) is None


async def test_no_hit_says_so_and_offers_the_list_again_without_the_model():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, "ortopedia")
    assert result.action == "reply"
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    bubble = result.bubbles[-1]
    assert bubble.body.startswith(fr.SEARCH_NO_HIT_SERVICE)
    assert _item_ids(bubble)[0] == "svc|Consulta Cardiológica"
    # The full list again, with its Portal pointer.
    assert bubble.source is not None


async def test_a_question_still_goes_to_the_model():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, "quanto custa a consulta?")
    assert result.action == "delegate_llm"


@pytest.mark.parametrize(
    "typed", ["desculpa, quero mudar o médico", "prefiro outro horário mesmo", "tem algo amanhã?"]
)
async def test_talk_that_names_nothing_still_goes_to_the_model(typed):
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, typed)
    assert result.action == "delegate_llm"


async def test_one_letter_is_not_a_search():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    result = await _type(tenant, "a")
    assert result.action == "reply"
    assert result.bubbles[-1].body.startswith(fr.SEARCH_NO_HIT_SERVICE)


def test_search_text_is_clamped_at_60():
    assert len(cs.normalize_query("x" * 500)) == cs.MAX_QUERY_CHARS == 60
    assert cs.normalize_query("  Cardiológica  🏥 ") == "cardiologica"


def test_search_key_drops_accents_case_emoji_and_punctuation():
    assert cs.search_key("🏥 Consulta Cardiológica (adulto)") == "consulta cardiologica adulto"
    assert cs.partial_matches(["Ação", "Outro"], "acao", str) == ["Ação"]
    assert cs.partial_matches(["Ação", "Outro"], "a", str) == []  # too short


# --------------------------------------------------------------------------
# Clinic-wide service list (no pinned row) and doctors ("Não sei" pinned)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [10, 11, 17, 40])
async def test_clinic_wide_list_pages(count):
    tenant = _tenant(_services(count))
    doctors = _doctors(2)
    first = fr._enter_clinic_service_catalog(tenant, doctors)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_CATALOG_SERVICE, tenant, doctors)
    seen: list[str] = []
    for bubble in pages:
        _assert_whatsapp_caps(bubble)
        seen += _item_ids(bubble)
    assert seen == [f"svc|Serviço {i:02d}" for i in range(count)]
    assert (len(pages) == 1) is (count <= 10)


async def test_clinic_wide_partial_search_selects_one_hit():
    tenant = _tenant(_services(0, names=SEARCH_NAMES))
    doctors = _doctors(2)
    result = await _type(tenant, "vista", step=fr.STEP_AWAITING_CATALOG_SERVICE, doctors=doctors)
    # Both doctors inherit the clinic catalogue: the doctor list for that service.
    assert result.flow_selected_type == "Exame de Vista"


@pytest.mark.parametrize("count", [9, 10, 17, 40])
async def test_every_doctor_is_reachable_with_nao_sei_on_every_page(count):
    tenant = _tenant(_services(2))
    doctors = _doctors(count)
    first = fr._enter_professional_list(tenant, doctors)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_PROFESSIONAL, tenant, doctors)
    seen: list[str] = []
    for bubble in pages:
        _assert_whatsapp_caps(bubble)
        assert bubble.rows[-1][0] == "profhelp|0"
        seen += _item_ids(bubble)
    assert seen == [f"prof|{d.id}" for d in doctors]


async def test_a_doctor_on_page_two_is_picked_by_id():
    tenant = _tenant(_services(2))
    doctors = _doctors(17)
    first = fr._enter_professional_list(tenant, doctors)
    pages = await _walk(first.bubbles[-1], fr.STEP_AWAITING_PROFESSIONAL, tenant, doctors)
    target = doctors[12]
    title = next(row[1] for row in pages[1].rows if row[0] == f"prof|{target.id}")
    picked = await _type(
        tenant, _tap(pages[1], title), step=fr.STEP_AWAITING_PROFESSIONAL, doctors=doctors
    )
    assert picked.flow_selected_professional_id == target.id


async def test_paging_the_doctor_list_keeps_the_service_and_the_plan_mark():
    plans = [{"id": "p1", "name": "Unimed"}]
    tenant = _tenant(_services(2), plans=plans)
    doctors = _doctors(12)
    result = await _type(
        tenant,
        inbound_routing_text(fr.LABEL_MORE_PROFESSIONALS, "profmore|1"),
        step=fr.STEP_AWAITING_PROFESSIONAL,
        doctors=doctors,
        flow_selected_type="Serviço 01",
        flow_selected_insurance="Unimed",
    )
    assert result.flow_selected_type == "Serviço 01"
    assert result.flow_selected_insurance == "Unimed"
    assert all(fr.INSURANCE_ACCEPTED_MARK in (row[2] or "") for row in result.bubbles[-1].rows[:4])


async def test_typed_doctor_fragment_one_hit_opens_their_services():
    tenant = _tenant(_services(2))
    doctors = _doctors(0, names=["Dr. Diogo Raposo", "Dra. Ana Lima", "Dr. Paulo Souza"])
    result = await _type(tenant, "raposo", step=fr.STEP_AWAITING_PROFESSIONAL, doctors=doctors)
    assert result.flow_selected_professional_id == doctors[0].id
    assert result.flow_step == fr.STEP_AWAITING_SERVICE


async def test_typed_doctor_fragment_many_hits_filters_and_none_reoffers():
    tenant = _tenant(_services(2))
    doctors = _doctors(0, names=["Dr. Diogo Raposo", "Dra. Ana Lima", "Dr. Paulo Souza"])
    many = await _type(tenant, "dr", step=fr.STEP_AWAITING_PROFESSIONAL, doctors=doctors)
    assert many.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert len(_item_ids(many.bubbles[-1])) == 3
    assert many.bubbles[-1].rows[-1][0] == "profhelp|0"
    none = await _type(tenant, "Silva", step=fr.STEP_AWAITING_PROFESSIONAL, doctors=doctors)
    assert none.action == "reply"
    assert none.bubbles[-1].body.startswith(fr.SEARCH_NO_HIT_PROFESSIONAL)


# --------------------------------------------------------------------------
# Convênio: a fragment narrows the list, never decides the coverage alone
# --------------------------------------------------------------------------


async def test_insurance_fragment_shows_the_matching_plans_to_tap():
    plans = [
        {"id": "p1", "name": "Unimed BH"},
        {"id": "p2", "name": "Unimed Rio"},
        {"id": "p3", "name": "Amil"},
    ]
    tenant = _tenant(_services(2), plans=plans)
    result = await _type(tenant, "unimed", step=fr.STEP_AWAITING_INSURANCE)
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_insurance is None
    titles = [row[1] for row in result.bubbles[-1].rows]
    assert titles == [
        "Unimed BH",
        "Unimed Rio",
        fr.LABEL_INSURANCE_PARTICULAR,
        fr.LABEL_INSURANCE_OTHER,
    ]


async def test_insurance_text_with_no_hit_is_kept_as_typed_like_before():
    plans = [{"id": "p1", "name": "Unimed BH"}]
    tenant = _tenant(_services(2), plans=plans)
    result = await _type(tenant, "Bradesco", step=fr.STEP_AWAITING_INSURANCE)
    assert result.flow_selected_insurance == "Bradesco"


# --------------------------------------------------------------------------
# "Alterar consulta" lists page the same way
# --------------------------------------------------------------------------


async def _edit(tenant, step, body, doctors, ctx=None):
    appt = ef.appointment()
    draft = ef.draft_for(appt)
    conv = ef.conversation(draft, step)
    return await edit_step(conv, tenant, body, [appt], doctors, ctx or ef.context())


async def test_edit_service_list_pages_and_a_later_page_tap_changes_the_service():
    tenant = ef.tenant()
    doctors = ef.professionals()
    doctors[0].appointment_types = _services(20)
    first = await _edit(tenant, fr.STEP_EDIT_MORE, ae.LABEL_EDIT_SERVICE, doctors)
    bubble = first.bubbles[-1]
    _assert_whatsapp_caps(bubble)
    assert _more_title(bubble) == fr.LABEL_MORE_SERVICES
    second = await _edit(
        tenant, fr.STEP_EDIT_SERVICE, _tap(bubble, fr.LABEL_MORE_SERVICES), doctors
    )
    assert second.flow_step == fr.STEP_EDIT_SERVICE
    assert _item_ids(second.bubbles[-1])[0] == "svc|Serviço 08"


async def test_edit_service_partial_search_selects_one_hit():
    tenant = ef.tenant()
    doctors = ef.professionals()
    doctors[0].appointment_types = _services(0, names=SEARCH_NAMES)
    result = await _edit(tenant, fr.STEP_EDIT_SERVICE, "retina", doctors)
    assert result.flow_step != fr.STEP_EDIT_SERVICE
    assert ae.EditDraft.from_json(result.flow_edit_draft).current["service"] == (
        "Mapeamento de Retina"
    )


async def test_edit_doctor_list_pages_with_live_marks_on_the_shown_page():
    tenant = ef.tenant()
    doctors = ef.professionals() + _doctors(15)
    calendar = ef.EditCalendar(free=True)
    ctx = ef.context(calendars={str(p.id): calendar for p in doctors})
    first = await _edit(tenant, fr.STEP_EDIT_MORE, ae.LABEL_EDIT_DOCTOR, doctors, ctx)
    bubble = first.bubbles[-1]
    _assert_whatsapp_caps(bubble)
    assert _more_title(bubble) == fr.LABEL_MORE_PROFESSIONALS
    second = await _edit(
        tenant, fr.STEP_EDIT_DOCTOR, _tap(bubble, fr.LABEL_MORE_PROFESSIONALS), doctors, ctx
    )
    rows = second.bubbles[-1].rows
    assert _item_ids(second.bubbles[-1])[0] == f"prof|{doctors[8].id}"
    assert all("Livre" in (row[2] or "") for row in rows if row[0].startswith("prof|"))
