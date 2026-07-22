"""Unit tests for worker helper functions (no DB / network)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services.email import send_calendar_alert  # noqa: E402
from secretaria.services.tenant_config import RuntimeAppointmentType  # noqa: E402
from secretaria.workers.tasks import (  # noqa: E402
    _appointment_context_text,
    _is_rate_limited,
    _manage_owner_calendar_target,
    _render_greeting_template,
    _should_inject_appointment_context,
    extract_patient_name,
)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("meu nome é João", "João"),
        ("oi, meu nome é João Silva e quero marcar", "João Silva"),
        ("me chamo MARIA", "Maria"),
        ("pode me chamar de Zé", "Zé"),
        ("meu nome é ana paula maria de souza", "Ana Paula Maria"),
        ("bom dia", None),
        ("quero agendar amanhã", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_patient_name(body, expected):
    assert extract_patient_name(body) == expected


def test_render_greeting_template_with_name():
    out = _render_greeting_template("Olá {{name}}, que bom te ver!", "João")
    assert out == "Olá João, que bom te ver!"


def test_render_greeting_template_without_name_cleans_spacing():
    out = _render_greeting_template("Olá {{name}}, que bom te ver!", None)
    assert out == "Olá, que bom te ver!"


class _FakeRedis:
    """Minimal async Redis stub covering the commands _is_rate_limited uses."""

    def __init__(self):
        self.store: dict[str, object] = {}

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def setex(self, key, seconds, value):
        self.store[key] = value


async def test_rate_limit_allows_under_cap_then_silences():
    redis = _FakeRedis()
    # Default RATE_LIMIT_MAX_MESSAGES is 10: first 10 pass, 11th trips silence.
    results = [await _is_rate_limited(redis, "pn", "55119999") for _ in range(11)]
    assert results[:10] == [False] * 10
    assert results[10] is True
    # Once silenced, further messages stay blocked.
    assert await _is_rate_limited(redis, "pn", "55119999") is True


async def test_rate_limit_disabled_without_redis():
    assert await _is_rate_limited(None, "pn", "55119999") is False


async def test_rate_limit_is_per_sender():
    redis = _FakeRedis()
    for _ in range(11):
        await _is_rate_limited(redis, "pn", "aaa")
    # A different wa_id has its own counter and is not affected.
    assert await _is_rate_limited(redis, "pn", "bbb") is False


# ---------------------------------------------------------------------------
# Calendar alert email (send_calendar_alert)
# ---------------------------------------------------------------------------


async def test_calendar_alert_no_smtp_host_skips_silently():
    """When SMTP_HOST is empty the function returns without trying to connect."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        mock_settings.return_value.SMTP_HOST = ""
        # asyncio.to_thread must NOT be called — if it is the test would hang.
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_calendar_alert("owner@example.com", "Clínica Teste")
            mock_thread.assert_not_called()


async def test_calendar_alert_sends_when_smtp_configured():
    """When SMTP_HOST is set, asyncio.to_thread is called with the right args."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        cfg = mock_settings.return_value
        cfg.SMTP_HOST = "smtp.example.com"
        cfg.SMTP_PORT = 587
        cfg.SMTP_USERNAME = "user@example.com"
        cfg.SMTP_PASSWORD = "secret"
        cfg.SMTP_FROM_EMAIL = "noreply@example.com"
        cfg.SMTP_FROM_NAME = "SecretarIA"
        cfg.SMTP_USE_TLS = True

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_calendar_alert("owner@clinic.com", "Clínica Exemplo")
            mock_thread.assert_called_once()
            # First positional arg to to_thread is the sync function
            sync_fn = mock_thread.call_args.args[0]
            assert sync_fn.__name__ == "_send_sync"
            assert mock_thread.call_args.args[1] == "owner@clinic.com"


async def test_calendar_alert_swallows_smtp_error():
    """A send failure does not raise — it is logged and swallowed."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        mock_settings.return_value.SMTP_HOST = "smtp.example.com"
        mock_settings.return_value.SMTP_PORT = 587
        mock_settings.return_value.SMTP_USERNAME = ""
        mock_settings.return_value.SMTP_PASSWORD = ""
        mock_settings.return_value.SMTP_FROM_EMAIL = ""
        mock_settings.return_value.SMTP_FROM_NAME = ""
        mock_settings.return_value.SMTP_USE_TLS = False

        with patch(
            "asyncio.to_thread", new_callable=AsyncMock, side_effect=OSError("conn refused")
        ):
            # Must not raise
            await send_calendar_alert("owner@clinic.com", "Clínica Exemplo")


