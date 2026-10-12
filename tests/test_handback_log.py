"""The hand-back event: one shape, a closed vocabulary, no values (TASK-030 P1)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.logging import redact_secrets  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402


class _LogRecorder:
    """Records every structlog call (`caplog` is empty for structlog in this suite)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(handback_log, "logger", recorder)
    return recorder


def _only_event(log: _LogRecorder) -> dict:
    assert [event for _level, event, _fields in log.records] == [handback_log.EVENT_NAME]
    level, _event, fields = log.records[0]
    assert level == "info"
    return fields


def test_the_event_carries_ids_as_strings_and_names_as_lists(log: _LogRecorder) -> None:
    conversation_id, tenant_id = uuid4(), uuid4()

    handback_log.log_handback_entered(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        source_tool="set_booking_draft",
        landing_step="awaiting_day",
        supplied=("service", "professional", "insurance"),
        accepted=("service", "professional"),
        dropped={"insurance": "unmatched_plan"},
        fallback=None,
        topology="multi",
        channel="whatsapp",
    )

    assert _only_event(log) == {
        "conversation_id": str(conversation_id),
        "tenant_id": str(tenant_id),
        "source_tool": "set_booking_draft",
        "landing_step": "awaiting_day",
        "supplied": ["service", "professional", "insurance"],
        "accepted": ["service", "professional"],
        "dropped": {"insurance": "unmatched_plan"},
        "fallback": None,
        "topology": "multi",
        "channel": "whatsapp",
    }


def test_optional_arguments_default_to_empty_and_none(log: _LogRecorder) -> None:
    handback_log.log_handback_entered(
        conversation_id=None,
        tenant_id=None,
        source_tool="show_main_menu",
        landing_step=None,
    )

    assert _only_event(log) == {
        "conversation_id": None,
        "tenant_id": None,
        "source_tool": "show_main_menu",
        "landing_step": None,
        "supplied": [],
        "accepted": [],
        "dropped": {},
        "fallback": None,
        "topology": None,
        "channel": None,
    }


def test_a_value_outside_the_vocabulary_is_logged_as_other_never_as_itself(
    log: _LogRecorder,
) -> None:
    leak = "Maria Silva maria@example.com"

    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool=leak,
        landing_step=leak,
        supplied=(leak, "service"),
        accepted=[leak],
        dropped={leak: leak},
        fallback=leak,
        topology=leak,
        channel=leak,
    )

    fields = _only_event(log)
    assert leak not in repr(fields)
    assert fields["source_tool"] == "other"
    assert fields["landing_step"] == "other"
    assert fields["supplied"] == ["other", "service"]
    assert fields["accepted"] == ["other"]
    assert fields["dropped"] == {"other": "other"}
    assert fields["fallback"] == "other"
    assert fields["topology"] == "other"
    assert fields["channel"] == "other"


def test_logging_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    warned: list[str] = []

    class _Broken:
        def info(self, *_args, **_kwargs) -> None:
            raise RuntimeError("log backend down")

        def warning(self, event: str, **_fields) -> None:
            warned.append(event)

    monkeypatch.setattr(handback_log, "logger", _Broken())

    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool="show_main_menu",
        landing_step="menu",
    )

    assert warned == ["conversation_handback_log_failed"]


def test_no_field_is_blanked_by_the_redaction_processor(log: _LogRecorder) -> None:
    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool="manage_existing_appointment",
        landing_step="manage_cancel_confirm",
        supplied=("action",),
        accepted=("action",),
        dropped={"service": "not_in_catalog"},
        fallback="no_appointments",
        topology="sole",
        channel="brain_message",
    )
    fields = _only_event(log)

    scrubbed = redact_secrets(None, "info", {"event": handback_log.EVENT_NAME, **fields})

    assert scrubbed == {"event": handback_log.EVENT_NAME, **fields}


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            FlowRouterResult(
                action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_day"
            ),
            ("awaiting_day", None),
        ),
        (FlowRouterResult(action="reply", flow_state=FlowState.MENU), ("menu", None)),
        (FlowRouterResult(action="reply", flow_state=FlowState.IDLE), ("idle", None)),
        (
            FlowRouterResult(
                action="calendar_unavailable",
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step="awaiting_day",
            ),
            ("human_handover", "calendar_unavailable"),
        ),
        (
            FlowRouterResult(action="professional_config_incomplete", flow_state=FlowState.IDLE),
            ("config_incomplete", "professional_config_incomplete"),
        ),
    ],
)
def test_landing_of_names_the_step_or_the_pseudo_step(
    result: FlowRouterResult, expected: tuple[str, str | None]
) -> None:
    assert handback_log.landing_of(result) == expected


@pytest.mark.parametrize(
    "prefix, vocabulary",
    [
        ("SOURCE_", "SOURCE_TOOLS"),
        ("FALLBACK_", "FALLBACK_REASONS"),
        ("DROP_", "DROP_REASONS"),
        ("FIELD_", "FIELD_NAMES"),
    ],
)
def test_every_constant_is_in_its_vocabulary_and_back(prefix: str, vocabulary: str) -> None:
    constants = {
        value
        for name, value in vars(handback_log).items()
        if name.startswith(prefix) and name != vocabulary and isinstance(value, str)
    }
    assert constants == set(getattr(handback_log, vocabulary))


def test_source_tools_are_the_six_agent_hand_backs() -> None:
    assert handback_log.SOURCE_TOOLS == {
        "set_booking_draft",
        "show_main_menu",
        "manage_existing_appointment",
        "select_professional",
        "start_guided_booking",
        "request_human_handoff",
    }
