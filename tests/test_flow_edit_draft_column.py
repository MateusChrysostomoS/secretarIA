"""conversations.flow_edit_draft + FlowState.EDIT_BOOKING (TASK-032 R6, spec §5.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import importlib.util  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.models import Conversation, FlowState  # noqa: E402
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import state_expiry  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401
from tests._reminders_r3 import get_conversation, set_conversation, wire  # noqa: E402
from tests._reminders_v2 import WA_ID, seed_world  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b1c4e7a2d9f3"
DOWN_REVISION = "a7e2c9d4f1b6"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_edit_draft.py"
    spec = importlib.util.spec_from_file_location("flow_edit_draft_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_r3():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["c2d5f8a1e4b6"]
    assert REVISION in {r.revision for r in scripts.walk_revisions()}
    assert _migration().down_revision == DOWN_REVISION


def test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'edit.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_edit_draft"]["nullable"] is True
            assert (
                conn.execute(sa.text("SELECT flow_edit_draft FROM conversations")).scalar() is None
            )
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_edit_draft" not in names
    engine.dispose()


def test_the_model_and_the_state_exist_without_a_state_migration():
    column = Conversation.__table__.c.flow_edit_draft
    assert column.nullable is True
    assert FlowState.EDIT_BOOKING.value == "EDIT_BOOKING"
    assert len(FlowState.EDIT_BOOKING.value) <= 32  # flow_state is VARCHAR(32), no CHECK


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def test_apply_writes_the_draft_a_result_names_and_clears_it_otherwise(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    draft = {
        "appointment_id": str(world.appointment.id),
        "current": {},
        "original": {},
        "stage": {},
    }
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        flow_edit_draft=draft,
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )
    assert (await get_conversation(db, world)).flow_edit_draft == draft

    leave = FlowRouterResult(
        action="reply", bubbles=[TextBubble(body="ok")], flow_state=FlowState.MENU
    )
    await tasks._apply_flow_result(_reply(world), leave, WA_ID, tenant=world.tenant, waba_token="t")
    assert (await get_conversation(db, world)).flow_edit_draft is None


def _parked(**kw):
    base = dict(
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft={"appointment_id": "x"},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_edit_floor_drops_a_long_quiet_edit_and_nothing_else():
    quiet = datetime.now(UTC) - timedelta(days=2)
    tenant = SimpleNamespace(initial_flows={})

    stale = _parked()
    assert state_expiry._expire_stale_edit_state(stale, tenant, quiet) is True
    assert stale.flow_state == FlowState.IDLE
    assert stale.flow_step is None
    assert stale.flow_edit_draft is None
    assert stale.flow_managing_appointment_id is None

    recent = _parked()
    assert state_expiry._expire_stale_edit_state(recent, tenant, datetime.now(UTC)) is False
    assert recent.flow_state == FlowState.EDIT_BOOKING

    other = _parked(flow_state=FlowState.SERVICE_CATALOG)
    assert state_expiry._expire_stale_edit_state(other, tenant, quiet) is False


async def test_no_to_quer_continuar_drops_the_edit_draft(db):  # noqa: F811
    from tests._reminders_r3 import consent, turn

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        reactivation_origin=FlowState.EDIT_BOOKING.value,
        flow_edit_draft={"appointment_id": str(world.appointment.id)},
    )
    await turn(db, world, "Não", send=False)
    assert (await get_conversation(db, world)).flow_edit_draft is None
