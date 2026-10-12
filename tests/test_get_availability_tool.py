"""get_availability: free WINDOWS only, never an event (TASK-030 P4, spec §4.6, criterion 5).

Three layers, all without Google or OpenAI:

  * the answer's SHAPE and the canary: a REAL `CalendarService` over a fake Google client whose
    events carry a unique string in every field (title, attendee, id, description, link) - the
    string must appear nowhere in what the AI receives, and no key outside the documented set
    may appear;
  * resolution and refusals with fake agendas (which doctor, which service, which days, which
    failures), with the clock pinned to a fixed anchor;
  * tenant isolation against a real in-memory database: two tenants, same-named doctors, holds.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from pseudonymize_core import Pseudonymizer  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    availability_tool as at,
    graph,
    pii,
    tools as ai_tools,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    pii_pseudonymization as store,
)
from secretaria.services.calendar import (  # noqa: E402
    CalendarService,
    CalendarUnavailableError,
    GoogleTokenRevokedError,
)
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # Monday 09:00 in São Paulo
TUESDAY, WEDNESDAY = date(2026, 10, 6), date(2026, 10, 7)
FREE = {TUESDAY: ["08:00", "08:30", "09:00", "10:00"], WEDNESDAY: ["14:00", "14:30"]}
CANARY = "CANARY-5e2c8b"

DOCUMENTED_KEYS = {
    "windows",
    "timezone",
    "slot_minutes",
    "day_from",
    "day_to",
    "professional",
    "clamped",
    "note",
}


class _Clock(datetime):
    """`availability_tool.datetime`: the clinic's today is whatever the test says."""

    now_utc = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.now_utc.astimezone(tz) if tz else cls.now_utc.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    _Clock.now_utc = NOW
    monkeypatch.setattr(at, "datetime", _Clock)
    return _Clock


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log

    def named(self, event):
        return [fields for name, fields in self.events if name == event]


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(at, "logger", recorder)
    return recorder


class _Agenda:
    """A fixed free-time table behind the calendar surface `services/availability.py` reads."""

    def __init__(self, free=None, *, minutes=30, tz=TZ, error: Exception | None = None):
        self.tzinfo = tz
        self.default_slot_minutes = minutes
        self.free = FREE if free is None else free
        self.error = error
        self.day_scans = 0
        self.slot_reads: list[tuple[date, int | None]] = []
        self.asked_minutes: list[int | None] = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.error is not None:
            raise self.error
        self.day_scans += 1
        self.asked_minutes.append(slot_minutes)
        first = start_day.date()
        wanted = {first + timedelta(days=i) for i in range(days)}
        return [
            datetime(d.year, d.month, d.day, tzinfo=self.tzinfo)
            for d in sorted(self.free)
            if d in wanted and self.free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.error is not None:
            raise self.error
        self.slot_reads.append((day.date(), slot_minutes))
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self.free.get(day.date(), [])[:max_slots]
        ]


def _config(tenant_id, timezone="America/Sao_Paulo", types=()) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinica",
        language="pt-BR",
        timezone=timezone,
        appointment_duration_min=30,
        appointment_types=list(types),
        business_hours={},
        google_calendar_id="tenant-cal",
        google_refresh_token="refresh-token",
    )


@contextmanager
def _turn(tenant_id, *, calendar=None, config=None, conversation_id=None):
    """What graph.run_agent sets before the agent runs."""
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._calendar_ctx, calendar),
        (ai_tools._tenant_config_ctx, config),
        (ai_tools._conversation_id_ctx, conversation_id),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _ask(**args):
    return await at.get_availability.ainvoke(args)


def _doctor(name):
    return SimpleNamespace(id=uuid4(), name=name)


_CONSULTA = {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}
_RETORNO = {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 1}
_CIRURGIA = {"name": "Cirurgia", "duration_min": 90, "is_active": True, "sort_order": 2}


