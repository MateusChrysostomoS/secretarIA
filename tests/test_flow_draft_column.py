"""Conversation.flow_draft: the AI draft parked while pra-quem is answered (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import importlib.util  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_OTHER,
)
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests.test_flow_router import _conversation, _tenant  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "e7d3c1a9b5f2"
RECORD = draft_record(
    BookingDraft(service="Primeira Consulta", attendee="other"),
    saved_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
)


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_draft.py"
    spec = importlib.util.spec_from_file_location("flow_draft_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_reminder_foundation():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == "b8d3f1a6c2e5"


def test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'flow_draft.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_draft"]["nullable"] is True
            assert conn.execute(sa.text("SELECT flow_draft FROM conversations")).scalar() is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_draft" not in names
    engine.dispose()


def _parked(step, **kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_selected_type=fr.ATTENDEE_NEXT_BOOK,
        flow_draft=RECORD,
        **kw,
    )


async def test_the_draft_rides_every_attendee_step():
    tenant = _tenant()
    name_request = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_CHOICE), tenant, None, LABEL_ATTENDEE_OTHER
    )
    assert name_request.flow_step == fr.STEP_AWAITING_ATTENDEE_NAME
    assert name_request.flow_draft == RECORD

    card = await fr.route(_parked(fr.STEP_AWAITING_ATTENDEE_NAME), tenant, None, "maria da silva")
    assert card.flow_step == fr.STEP_AWAITING_ATTENDEE_AUTH
    assert card.flow_draft == RECORD

    # The patient gives up on the authorization: back to the question, draft kept.
    back = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_AUTH, flow_attendee_name="Maria da Silva"),
        tenant,
        None,
        LABEL_ATTENDEE_AUTH_BACK,
    )
    assert back.flow_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    assert back.flow_draft == RECORD

    free_text = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_CHOICE), tenant, None, "hmm, deixa eu pensar"
    )
    assert free_text.action == "delegate_llm"
    assert free_text.flow_draft == RECORD


async def test_the_draft_is_dropped_by_anything_that_leaves_the_attendee_steps():
    tenant = _tenant()
    on_service = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE,
        flow_draft=RECORD,
    )
    detail = await fr.route(on_service, tenant, None, "Primeira Consulta")
    assert detail.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert detail.flow_draft is None

    llm = await fr.route(
        _conversation(flow_state=FlowState.LLM, flow_draft=RECORD), tenant, None, "oi"
    )
    assert llm.flow_draft is None


def _stale(state, step):
    return SimpleNamespace(
        flow_state=state,
        flow_step=step,
        flow_selected_type=fr.ATTENDEE_NEXT_BOOK,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Maria da Silva",
        flow_draft=RECORD,
    )


def test_the_attendee_step_floor_drops_the_draft():
    conversation = _stale(FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_ATTENDEE_AUTH)
    stale = datetime.now(UTC) - timedelta(days=3)
    assert tasks._expire_stale_attendee_step(conversation, SimpleNamespace(initial_flows={}), stale)
    assert conversation.flow_draft is None
    assert conversation.flow_attendee_name is None


def test_the_llm_floor_drops_the_draft():
    conversation = _stale(FlowState.LLM, None)
    stale = datetime.now(UTC) - timedelta(days=3)
    assert tasks._expire_stale_llm_state(conversation, SimpleNamespace(initial_flows={}), stale)
    assert conversation.flow_draft is None