# ---------------------------------------------------------------------------
# Owning-professional calendar for the manage flow (_manage_owner_calendar_target)
# ---------------------------------------------------------------------------
#
# Pure function (no DB/network) - given the conversation snapshot's manage-flow
# target, the upcoming_appointments dicts and the tenant's active-professional
# rows, decides whose calendar a cancel/reschedule should act on.


def _professional_row(prof_id):
    """A duck-typed stand-in for a `Professional` ORM row - the helper only
    ever reads `.id` off these, so a SimpleNamespace is enough."""
    return SimpleNamespace(id=prof_id)


def test_manage_owner_calendar_target_none_outside_manage_booking():
    # Not MANAGE_BOOKING at all -> None regardless of anything else.
    assert _manage_owner_calendar_target(FlowState.IDLE, uuid4(), [], []) is None
    assert _manage_owner_calendar_target(FlowState.SERVICE_CATALOG, uuid4(), [], []) is None


def test_manage_owner_calendar_target_none_without_managing_id():
    # MANAGE_BOOKING but no target picked yet (e.g. the entry turn) -> None.
    assert _manage_owner_calendar_target(FlowState.MANAGE_BOOKING, None, [], []) is None


def test_manage_owner_calendar_target_returns_owning_professional():
    # The managed appointment belongs to professional B. Note there is no
    # flow_selected_professional_id parameter at all here - the helper can
    # never see a stale booking-flow selection ("A"), so it has no way to
    # let one leak into the manage-turn's calendar choice.
    professional_a, professional_b = uuid4(), uuid4()
    appt_id = uuid4()
    upcoming = [{"id": str(appt_id), "professional_id": str(professional_b)}]
    rows = [_professional_row(professional_a), _professional_row(professional_b)]

    target = _manage_owner_calendar_target(FlowState.MANAGE_BOOKING, appt_id, upcoming, rows)
    assert target is rows[1]
    assert target.id == professional_b


def test_manage_owner_calendar_target_no_professional_returns_tenant_literal():
    appt_id = uuid4()
    upcoming = [{"id": str(appt_id), "professional_id": None}]
    target = _manage_owner_calendar_target(FlowState.MANAGE_BOOKING, appt_id, upcoming, [])
    assert target == "tenant"


def test_manage_owner_calendar_target_appointment_not_found_returns_none():
    # The managing id doesn't match anything in the loaded snapshot (stale
    # list) -> None, so the caller falls back to its existing resolution.
    target = _manage_owner_calendar_target(FlowState.MANAGE_BOOKING, uuid4(), [], [])
    assert target is None


def test_manage_owner_calendar_target_owning_professional_not_active_returns_none():
    # The appointment has a professional_id, but that professional isn't in
    # the active roster passed in (e.g. deactivated) -> None, not "tenant".
    appt_id = uuid4()
    upcoming = [{"id": str(appt_id), "professional_id": str(uuid4())}]
    target = _manage_owner_calendar_target(FlowState.MANAGE_BOOKING, appt_id, upcoming, [])
    assert target is None


# ---------------------------------------------------------------------------
# Appointment-context injection gate (_should_inject_appointment_context)
# ---------------------------------------------------------------------------
#
# Pure gate, same shape as _should_inject_post_consult_knowledge (see
# test_post_consult_injection.py): an empty/None appointment list always
# loses; otherwise the turn qualifies on flow_state == LLM OR delegated_to_llm.

_ONE_APPT = [{"id": "a1"}]


def test_should_inject_appointment_context_empty_list_is_false():
    assert _should_inject_appointment_context([], FlowState.LLM, True) is False


def test_should_inject_appointment_context_none_is_false():
    assert _should_inject_appointment_context(None, FlowState.LLM, True) is False