class _Clinic:
    """A roster, per-doctor catalogs and per-doctor agendas, patched into the plugin helpers."""

    def __init__(self, monkeypatch, tenant_id):
        self.tenant_id = tenant_id
        self.roster: list = []
        self.catalog: dict = {}
        self.agendas: dict = {}
        self.calendars_built: list = []

        async def _active(asked_tenant_id):
            assert asked_tenant_id == tenant_id
            return list(self.roster)

        async def _services(asked_tenant_id, professional):
            assert asked_tenant_id == tenant_id
            return list(self.catalog.get(professional.id, [_CONSULTA]))

        async def _calendar(asked_tenant_id, professional):
            assert asked_tenant_id == tenant_id
            self.calendars_built.append(professional.id)
            return self.agendas.setdefault(professional.id, _Agenda())

        monkeypatch.setattr(mp, "_active_professionals", _active)
        monkeypatch.setattr(mp, "_professional_services", _services)
        monkeypatch.setattr(mp, "_professional_calendar", _calendar)

    def add(self, name, *services, agenda=None):
        doctor = _doctor(name)
        self.roster.append(doctor)
        if services:
            self.catalog[doctor.id] = list(services)
        if agenda is not None:
            self.agendas[doctor.id] = agenda
        return doctor


@pytest.fixture
def clinic(monkeypatch):
    return _Clinic(monkeypatch, uuid4())


# --------------------------------------------------------------------------
# The answer: shape, time zone, professional, duration
# --------------------------------------------------------------------------


async def test_the_answer_is_windows_and_only_the_documented_keys(clinic):
    doctor = clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda, config=_config(clinic.tenant_id)):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-08")
    assert set(result) <= DOCUMENTED_KEYS
    assert result == {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:30"},
            {"day": "2026-10-06", "start": "10:00", "end": "10:30"},
            {"day": "2026-10-07", "start": "14:00", "end": "15:00"},
        ],
        "timezone": "America/Sao_Paulo",
        "slot_minutes": 30,
        "day_from": "2026-10-05",
        "day_to": "2026-10-08",
        "professional": doctor.name,
    }
    assert all(set(window) == {"day", "start", "end"} for window in result["windows"])
    json.dumps(result)  # serializable as it is


async def test_a_clinic_without_professionals_has_no_professional_key(clinic):
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id)):
        result = await _ask()
    assert "professional" not in result
    assert result["windows"]


async def test_the_service_decides_the_slot_length(clinic):
    doctor = clinic.add("Dra. Única", _CONSULTA, _CIRURGIA)
    agenda = _Agenda(minutes=30)
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(service=" cirurgia ")  # canonical, case/space-insensitive
    assert result["slot_minutes"] == 90
    assert set(agenda.asked_minutes) == {90}
    assert {minutes for _day, minutes in agenda.slot_reads} == {90}
    assert result["professional"] == doctor.name


async def test_without_a_service_the_agendas_default_duration_is_used(clinic):
    clinic.add("Dra. Única", _CONSULTA, _CIRURGIA)
    agenda = _Agenda(minutes=45)
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask()
    assert result["slot_minutes"] == 45


async def test_a_doctor_with_no_free_window_gets_an_empty_list_and_a_sentence(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(free={})):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-12")
    assert result["windows"] == []
    assert "clamped" not in result
    assert result["note"] == "Nenhum horário livre de 05/10 a 12/10."


async def test_the_timezone_is_the_clinics_even_when_it_is_not_utc(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(tz=ZoneInfo("America/New_York"))):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert result["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "now_utc, tz_name, clinic_today, differs_from_utc",
    [
        # 22:30-23:30 the evening before in São Paulo: the UTC date is already tomorrow.
        (datetime(2026, 10, 3, 2, 0, tzinfo=UTC), "America/Sao_Paulo", "2026-10-02", True),
        # The same UTC time of day in New York lands on different dates across a DST change.
        (datetime(2037, 7, 1, 4, 30, tzinfo=UTC), "America/New_York", "2037-07-01", False),  # UTC-4
        (datetime(2037, 12, 1, 4, 30, tzinfo=UTC), "America/New_York", "2037-11-30", True),  # UTC-5
    ],
)
async def test_an_omitted_first_day_is_the_clinics_today_not_the_servers(
    clinic, clock, now_utc, tz_name, clinic_today, differs_from_utc
):
    clock.now_utc = now_utc
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(free={}, tz=ZoneInfo(tz_name))):
        result = await _ask()
    assert result["day_from"] == clinic_today
    assert (now_utc.date().isoformat() != clinic_today) is differs_from_utc


