"""set_booking_draft v2: six fields, formats only, never a name (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    set_booking_draft,
    set_booking_draft_v2,
)
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services.booking_draft import BookingDraft  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.workers.shared.llm_context import _flow_handback_tools  # noqa: E402

ANA = SimpleNamespace(id=uuid4(), name="Dra. Ana")
BETO = SimpleNamespace(id=uuid4(), name="Dr. Beto")


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def clinic(monkeypatch):
    roster = [ANA, BETO]

    async def _pros(_tenant_id):
        return list(roster)

    monkeypatch.setattr(mp, "_active_professionals", _pros)
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    token = ai_tools._tenant_id_ctx.set(uuid4())
    yield roster, log
    ai_tools._tenant_id_ctx.reset(token)


def test_the_v2_tool_is_named_set_booking_draft_with_six_string_args():
    assert set_booking_draft_v2.name == "set_booking_draft"
    assert set(set_booking_draft_v2.args) == {
        "service",
        "professional",
        "insurance",
        "for_whom",
        "day",
        "time",
    }


async def test_v2_carries_every_field_raw_for_the_resolver(clinic):
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke(
            {
                "service": " limpeza ",
                "professional": "dra. ana",
                "insurance": "Unimed",
                "for_whom": "me",
                "day": "2026-10-08",
                "time": "10:00",
            }
        )
    assert exc.value.draft == BookingDraft(
        service="limpeza",  # the resolver canonicalizes against the FRESH catalog
        professional_id=ANA.id,
        insurance="Unimed",
        attendee="self",
        day=dt.date(2026, 10, 8),
        time=dt.time(10, 0),
    )


async def test_v2_other_is_other_and_empty_is_unknown(clinic):
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({"for_whom": "other"})
    assert exc.value.draft.attendee == "other"
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({})
    assert exc.value.draft == BookingDraft()


async def test_v2_for_whom_is_never_a_name(clinic):
    out = await set_booking_draft_v2.ainvoke({"for_whom": "Maria Silva"})
    assert "error" in out
    assert "Maria" not in out["error"]


@pytest.mark.parametrize(
    "field, value",
    [("day", "08/10/2026"), ("day", "2026-13-01"), ("time", "10h"), ("time", "25:00")],
)
async def test_v2_a_bad_format_is_a_recoverable_error(clinic, field, value):
    out = await set_booking_draft_v2.ainvoke({"service": "Limpeza", field: value})
    assert "error" in out


@pytest.mark.parametrize(
    "name, reason",
    [("Dr. Fantasma", "unknown_professional"), ("Dra. Ana", "ambiguous_professional")],
)
async def test_v2_an_unresolvable_doctor_is_dropped_not_an_error(clinic, name, reason):
    roster, log = clinic
    if reason == "ambiguous_professional":
        roster.append(SimpleNamespace(id=uuid4(), name="Dra. Ana"))
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({"professional": name, "service": "Limpeza"})
    assert exc.value.draft.professional_id is None
    assert exc.value.draft.service == "Limpeza"
    dropped = [f for e, f in log.events if e == "booking_draft_tool_item_dropped"]
    assert dropped == [{"field": "professional", "reason": reason}]


async def test_v2_without_a_clinic_is_an_error():
    token = ai_tools._tenant_id_ctx.set(None)
    try:
        out = await set_booking_draft_v2.ainvoke({"service": "Limpeza"})
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert "error" in out


def test_the_clinic_switch_picks_the_draft_tool():
    off = _flow_handback_tools(SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_SOLE, [])
    on = _flow_handback_tools(
        SimpleNamespace(initial_flows={"ai_draft_v2": True}), BOOKING_TOPOLOGY_SOLE, []
    )
    assert set_booking_draft in off and set_booking_draft_v2 not in off
    assert set_booking_draft_v2 in on and set_booking_draft not in on
    assert sorted(t.name for t in on) == sorted(t.name for t in off)