def test_should_inject_appointment_context_flow_state_llm_qualifies():
    assert _should_inject_appointment_context(_ONE_APPT, FlowState.LLM, False) is True


def test_should_inject_appointment_context_delegated_this_turn_qualifies():
    """The deterministic router just delegated THIS turn (e.g. the "Outro" tap)."""
    assert _should_inject_appointment_context(_ONE_APPT, FlowState.MENU, True) is True


def test_should_inject_appointment_context_neither_condition_is_false():
    assert _should_inject_appointment_context(_ONE_APPT, FlowState.MENU, False) is False
    assert _should_inject_appointment_context(_ONE_APPT, FlowState.IDLE, False) is False


def test_should_inject_appointment_context_none_flow_state_not_delegated_is_false():
    """flow_state unresolved (e.g. no tenant/conversation), ordinary turn."""
    assert _should_inject_appointment_context(_ONE_APPT, None, False) is False


# ---------------------------------------------------------------------------
# Appointment-context rendering (_appointment_context_text)
# ---------------------------------------------------------------------------

_TZ = "America/Sao_Paulo"  # UTC-3, no DST since 2019 - fixed offset in tests.


def _appt(start_at, appointment_type="Consulta Geral", professional_id=None):
    return {
        "id": str(uuid4()),
        "google_event_id": "evt",
        "appointment_type": appointment_type,
        "start_at": start_at,
        "end_at": start_at + timedelta(minutes=30),
        "professional_id": professional_id,
    }


def test_appointment_context_text_none_on_empty():
    assert _appointment_context_text([], _TZ, {}, []) is None


def test_appointment_context_text_nearest_line_has_service_and_doctor():
    prof_id = str(uuid4())
    start = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # 11:00 America/Sao_Paulo
    text = _appointment_context_text(
        [_appt(start, professional_id=prof_id)], _TZ, {prof_id: "Dra. Ana"}, []
    )
    assert text == "Próxima consulta: 03/08 às 11:00 — Consulta Geral — Dra. Ana"


def test_appointment_context_text_includes_price_and_orientacoes_when_matched():
    start = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    types = [
        RuntimeAppointmentType(
            name="Consulta Geral",
            description=None,
            duration_min=30,
            price="R$ 200",
            requirements=["Jejum de 8 horas", "Trazer exames anteriores"],
        )
    ]
    text = _appointment_context_text([_appt(start)], _TZ, {}, types)
    lines = text.splitlines()
    assert lines[0] == "Próxima consulta: 03/08 às 11:00 — Consulta Geral — R$ 200"
    assert lines[1] == "Orientações: Jejum de 8 horas; Trazer exames anteriores"


def test_appointment_context_text_orientacoes_only_when_nearest_service_matches():
    start = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    types = [
        RuntimeAppointmentType(
            name="Consulta Geral",
            description=None,
            duration_min=30,
            requirements=["Jejum de 8 horas"],
        )
    ]
    # Nearest appointment's service name does NOT match the catalog entry.
    text = _appointment_context_text(
        [_appt(start, appointment_type="Consulta Desconhecida")], _TZ, {}, types
    )
    assert "Orientações" not in text
    assert "R$" not in text


def test_appointment_context_text_brief_line_per_other_appointment():
    nearest = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # 11:00 local
    other = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)  # 06:00 local
    prof_id = str(uuid4())
    text = _appointment_context_text(
        [_appt(nearest), _appt(other, appointment_type="Retorno", professional_id=prof_id)],
        _TZ,
        {prof_id: "Dr. Bruno"},
        [],
    )
    lines = text.splitlines()
    assert lines[0] == "Próxima consulta: 03/08 às 11:00 — Consulta Geral"
    assert lines[1] == "10/08 às 06:00 — Retorno — Dr. Bruno"


def test_appointment_context_text_inactive_owner_shows_no_name():
    """professional_names is built from the ACTIVE roster only - an owner
    absent from it (e.g. deactivated) simply renders with no doctor name."""
    start = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    text = _appointment_context_text(
        [_appt(start, professional_id=str(uuid4()))], _TZ, {}, []
    )
    assert text == "Próxima consulta: 03/08 às 11:00 — Consulta Geral"