def _real_calendar(monkeypatch, *, hours, minutes, items=(), now_utc=NOW):
    """A real CalendarService (its own slot walk) over a fake Google client, clock pinned."""
    from secretaria.services import calendar as calendar_module

    class _CalendarClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc.astimezone(tz) if tz else now_utc.replace(tzinfo=None)

    monkeypatch.setattr(calendar_module, "datetime", _CalendarClock)
    settings = SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    real = CalendarService(settings=settings)
    real._business_hours = hours
    real._default_slot_minutes = minutes
    monkeypatch.setattr(real, "_service", _GoogleService(list(items)))
    return real


async def test_today_with_hours_already_passed_goes_through_the_real_calendar(
    clinic, clock, monkeypatch
):
    """At 14:10 clinic time the 14:00 slot is gone and 15:00 is not."""
    afternoon = datetime(2026, 10, 5, 17, 10, tzinfo=UTC)
    clock.now_utc = afternoon
    real = _real_calendar(
        monkeypatch,
        hours={
            "monday": [{"start": "08:00", "end": "18:00"}],
            "tuesday": [{"start": "08:00", "end": "18:00"}],
        },
        minutes=60,
        now_utc=afternoon,
    )
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=real):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-06")
    assert result["windows"] == [
        {"day": "2026-10-05", "start": "15:00", "end": "18:00"},
        {"day": "2026-10-06", "start": "08:00", "end": "18:00"},
    ]


# --------------------------------------------------------------------------
# No event field of any kind leaves the calendar layer (success criterion 5)
# --------------------------------------------------------------------------


class _GoogleEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _canary_event(start, end):
    return {
        "id": f"evt-{CANARY}",
        "summary": f"Consulta - {CANARY} Silva",
        "description": f"Notas privadas de {CANARY}",
        "location": f"Sala {CANARY}",
        "htmlLink": f"https://calendar.example/{CANARY}",
        "attendees": [{"email": f"{CANARY}@example.com", "displayName": f"{CANARY} Silva"}],
        "creator": {"email": f"{CANARY}@example.com"},
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


async def test_a_calendar_event_never_reaches_the_ai_in_any_field(clinic, monkeypatch):
    clinic.add("Dra. Única")
    events = [_canary_event("2026-10-06T09:00:00-03:00", "2026-10-06T10:00:00-03:00")]
    real = _real_calendar(
        monkeypatch,
        hours={"tuesday": [{"start": "08:00", "end": "12:00"}]},
        minutes=60,
        items=events,
    )
    with _turn(clinic.tenant_id, calendar=real):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")

    # The busy hour is simply not offered ...
    assert result["windows"] == [
        {"day": "2026-10-06", "start": "08:00", "end": "09:00"},
        {"day": "2026-10-06", "start": "10:00", "end": "12:00"},
    ]
    # ... and nothing of the event survives, in any form.
    assert set(result) <= DOCUMENTED_KEYS
    assert CANARY not in json.dumps(result)
    assert CANARY not in repr(result)
    assert not any(key in result for key in ("busy", "events", "summary", "attendees", "id"))


async def test_the_pseudonymization_guard_wraps_the_tool_and_leaves_windows_intact(clinic):
    clinic.add("Dra. Única")
    (wrapped,) = pii.wrap_tools_with_pseudonymizer([at.get_availability])
    assert wrapped.coroutine is not at.get_availability.coroutine  # the guard is on
    assert (wrapped.name, wrapped.args) == (at.get_availability.name, at.get_availability.args)

    mapper = Pseudonymizer()
    mapper.add_identifier("PACIENTE", "Maria Souza")
    store_token = store._pseudonymizer_ctx.set(mapper)
    try:
        with _turn(clinic.tenant_id, calendar=_Agenda()):
            guarded = await wrapped.ainvoke({"day_from": "2026-10-06", "day_to": "2026-10-07"})
        with _turn(clinic.tenant_id, calendar=_Agenda()):
            plain = await _ask(day_from="2026-10-06", day_to="2026-10-07")
    finally:
        store._pseudonymizer_ctx.reset(store_token)
    assert guarded == plain


def test_the_agent_builds_the_tool_behind_the_guard(monkeypatch):
    class _Recorded:
        def __init__(self, tools):
            self.tools = tools

    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph, "create_react_agent", lambda m, tools, prompt: _Recorded(list(tools))
    )
    try:
        # `toolset_v2=` only exists from Task 7 on; the pseudonymization guard wraps every
        # tool of every agent, so the plain build already proves it.
        agent = graph.build_agent([at.get_availability], "sole")
    finally:
        graph._AGENTS.clear()
    (built,) = [t for t in agent.tools if t.name == "get_availability"]
    assert built.coroutine is not at.get_availability.coroutine


