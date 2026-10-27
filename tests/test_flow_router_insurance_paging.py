"""The convênio list pages instead of cutting plans (Instituto Gicovate, 2026-10-27).

A clinic may accept any number of convênios. Up to MAX_INSURANCE_PLAN_ROWS the list is
one page, as before; past it, each page shows INSURANCE_PAGE_SIZE plans plus "Ver mais
convênios", Particular and Outro - never more than WhatsApp's 10 rows. With 9 plans the
ninth (Unimed) used to be dropped silently.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import MAX_LIST_ROWS  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import (
    appointment_edit as ae,  # noqa: E402
    flow_router as fr,  # noqa: E402
)
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    appointment,
    context,
    conversation as edit_conversation,
    draft_for,
    professionals as edit_professionals,
    tenant as edit_tenant,
)
from tests.test_flow_router_insurance import (  # noqa: E402
    _conversation,
    _FakeCalendar,
    _professionals,
    _tenant,
)

GICOVATE = [
    "Amil", "ASES", "Assim Saúde", "Bradesco Saúde", "CASSI",
    "Hapvida", "Petrobras", "SulAmérica", "Unimed",
]


def _plan_titles(bubble) -> list[str]:
    fixed = {fr.LABEL_MORE_INSURANCES, fr.LABEL_INSURANCE_PARTICULAR, fr.LABEL_INSURANCE_OTHER}
    return [row[1] for row in bubble.rows if row[1] not in fixed]


def _tap(result, title: str) -> str:
    """The routing text the router reads when the patient taps the row titled `title`."""
    row_id = next(row[0] for row in result.bubbles[0].rows if row[1] == title)
    return inbound_routing_text(title, row_id)


def test_up_to_eight_plans_is_one_page_with_no_more_row():
    shown, next_page = fr.insurance_page(GICOVATE[:8], 0)
    assert shown == GICOVATE[:8]
    assert next_page is None
    result = fr._enter_insurance(_tenant(collect_insurance=True, insurances=GICOVATE[:8]))
    titles = [row[1] for row in result.bubbles[0].rows]
    assert fr.LABEL_MORE_INSURANCES not in titles
    assert len(titles) == MAX_LIST_ROWS


@pytest.mark.parametrize("count", [9, 10, 15, 22, 40])
def test_every_plan_appears_exactly_once_and_no_page_exceeds_the_row_cap(count):
    plans = [f"Plano {i:02d}" for i in range(count)]
    tenant = _tenant(collect_insurance=True, insurances=plans)
    seen: list[str] = []
    page = 0
    for _ in range(count):  # bounded: a cursor bug must fail, not hang
        result = fr._enter_insurance(tenant, page=page)
        rows = result.bubbles[0].rows
        assert len(rows) <= MAX_LIST_ROWS
        assert [row[1] for row in rows][-2:] == [
            fr.LABEL_INSURANCE_PARTICULAR,
            fr.LABEL_INSURANCE_OTHER,
        ]
        seen += _plan_titles(result.bubbles[0])
        more = [row[0] for row in rows if row[1] == fr.LABEL_MORE_INSURANCES]
        if not more:
            break
        page = int(more[0].removeprefix(fr.ROW_INSURANCE_MORE_PREFIX))
    assert seen == plans


def test_a_cursor_past_the_end_restarts_at_the_first_page():
    assert fr.insurance_page(GICOVATE, 99) == fr.insurance_page(GICOVATE, 0)
    assert fr.insurance_page(GICOVATE, -1) == fr.insurance_page(GICOVATE, 0)


def test_the_page_cursor_rides_in_the_tap():
    assert inbound_routing_text(fr.LABEL_MORE_INSURANCES, "insmore|1") == "Ver mais convênios (1)"
    assert fr.insurance_more_page("Ver mais convênios (1)") == 1
    assert fr.insurance_more_page("Ver mais convênios") == 0  # typed: first page
    assert fr.insurance_more_page("Unimed") is None


async def test_gicovate_unimed_is_reachable_through_ver_mais_and_books():
    tenant = _tenant(collect_insurance=True, insurances=GICOVATE)
    pros = _professionals(1)
    first = fr._enter_insurance(tenant)
    assert "Unimed" not in _plan_titles(first.bubbles[0])
    assert fr.LABEL_MORE_INSURANCES in [row[1] for row in first.bubbles[0].rows]

    second = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_INSURANCE, flow_selected_type=None),
        tenant,
        _FakeCalendar(),
        _tap(first, fr.LABEL_MORE_INSURANCES),
        professionals=pros,
    )
    assert second.flow_step == fr.STEP_AWAITING_INSURANCE
    assert _plan_titles(second.bubbles[0]) == ["SulAmérica", "Unimed"]
    assert fr.LABEL_MORE_INSURANCES not in [row[1] for row in second.bubbles[0].rows]

    picked = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_INSURANCE),
        tenant,
        _FakeCalendar(),
        _tap(second, "Unimed"),
        professionals=pros,
    )
    assert picked.flow_selected_insurance == "Unimed"
    assert picked.flow_step == fr.STEP_AWAITING_DAY


async def test_the_edit_flow_pages_the_same_way():
    plans = [{"id": f"p{i}", "name": name} for i, name in enumerate(GICOVATE)]
    tenant = edit_tenant(insurance_plans=plans, insurance_accepted_by={})
    appt = appointment()
    draft = draft_for(appt)

    async def step(step_name, body):
        conv = edit_conversation(draft, step_name)
        return await edit_step(conv, tenant, body, [appt], edit_professionals(), context())

    first = await step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE)
    assert len(first.bubbles[-1].rows) <= MAX_LIST_ROWS
    assert "Unimed" not in _plan_titles(first.bubbles[-1])
    more_id = next(r[0] for r in first.bubbles[-1].rows if r[1] == fr.LABEL_MORE_INSURANCES)

    second = await step(
        fr.STEP_EDIT_INSURANCE, inbound_routing_text(fr.LABEL_MORE_INSURANCES, more_id)
    )
    assert second.flow_step == fr.STEP_EDIT_INSURANCE
    assert _plan_titles(second.bubbles[-1]) == ["SulAmérica", "Unimed"]
