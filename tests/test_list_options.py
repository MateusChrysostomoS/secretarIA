"""TASK-082 S1 - the Portal's paged option lists: resolve, page, search, cursor, contains.

`services/list_options.py` is the ONE place that turns a card's stored pointer
(`kind` + server-only `scope`) back into the ordered catalogue, for both the
options route and the tap check. These tests pin it without a database: the
inputs are the same plain snapshots `flow_router.route()` receives.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from secretaria.services import list_options as lo


def _tenant(*, services: list[str] | None = None, plans: list[dict] | None = None, **extra):
    return SimpleNamespace(
        appointment_types=[
            {"name": name, "duration_min": 30, "is_active": True, "sort_order": i}
            for i, name in enumerate(services or [])
        ],
        insurance_plans=plans,
        insurances=None,
        insurance_mode=extra.get("insurance_mode"),
        insurance_accepted_by=extra.get("insurance_accepted_by", {}),
        collect_insurance=True,
    )


def _doctor(name: str, services: list[str] | None = None, *, specialty: str | None = None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=specialty,
        appointment_types=(
            None
            if services is None
            else [
                {"name": s, "duration_min": 30, "is_active": True, "sort_order": i}
                for i, s in enumerate(services)
            ]
        ),
    )


SERVICES_25 = [f"Serviço {i:02d}" for i in range(25)]


# --------------------------------------------------------------------------
# Cursor: opaque, versioned, bounded
# --------------------------------------------------------------------------


def test_cursor_round_trips_and_has_no_padding() -> None:
    for offset in (0, 10, 990, lo.MAX_CURSOR_OFFSET):
        token = lo.encode_cursor(offset)
        assert "=" not in token
        assert lo.decode_cursor(token) == offset


@pytest.mark.parametrize(
    "bad",
    [
        "not base64 !!",
        lo.encode_cursor(10)[:-2] + "@@",
        # Valid base64url of JSON with the wrong version / shape / range.
        "eyJ2IjoyLCJvIjoxMH0",  # {"v":2,"o":10}
        "eyJ2IjoxLCJvIjotMX0",  # {"v":1,"o":-1}
        "eyJ2IjoxLCJvIjoxMDAxfQ",  # {"v":1,"o":1001}
        "eyJ2IjoxLCJvIjoiMTAifQ",  # {"v":1,"o":"10"}
        "eyJ2IjoxfQ",  # {"v":1}
        "WzEsMl0",  # [1,2]
        "x" * (lo.MAX_CURSOR_CHARS + 1),
        "",
    ],
)
def test_a_tampered_cursor_is_refused(bad: str) -> None:
    with pytest.raises(lo.InvalidCursor):
        lo.decode_cursor(bad)


def test_no_cursor_is_the_first_page() -> None:
    assert lo.decode_cursor(None) == 0


# --------------------------------------------------------------------------
# Paging: first / middle / last / past the end / empty
# --------------------------------------------------------------------------


def _service_page(tenant, *, cursor=None, page_size=10, q=None):
    return lo.options_page(
        lo.KIND_SERVICE,
        {"list": "tenant"},
        tenant,
        [],
        cursor=cursor,
        page_size=page_size,
        q=q,
    )


def test_pages_walk_the_whole_catalogue_in_order() -> None:
    tenant = _tenant(services=SERVICES_25)
    first = _service_page(tenant)
    assert [i["id"] for i in first.items] == [f"svc|{n}" for n in SERVICES_25[:10]]
    assert first.total == 25 and first.next_cursor == lo.encode_cursor(10)

    middle = _service_page(tenant, cursor=first.next_cursor)
    assert [i["id"] for i in middle.items] == [f"svc|{n}" for n in SERVICES_25[10:20]]
    assert middle.next_cursor == lo.encode_cursor(20)

    last = _service_page(tenant, cursor=middle.next_cursor)
    assert [i["id"] for i in last.items] == [f"svc|{n}" for n in SERVICES_25[20:]]
    assert last.next_cursor is None and last.total == 25


def test_an_offset_past_the_end_is_an_empty_page_not_an_error() -> None:
    page = _service_page(_tenant(services=SERVICES_25), cursor=lo.encode_cursor(990))
    assert page.items == [] and page.next_cursor is None and page.total == 25


def test_an_empty_catalogue_is_an_empty_page() -> None:
    page = _service_page(_tenant(services=[]))
    assert page.items == [] and page.next_cursor is None and page.total == 0


def test_page_size_is_honoured() -> None:
    page = _service_page(_tenant(services=SERVICES_25), page_size=3)
    assert len(page.items) == 3 and page.next_cursor == lo.encode_cursor(3)


def test_items_carry_the_full_title_never_the_24_char_cut() -> None:
    long_name = "Consulta oftalmológica completa com mapeamento de retina"
    page = _service_page(_tenant(services=["Retorno", long_name]))
    titles = {i["id"]: i["title"] for i in page.items}
    assert titles[f"svc|{long_name}"] == long_name
    # A short name keeps the same decorated title WhatsApp shows.
    assert titles["svc|Retorno"] == "🏥 Retorno"
    assert set(page.items[0]) == {"id", "title", "description"}


# --------------------------------------------------------------------------
# Search: whole catalogue, accent/case/emoji-insensitive, partial, still paged
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("cardio", ["Cardiologia"]),
        ("AVALIACAO", ["Avaliação inicial"]),
        ("avaliação", ["Avaliação inicial"]),
        ("🏥 retorno", ["Retorno"]),
        ("  retorno   ", ["Retorno"]),
        ("ção ini", ["Avaliação inicial"]),
        ("zzz", []),
    ],
)
def test_search_is_partial_and_insensitive(q: str, expected: list[str]) -> None:
    tenant = _tenant(services=["Avaliação inicial", "Cardiologia", "Retorno"])
    page = _service_page(tenant, q=q)
    assert [i["id"] for i in page.items] == [f"svc|{n}" for n in expected]
    assert page.total == len(expected)


def test_search_reaches_past_the_first_page_and_still_pages() -> None:
    names = [f"Serviço {i:02d}" for i in range(30)] + [f"Exame {i:02d}" for i in range(15)]
    tenant = _tenant(services=names)
    page = _service_page(tenant, q="exame")
    assert page.total == 15
    assert [i["id"] for i in page.items] == [f"svc|Exame {i:02d}" for i in range(10)]
    rest = _service_page(tenant, q="exame", cursor=page.next_cursor)
    assert [i["id"] for i in rest.items] == [f"svc|Exame {i:02d}" for i in range(10, 15)]
    assert rest.next_cursor is None


def test_empty_or_blank_query_means_no_filter() -> None:
    tenant = _tenant(services=SERVICES_25)
    assert _service_page(tenant, q="").total == 25
    assert _service_page(tenant, q="   ").total == 25


def test_a_long_query_is_clamped_not_refused() -> None:
    name = "a" * 70
    tenant = _tenant(services=[name, "b"])
    page = _service_page(tenant, q="A" * 200)
    # Clamped to MAX_QUERY_CHARS, the first 60 "a"s still match the 70-char name.
    assert [i["id"] for i in page.items] == [f"svc|{name}"]
    assert lo.normalize_query("x" * 200) == "x" * lo.MAX_QUERY_CHARS


def test_a_query_of_only_symbols_matches_everything() -> None:
    assert _service_page(_tenant(services=SERVICES_25), q="🏥 ✅").total == 25


# --------------------------------------------------------------------------
# Scopes: each kind resolves the list the router would draw
# --------------------------------------------------------------------------


def test_doctor_scope_lists_only_that_doctors_services() -> None:
    ana = _doctor("Ana", ["Retorno", "Consulta"])
    bia = _doctor("Bia", ["Exame"])
    tenant = _tenant(services=["Legado"])
    items = lo.resolve(
        lo.KIND_SERVICE, {"list": "doctor", "professional_id": str(ana.id)}, tenant, [ana, bia]
    )
    assert [row[0] for row in items] == ["svc|Retorno", "svc|Consulta"]


def test_doctor_scope_of_a_doctor_no_longer_active_is_empty() -> None:
    items = lo.resolve(
        lo.KIND_SERVICE,
        {"list": "doctor", "professional_id": str(uuid4())},
        _tenant(services=["Legado"]),
        [_doctor("Ana", ["Retorno"])],
    )
    assert items == []


def test_clinic_scope_is_the_deduplicated_union() -> None:
    ana = _doctor("Ana", ["Retorno", "Consulta"])
    bia = _doctor("Bia", ["Consulta", "Exame"])
    items = lo.resolve(lo.KIND_SERVICE, {"list": "clinic"}, _tenant(), [ana, bia])
    assert sorted(row[0] for row in items) == ["svc|Consulta", "svc|Exame", "svc|Retorno"]


def test_professional_scope_lists_every_doctor_with_full_names_and_marks() -> None:
    plan_id = str(uuid4())
    long = _doctor("Dra. Ana Beatriz Figueiredo Albuquerque Souza", specialty="Retina")
    other = _doctor("Dr. Bruno", specialty="Glaucoma")
    tenant = _tenant(
        plans=[{"id": plan_id, "name": "Unimed"}],
        insurance_mode="independent",
        insurance_accepted_by={str(long.id): [plan_id]},
    )
    items = lo.resolve(lo.KIND_PROFESSIONAL, {"insurance": "Unimed"}, tenant, [other, long])
    by_id = {row[0]: row for row in items}
    assert by_id[f"prof|{long.id}"][1] == "🥼 Dra. Ana Beatriz Figueiredo Albuquerque Souza"
    assert "Retina" in by_id[f"prof|{long.id}"][2] and "✅" in by_id[f"prof|{long.id}"][2]
    assert by_id[f"prof|{other.id}"][2] == "Glaucoma"


def test_professional_scope_can_be_narrowed_to_a_pool() -> None:
    ana, bia, cai = _doctor("Ana"), _doctor("Bia"), _doctor("Cai")
    items = lo.resolve(
        lo.KIND_PROFESSIONAL,
        {"insurance": None, "only_ids": [str(bia.id), str(cai.id)]},
        _tenant(),
        [ana, bia, cai],
    )
    assert [row[0] for row in items] == [f"prof|{bia.id}", f"prof|{cai.id}"]


def test_insurance_scope_lists_the_plans_in_clinic_order_without_pinned_rows() -> None:
    plans = [{"id": str(uuid4()), "name": f"Plano {i:02d}", "mechanism": None} for i in range(14)]
    items = lo.resolve(lo.KIND_INSURANCE, {}, _tenant(plans=plans), [])
    assert [row[0] for row in items] == [f"ins|Plano {i:02d}" for i in range(14)]
    assert all(row[0] not in ("ins|particular", "ins|outro") for row in items)


def test_an_unknown_kind_or_a_malformed_scope_resolves_to_nothing() -> None:
    tenant = _tenant(services=["Retorno"])
    assert lo.resolve("slot", {}, tenant, []) == []
    assert lo.resolve(lo.KIND_SERVICE, {"list": "nope"}, tenant, []) == []
    assert lo.resolve(lo.KIND_SERVICE, "not a dict", tenant, []) == []  # type: ignore[arg-type]
    assert lo.resolve(lo.KIND_SERVICE, {"list": "doctor", "professional_id": "x"}, tenant, []) == []


# --------------------------------------------------------------------------
# contains: the tap check's re-resolution
# --------------------------------------------------------------------------


def test_contains_accepts_an_item_beyond_the_first_page() -> None:
    tenant = _tenant(services=SERVICES_25)
    assert lo.contains(lo.KIND_SERVICE, {"list": "tenant"}, tenant, [], "svc|Serviço 24")


def test_contains_rejects_a_forged_or_cross_kind_id() -> None:
    ana = _doctor("Ana", ["Retorno"])
    tenant = _tenant(services=SERVICES_25)
    scope = {"list": "tenant"}
    assert not lo.contains(lo.KIND_SERVICE, scope, tenant, [ana], "svc|Anything")
    assert not lo.contains(lo.KIND_SERVICE, scope, tenant, [ana], f"prof|{ana.id}")
    assert not lo.contains(lo.KIND_PROFESSIONAL, {"insurance": None}, tenant, [ana], "prof|x")
    assert lo.contains(lo.KIND_PROFESSIONAL, {"insurance": None}, tenant, [ana], f"prof|{ana.id}")
    stranger = f"prof|{uuid4()}"
    assert not lo.contains(lo.KIND_PROFESSIONAL, {"insurance": None}, tenant, [ana], stranger)


def test_kind_for_reply_id() -> None:
    assert lo.kind_for_reply_id("svc|X") == lo.KIND_SERVICE
    assert lo.kind_for_reply_id(f"prof|{uuid4()}") == lo.KIND_PROFESSIONAL
    assert lo.kind_for_reply_id("ins|Unimed") == lo.KIND_INSURANCE
    assert lo.kind_for_reply_id("slot|2030-01-01") is None
    assert lo.kind_for_reply_id("svchelp|0") is None


# --------------------------------------------------------------------------
# The stored blob: what is kept, what is projected
# --------------------------------------------------------------------------


def test_stored_source_accepts_only_a_well_formed_pointer() -> None:
    good = {"v": 1, "kind": "service", "scope": {"list": "tenant"}, "pinned_ids": [], "total": 3}
    assert lo.stored_source({"kind": "list", "source": good}) == good
    assert lo.stored_source({"kind": "list"}) is None
    assert lo.stored_source(None) is None
    assert lo.stored_source({"source": {**good, "v": 2}}) is None
    assert lo.stored_source({"source": {**good, "kind": "slot"}}) is None
    assert lo.stored_source({"source": {**good, "scope": "x"}}) is None


def test_card_source_record_is_page_one_plus_pointer() -> None:
    from secretaria.ai.formatter import OptionSource

    rows = [(f"svc|{n}", n, None) for n in SERVICES_25]
    source = OptionSource(
        kind=lo.KIND_SERVICE,
        scope={"list": "tenant"},
        items=rows,
        pinned=[("svchelp|0", "Não sei", None)],
    )
    assert lo.first_page_rows(source) == rows[:10] + [("svchelp|0", "Não sei", None)]
    record = lo.card_source(source)
    assert record == {
        "v": 1,
        "kind": "service",
        "scope": {"list": "tenant"},
        "pinned_ids": ["svchelp|0"],
        "total": 25,
        "next_cursor": lo.encode_cursor(10),
    }
    small = OptionSource(kind=lo.KIND_SERVICE, scope={"list": "tenant"}, items=rows[:4])
    assert lo.card_source(small)["next_cursor"] is None


def test_professional_ids_are_uuids() -> None:
    # Sanity on the fixture: prof| ids embed a real UUID, which the router relies on.
    doc = _doctor("Ana")
    assert UUID(str(doc.id))