def test_the_model_facing_contract_is_pinned_for_the_prompt_writer():
    tool = at.get_availability
    assert tool.name == "get_availability"
    assert set(tool.args) == {"professional", "service", "day_from", "day_to"}
    text = tool.description
    for phrase in (
        "HORÁRIOS LIVRES",
        "{day, start, end}",
        "Nunca devolve compromissos, nomes nem horários ocupados",
        "set_booking_draft",
        "create_event",
        "só preparam o cartão de confirmação",
        "14 dias e 30 janelas",
        "a lista NÃO cobre tudo o que foi pedido",
        "NUNCA invente um horário",
    ):
        assert phrase in text


# --------------------------------------------------------------------------
# Which doctor, which service: resolved like the draft does, or refused
# --------------------------------------------------------------------------


async def test_a_single_professional_clinic_reads_that_professionals_agenda(clinic):
    doctor = clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id)):
        result = await _ask(professional="dra. única")
    assert result["professional"] == doctor.name
    assert clinic.calendars_built == []  # the tenant-level agenda IS theirs


async def test_a_multi_professional_clinic_resolves_by_name_case_insensitively(clinic):
    ana_agenda, beto_agenda = _Agenda({TUESDAY: ["08:00"]}), _Agenda({TUESDAY: ["15:00"]})
    ana = clinic.add("Dra. Ana", agenda=ana_agenda)
    clinic.add("Dr. Beto", agenda=beto_agenda)
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(professional=" DRA. ana ", day_from="2026-10-06", day_to="2026-10-06")
    assert result["professional"] == ana.name
    assert result["windows"][0]["start"] == "08:00"
    assert clinic.calendars_built == [ana.id]


async def test_a_service_only_one_doctor_offers_resolves_to_that_doctor(clinic):
    clinic.add("Dra. Ana", _CONSULTA)
    beto = clinic.add("Dr. Beto", _RETORNO, agenda=_Agenda(minutes=30))
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(service="retorno")
    assert result["professional"] == beto.name
    assert result["slot_minutes"] == 20
    assert clinic.calendars_built == [beto.id]


_ANA, _BETO = ("Dra. Ana", (_CONSULTA,)), ("Dr. Beto", (_RETORNO,))


@pytest.mark.parametrize(
    "roster, args, reason, fragments",
    [
        ([_ANA, _BETO], {}, "professional_required", ["Dra. Ana", "Dr. Beto", "`professional`"]),
        ([_ANA, _BETO], {"professional": "Dr. Fantasma"}, "professional_unknown", ["Dra. Ana"]),
        (
            [_ANA, _BETO, _ANA],
            {"professional": "Dra. Ana"},
            "professional_ambiguous",
            ["inequívoca"],
        ),
        (
            [_ANA, _BETO],
            {"service": "Botox"},
            "no_professional_offers_service",
            ["Nenhum profissional"],
        ),
        (
            [_ANA, ("Dr. Beto", (_CONSULTA,))],
            {"service": "Consulta"},
            "several_professionals_offer_service",
            ["Dra. Ana, Dr. Beto", "`professional`"],
        ),
        (
            [_ANA, _BETO],
            {"professional": "Dr. Beto", "service": "Consulta"},
            "service_unknown",
            ["Retorno"],
        ),
    ],
    ids=["no_hint", "unknown", "duplicate_name", "nobody_offers", "two_offer", "not_his_service"],
)
async def test_a_multi_professional_request_that_cannot_be_resolved_is_a_short_error(
    clinic, log, roster, args, reason, fragments
):
    for name, services in roster:
        clinic.add(name, *services)
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(**args)
    assert set(result) == {"error"}
    for fragment in fragments:
        assert fragment in result["error"]
    # Never an echo of what the model sent: it may carry the patient's own words.
    for sent in args.values():
        assert sent not in result["error"]
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == [reason]
    assert clinic.calendars_built == []
    assert log.named("ai_availability_read") == []


