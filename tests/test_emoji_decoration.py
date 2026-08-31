"""FEAT 44 - which surfaces wear an emoji, and that decorating broke no matcher.

Two properties, and the second is the one with teeth:

  1. RENDER - the request's exact mapping. Check/cross on affirm/decline
     buttons, hospital on a service row that has room for it, calendar on every
     day and time, arrow on the picker's back rows, and *nothing* on
     "Não sei" / "Ver mais dias".

  2. MATCH - every label above is compared against what a tap echoes back, and
     `svc|` rows are compared against the *displayed title itself* (`svc|` is
     deliberately absent from `_PAYLOAD_ROW_PREFIXES`). Three forms therefore
     have to keep resolving forever: the decorated tap, the bare tap from a card
     rendered before this shipped, and - the common case for a yes/no question -
     an answer the patient simply TYPED. `strip_decoration` inside each layer's
     `_norm` is what buys all three at once, and these tests are what prove it:
     a naive `LABEL_YES = "✅ Sim"` passes every render assertion in part 1
     while silently breaking "sim".

tests/test_whatsapp_limits.py covers the helpers' own arithmetic; this file is
about the flow that uses them.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import (  # noqa: E402
    DECORATION_EMOJI,
    EMOJI_AFFIRMATIVE,
    EMOJI_BACK,
    EMOJI_NEGATIVE,
    EMOJI_SERVICE,
    MAX_BUTTON_LABEL_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    truncate_button_label,
    truncate_list_row_title,
)
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_scope import canonical_service_name  # noqa: E402
from secretaria.workers.tasks import _label_match_body  # noqa: E402

_SHORT = "Consulta Geral"  # 14 chars: room to spare for the hospital emoji
_LONG = "Consulta de rotina infantil"  # 27 chars: already truncated today


def _services():
    return [
        {"name": _SHORT, "duration_min": 30, "is_active": True, "sort_order": 0},
        {"name": _LONG, "duration_min": 30, "is_active": True, "sort_order": 1},
    ]


def _tenant(**kw):
    base = dict(
        initial_flows={},
        appointment_types=_services(),
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurances=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. Render - the request's mapping, label by label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, emoji",
    [
        (fr.LABEL_YES, EMOJI_AFFIRMATIVE),
        (fr.LABEL_BOOK_SERVICE, EMOJI_AFFIRMATIVE),
        (fr.LABEL_CONFIRM, EMOJI_AFFIRMATIVE),
        (fr.LABEL_NO, EMOJI_NEGATIVE),
        (fr.LABEL_CANCEL, EMOJI_NEGATIVE),
        (fr.LABEL_CANCEL_APPT, EMOJI_NEGATIVE),
        (fr.LABEL_OTHER_SERVICE, EMOJI_NEGATIVE),
        (fr.LABEL_ANOTHER_DAY, EMOJI_BACK),
        (fr.LABEL_ANOTHER_SERVICE, EMOJI_BACK),
    ],
)
def test_decorated_labels_carry_the_emoji_the_request_asked_for(label, emoji):
    assert label.startswith(f"{emoji} ")


@pytest.mark.parametrize(
    "label",
    [
        fr.LABEL_DONT_KNOW,  # named in the request as an explicit exception
        fr.LABEL_MORE_DAYS,  # ditto - it pages forward, it is not a day
        fr.LABEL_RESCHEDULE,  # neither an affirmation nor a refusal
        fr.LABEL_BACK,
        fr.LABEL_OTHER,
    ],
)
def test_undecorated_labels_stay_bare(label):
    assert not any(label.startswith(emoji) for emoji in DECORATION_EMOJI)


def test_the_two_labels_the_request_named_as_exceptions_are_unchanged():
    # Spelled out rather than derived: these two are the request's own carve-out
    # (only "Nao sei" keeps no emoji among the service rows; among the day rows
    # only "Ver mais dias" does).
    assert fr.LABEL_DONT_KNOW == "Não sei"
    assert fr.LABEL_MORE_DAYS == "Ver mais dias"


def test_no_decorated_label_is_silently_cut_by_the_send_path():
    # THE trap this feature walks into. A fixed label is compared against in
    # FULL, but services/whatsapp.py re-applies the cut before sending, so a
    # label pushed one character over its cap renders fine and then does nothing
    # when tapped. "arrow + Escolher outro Serviço" was exactly 25 - one over -
    # which is why the back rows read "Outro dia"/"Outro serviço" today.
    for row_label in (
        fr.LABEL_ANOTHER_DAY,
        fr.LABEL_ANOTHER_SERVICE,
        fr.LABEL_MORE_DAYS,
    ):
        assert truncate_list_row_title(row_label) == row_label
        assert len(row_label) <= MAX_LIST_ROW_TITLE_CHARS
    for button_label in (
        fr.LABEL_YES,
        fr.LABEL_NO,
        fr.LABEL_CONFIRM,
        fr.LABEL_CANCEL,
        fr.LABEL_CANCEL_APPT,
        fr.LABEL_BOOK_SERVICE,
        fr.LABEL_OTHER_SERVICE,
    ):
        assert truncate_button_label(button_label) == button_label
        assert len(button_label) <= MAX_BUTTON_LABEL_CHARS


def test_default_reactivation_buttons_are_decorated_but_a_tenants_own_are_not():
    # The emoji belongs to the copy WE ship. A clinic that typed its own pair
    # into the hub gets its own words back, untouched.
    assert fr.DEFAULT_REACTIVATION_BUTTONS == ["✅ Sim", "❌ Não"]
    assert fr.reactivation_choice_buttons(_tenant()) == ["✅ Sim", "❌ Não"]
    custom = fr.reactivation_choice_buttons(
        _tenant(initial_flows={"reactivation": {"buttons": ["Claro", "Agora não"]}})
    )
    assert custom == ["Claro", "Agora não"]


def test_day_rows_carry_the_calendar_emoji_and_still_fit():
    label = fr._day_row_label(datetime(2026, 8, 18))
    assert label == "🗓️ Ter, 18/08"
    assert len(label) <= MAX_LIST_ROW_TITLE_CHARS


# ---------------------------------------------------------------------------
# 2. Service rows - conditional decoration, and the key still resolves
# ---------------------------------------------------------------------------


def test_a_short_service_name_gets_the_hospital_emoji():
    assert fr._service_row_title(_SHORT) == f"{EMOJI_SERVICE} {_SHORT}"


def test_a_service_name_that_is_already_truncated_stays_bare():
    # It has no room to spare, and the two characters would come straight out
    # of the tail that keeps it distinct from its siblings.
    assert fr._service_row_title(_LONG) == truncate_list_row_title(_LONG)
    assert EMOJI_SERVICE not in fr._service_row_title(_LONG)


def test_both_service_catalogs_render_a_name_identically():
    # Two different functions build the `svc|` list (`_service_list_bubble` for
    # the doctor-first flow, `_enter_clinic_service_catalog` for the clinic-wide
    # one). They share `_service_row_title` so one screen cannot decorate a
    # service the other leaves bare.
    tenant = _tenant()
    professional = SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        specialty=None,
        about=None,
        is_active=True,
        appointment_types=_services(),
        business_hours=tenant.business_hours,
    )
    doctor_first = fr._service_list_bubble(tenant, _services())
    clinic_wide = fr._enter_clinic_service_catalog(tenant, [professional])
    assert [t for _, t in doctor_first.rows if t != fr.LABEL_DONT_KNOW] == [
        t for _, t in clinic_wide.bubbles[0].rows
    ]


def test_a_tap_on_a_decorated_service_row_still_resolves_to_the_catalog_entry():
    # The one that matters most: `svc|` is NOT in _PAYLOAD_ROW_PREFIXES, so this
    # decorated title IS the whole body the router receives.
    tapped = fr._service_row_title(_SHORT)
    assert tapped != _SHORT  # it really is decorated
    assert canonical_service_name(_services(), tapped) == _SHORT


def test_the_undecorated_forms_of_a_service_name_keep_resolving():
    # A card rendered before FEAT 44, a name the patient typed, and an LLM tool
    # call all arrive bare and must land on the same entry.
    for candidate in (
        _SHORT,
        _SHORT.lower(),
        f"  {_SHORT}  ",
        truncate_list_row_title(_LONG),
    ):
        assert canonical_service_name(_services(), candidate) is not None


def test_decoration_cannot_make_two_services_resolve_to_each_other():
    services = [
        {"name": "Retorno adulto", "is_active": True},
        {"name": "Retorno infantil", "is_active": True},
    ]
    adulto = fr._service_row_title("Retorno adulto")
    infantil = fr._service_row_title("Retorno infantil")
    assert adulto != infantil
    assert canonical_service_name(services, adulto) == "Retorno adulto"
    assert canonical_service_name(services, infantil) == "Retorno infantil"


# ---------------------------------------------------------------------------
# 3. Match - decorated, bare and typed all normalise to the same key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, typed",
    [
        (fr.LABEL_YES, "sim"),
        (fr.LABEL_NO, "não"),
        (fr.LABEL_CONFIRM, "confirmar"),
        (fr.LABEL_CANCEL, "cancelar"),
        (fr.LABEL_CANCEL_APPT, "Cancelar"),
        (fr.LABEL_OTHER_SERVICE, "outro serviço"),
        (fr.LABEL_ANOTHER_DAY, "Outro dia"),
        (fr.LABEL_ANOTHER_SERVICE, "outro serviço"),
    ],
)
def test_a_typed_answer_matches_its_decorated_label(label, typed):
    # The regression a render-only change would have shipped: every one of these
    # comparisons is `_norm(body) == _norm(LABEL_X)`, and a patient answering a
    # yes/no question by typing is the common case, not the edge case.
    assert fr._norm(typed) == fr._norm(label)


@pytest.mark.parametrize(
    "label",
    [
        fr.LABEL_YES,
        fr.LABEL_NO,
        fr.LABEL_CONFIRM,
        fr.LABEL_CANCEL,
        fr.LABEL_CANCEL_APPT,
    ],
)
def test_a_tap_on_the_decorated_button_matches_it(label):
    assert fr._label_match(label, label)


def test_the_greeting_cards_matcher_in_the_worker_accepts_all_three_forms():
    # workers/tasks.py has its own normaliser; the greeting card renders
    # LABEL_CANCEL_APPT, so it needs the same strip or the manage flow becomes
    # unreachable from the very card that offers it.
    assert _label_match_body(fr.LABEL_CANCEL_APPT, fr.LABEL_CANCEL_APPT)
    assert _label_match_body("Cancelar", fr.LABEL_CANCEL_APPT)
    assert _label_match_body("cancelar", fr.LABEL_CANCEL_APPT)


def test_the_pickers_back_rows_are_matched_through_their_payload_suffix():
    # A control row's tap arrives as "<title> (<payload>)". `_control_match` has
    # to look past the suffix AND past the arrow - and a typed, undecorated
    # "outro serviço" still has to work.
    assert fr._control_match(f"{fr.LABEL_ANOTHER_SERVICE} (service)", fr.LABEL_ANOTHER_SERVICE)
    assert fr._control_match(f"{fr.LABEL_ANOTHER_DAY} (2)", fr.LABEL_ANOTHER_DAY)
    assert fr._control_match("Outro serviço (service)", fr.LABEL_ANOTHER_SERVICE)
    assert fr._control_match(f"{fr.LABEL_MORE_DAYS} (2)", fr.LABEL_MORE_DAYS)


def test_dont_know_still_matches_and_did_not_grow_an_emoji():
    assert fr._label_match(fr.LABEL_DONT_KNOW, fr.LABEL_DONT_KNOW)
    assert fr._label_match("não sei", fr.LABEL_DONT_KNOW)


# ---------------------------------------------------------------------------
# 4. End to end - a decorated tap still drives the flow forward
# ---------------------------------------------------------------------------


async def test_tapping_a_decorated_service_row_advances_the_flow():
    conv = SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
    )
    tapped = fr._service_row_title(_SHORT)
    res = await fr.route(conv, _tenant(), None, tapped)
    # It reached the service's own detail card, not the LLM fallback.
    assert res.action == "reply"
    assert res.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert res.flow_selected_type == _SHORT
    # ...and that card offers the decorated confirm/decline pair.
    assert res.bubbles[0].confirm_label == fr.LABEL_BOOK_SERVICE
    assert res.bubbles[0].cancel_label == fr.LABEL_OTHER_SERVICE