async def test_a_single_professional_clinic_refuses_another_doctors_name(clinic, log):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(professional="Dr. Fantasma")
    assert "Dra. Única" in result["error"] and "Fantasma" not in result["error"]
    assert agenda.day_scans == 0


async def test_a_clinic_without_professionals_refuses_a_professional_name(clinic, log):
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(professional="Dra. Ana")
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_professionals"]


async def test_a_clinic_without_professionals_reads_the_tenant_catalog(clinic):
    types = [RuntimeAppointmentType(name="Consulta", description=None, duration_min=25)]
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id, types=types)):
        result = await _ask(service="consulta")
    assert result["slot_minutes"] == 25


async def test_an_unknown_service_lists_the_clinics_services_without_echoing_it(clinic):
    clinic.add("Dra. Única", _CONSULTA, _RETORNO)
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(service="Rinoplastia")
    assert "Consulta, Retorno" in result["error"]
    assert "Rinoplastia" not in result["error"]


# --------------------------------------------------------------------------
# Days: refused, clamped, absurd
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, reason",
    [
        ({"day_from": "2026-10-04"}, "past_day"),
        ({"day_from": "1900-01-01"}, "past_day"),
        ({"day_from": "2026-10-09", "day_to": "2026-10-08"}, "reversed_range"),
        ({"day_to": "2026-10-04"}, "reversed_range"),
        ({"day_from": "2026-10-25"}, "beyond_window"),
        ({"day_from": "9999-12-31"}, "beyond_window"),
        ({"day_from": "08/10/2026"}, "bad_day_format"),
        ({"day_to": "20261008"}, "bad_day_format"),
        ({"day_from": "amanhã"}, "bad_day_format"),
        ({"day_from": "x" * 5000}, "bad_day_format"),
    ],
)
async def test_an_unusable_day_range_is_an_error_dict_and_never_reads_the_agenda(
    clinic, log, args, reason
):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(**args)
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == [reason]
    assert (agenda.day_scans, agenda.slot_reads) == (0, [])
    for sent in args.values():
        assert sent not in result["error"]


async def test_a_365_day_range_is_clamped_and_the_answer_says_so(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(day_from="2026-10-05", day_to="2027-10-05")
    assert (result["day_from"], result["day_to"]) == ("2026-10-05", "2026-10-18")
    assert result["clamped"] == ["max_days"]
    assert "14 dias" in result["note"] and "05/10" in result["note"] and "18/10" in result["note"]


async def test_the_range_stops_where_booking_stops(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(day_from="2026-10-20", day_to="2026-11-30")
    assert result["day_to"] == "2026-10-24"  # today + 19: the last day the flow accepts
    assert result["clamped"] == ["booking_window"]
    assert "24/10" in result["note"]


async def test_more_than_30_windows_are_cut_and_reported(clinic):
    clinic.add("Dra. Única")
    free = {
        date(2026, 10, 5) + timedelta(days=i): ["08:00", "10:00", "12:00"] for i in range(14)
    }  # 42 separate windows
    with _turn(clinic.tenant_id, calendar=_Agenda(free=free)):
        result = await _ask()
    assert len(result["windows"]) == 30
    assert result["clamped"] == ["max_windows"]
    assert result["day_to"] == result["windows"][-1]["day"] == "2026-10-14"
    assert "30" in result["note"] and "14/10" in result["note"]


# --------------------------------------------------------------------------
# Failures: an outage keeps today's behaviour, anything else is an error dict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("error", [CalendarUnavailableError("down"), GoogleTokenRevokedError("x")])
async def test_a_calendar_outage_propagates_like_every_other_calendar_read(clinic, error):
    """graph.run_agent maps it to the sentinel: a person is called in and the owner alerted."""
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(error=error)):
        with pytest.raises(CalendarUnavailableError):
            await _ask()


@pytest.mark.parametrize("error", [RuntimeError("Google credentials missing"), KeyError("boom")])
async def test_any_other_failure_is_an_error_dict_not_a_crash(clinic, log, error):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(error=error)):
        result = await _ask()
    assert set(result) == {"error"}
    assert "NUNCA invente horários" in result["error"]
    (failed,) = log.named("agent_availability_failed")
    assert failed == {"error_type": type(error).__name__}  # the type, never the message
    assert log.named("ai_availability_read") == []


async def test_a_roster_that_cannot_be_read_is_an_error_dict(clinic, monkeypatch):
    async def _broken(_tenant_id):
        raise ConnectionError("db down")

    monkeypatch.setattr(mp, "_active_professionals", _broken)
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask()
    assert set(result) == {"error"}


async def test_without_a_clinic_there_is_nothing_to_read(log):
    result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_clinic"]


async def test_without_an_agenda_in_context_the_environment_calendar_is_never_used(clinic, log):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=None):
        result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_calendar"]


async def test_a_multi_professional_turn_without_a_tenant_config_is_refused(clinic, log):
    clinic.add("Dra. Ana")
    clinic.add("Dr. Beto")
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=None):
        result = await _ask(professional="Dra. Ana")
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_calendar"]
    assert clinic.calendars_built == []


async def test_a_config_of_another_tenant_is_refused_before_anything_is_read(clinic, log):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda, config=_config(uuid4())):
        result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["tenant_mismatch"]
    assert agenda.day_scans == 0


# --------------------------------------------------------------------------
# The log line: counts only
# --------------------------------------------------------------------------


async def test_one_count_only_event_per_successful_read(clinic, log):
    clinic.add("Dra. Única", _CONSULTA)
    conversation_id = uuid4()
    with _turn(clinic.tenant_id, calendar=_Agenda(), conversation_id=conversation_id):
        await _ask(service="Consulta", day_from="2026-10-06", day_to="2026-10-12")
    (event,) = log.named("ai_availability_read")
    assert event == {
        "tenant_id": str(clinic.tenant_id),
        "conversation_id": str(conversation_id),
        "windows": 3,
        "days_scanned": 7,
        "clamped": False,
        "professional_resolved": True,
    }
    rendered = repr(log.events)
    for value in ("Consulta", "Dra. Única", "2026-10-06", "08:00"):
        assert value not in rendered


# --------------------------------------------------------------------------
# Tenants, holds and the per-professional agenda, against a real database
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


class _CalendarPerId:
    """Stands in for `CalendarService` in ai/tools.py: each calendar id has its own agenda."""

    agendas: dict[str, dict[date, list[str]]] = {}
    built: list[str] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls._make(config.google_calendar_id)

    @classmethod
    def for_professional(cls, tenant_config, *, google_calendar_id=None, **_ignored):
        return cls._make(google_calendar_id or tenant_config.google_calendar_id)

    @classmethod
    def _make(cls, calendar_id):
        cls.built.append(calendar_id)
        agenda = _Agenda(free=cls.agendas.get(calendar_id, {}))
        agenda.calendar_id = calendar_id
        return agenda


@pytest.fixture
def real_roster(monkeypatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    monkeypatch.setattr(ai_tools, "CalendarService", _CalendarPerId)
    _CalendarPerId.agendas = {}
    _CalendarPerId.built = []
    return db


async def _seed_tenant(db, *doctors):
    """A tenant with `doctors` as ("name", "calendar id") active professionals."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            appointment_types=[_CONSULTA],
        )
        session.add(tenant)
        await session.flush()
        rows = [
            Professional(tenant_id=tenant.id, name=name, google_calendar_id=cal, is_active=True)
            for name, cal in doctors
        ]
        session.add_all(rows)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        return tenant, rows


async def test_two_tenants_with_a_same_named_doctor_each_read_their_own_agenda(real_roster):
    tenant_a, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-A"), ("Dr. Beto", "beto-A"))
    tenant_b, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-B"), ("Dr. Caio", "caio-B"))
    _CalendarPerId.agendas = {
        "ana-A": {TUESDAY: ["08:00"]},
        "ana-B": {TUESDAY: ["16:00"]},
    }

    async def _turn_for(tenant):
        with _turn(tenant.id, config=_config(tenant.id)):
            return await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")

    a, b = await asyncio.gather(_turn_for(tenant_a), _turn_for(tenant_b))
    assert [w["start"] for w in a["windows"]] == ["08:00"]
    assert [w["start"] for w in b["windows"]] == ["16:00"]
    assert sorted(_CalendarPerId.built) == ["ana-A", "ana-B"]


async def test_a_doctor_the_tenant_does_not_have_is_unknown_and_no_agenda_is_built(real_roster):
    tenant_a, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-A"), ("Dr. Beto", "beto-A"))
    await _seed_tenant(real_roster, ("Dra. Carla", "carla-B"), ("Dr. Caio", "caio-B"))
    _CalendarPerId.agendas = {"carla-B": {TUESDAY: ["08:00"]}}
    with _turn(tenant_a.id, config=_config(tenant_a.id)):
        result = await _ask(professional="Dra. Carla")
    assert set(result) == {"error"}
    assert "Dra. Ana" in result["error"] and "Carla" not in result["error"]
    assert _CalendarPerId.built == []


async def test_the_per_professional_agenda_comes_from_the_workflows_own_resolution(real_roster):
    tenant, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-cal"), ("Dr. Beto", None))
    _CalendarPerId.agendas = {"ana-cal": {TUESDAY: ["08:00"]}, "tenant-cal": {TUESDAY: ["11:00"]}}
    with _turn(tenant.id, config=_config(tenant.id)):
        own = await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")
        fallback = await _ask(professional="Dr. Beto", day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in own["windows"]] == ["08:00"]  # her own calendar id
    assert [w["start"] for w in fallback["windows"]] == ["11:00"]  # falls back to the tenant's


async def _hold(tenant, professional_id, start_hour, minutes=30, conversation_id=None):
    """Hold `start_hour`:00 clinic time on Tuesday. Stored as UTC: SQLite drops the offset of a
    timestamptz and `booking_hold._aware` reads it back as UTC, so a UTC instant round-trips."""
    start = datetime(2026, 10, 6, start_hour, 0, tzinfo=TZ).astimezone(UTC)
    placed = await booking_hold_service.place_hold(
        tenant_id=tenant.id,
        conversation_id=conversation_id or uuid4(),
        patient_id=None,
        professional_id=professional_id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
    )
    assert placed is not None


async def test_a_slot_another_conversation_is_holding_is_not_offered(real_roster):
    tenant, (sole,) = await _seed_tenant(real_roster, ("Dra. Única", "sole-cal"))
    await _hold(tenant, sole.id, 10)  # holds are PLACED with the sole professional's id
    agenda = _Agenda({TUESDAY: ["09:00", "09:30", "10:00", "10:30"]})
    with _turn(tenant.id, calendar=agenda, config=_config(tenant.id)):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert result["windows"] == [
        {"day": "2026-10-06", "start": "09:00", "end": "10:00"},
        {"day": "2026-10-06", "start": "10:30", "end": "11:00"},
    ]


async def test_the_patients_own_hold_does_not_hide_the_slot_from_them(real_roster):
    tenant, (sole,) = await _seed_tenant(real_roster, ("Dra. Única", "sole-cal"))
    mine = uuid4()
    await _hold(tenant, sole.id, 10, conversation_id=mine)
    with _turn(tenant.id, calendar=_Agenda({TUESDAY: ["10:00"]}), conversation_id=mine):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in result["windows"]] == ["10:00"]


async def test_a_hold_on_another_doctor_hides_nothing_on_this_doctors_agenda(real_roster):
    tenant, (ana, beto) = await _seed_tenant(
        real_roster, ("Dra. Ana", "ana-cal"), ("Dr. Beto", "beto-cal")
    )
    await _hold(tenant, beto.id, 10)
    _CalendarPerId.agendas = {"ana-cal": {TUESDAY: ["10:00"]}, "beto-cal": {TUESDAY: ["10:00"]}}
    with _turn(tenant.id, config=_config(tenant.id)):
        hers = await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")
        his = await _ask(professional="Dr. Beto", day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in hers["windows"]] == ["10:00"]
    assert his["windows"] == []  # the positive control: the hold is read where it belongs


async def test_another_tenants_tenant_level_hold_hides_nothing(real_roster):
    held, _ = await _seed_tenant(real_roster)  # no professionals: the tenant-level agenda
    other, _ = await _seed_tenant(real_roster)
    await _hold(held, None, 10)
    free = {TUESDAY: ["10:00"]}
    with _turn(held.id, calendar=_Agenda(free)):
        mine = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    with _turn(other.id, calendar=_Agenda(free)):
        theirs = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert mine["windows"] == []
    assert [w["start"] for w in theirs["windows"]] == ["10:00"]
