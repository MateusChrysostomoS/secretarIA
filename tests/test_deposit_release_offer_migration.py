"""Migration b6d2e8f4a1c3 - release offers and their scheduled sends (TASK-052).

Cheapest layers first, like tests/test_deposit_before_migration.py: a single head right after the
`before` timing columns; the offline SQL only creates two tables (no ALTER, no DROP), its
downgrade only drops them; the models declare the same shape.
"""

import contextlib
import io
import pathlib

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from secretaria.core.database import Base

REPO = pathlib.Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "e2b7c4a9d1f5"
REVISION = "b6d2e8f4a1c3"


def _config() -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline(direction: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if direction == "up":
            command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
        else:
            command.downgrade(_config(), f"{REVISION}:{PREVIOUS_HEAD}", sql=True)
    return out.getvalue()


def test_the_revision_is_the_single_head_right_after_the_before_timing():
    script = ScriptDirectory.from_config(_config())
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in script.walk_revisions(head=heads[0])}
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def test_upgrade_only_creates_the_two_new_tables():
    sql = _offline("up").upper()
    assert "CREATE TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "CREATE TABLE DEPOSIT_RELEASE_EMAILS" in sql
    assert "ALTER TABLE" not in sql  # no existing table is touched
    assert "DROP " not in sql
    for name in (
        "UQ_DEPOSIT_RELEASE_OFFERS_RELEASED_APPOINTMENT_ID",
        "UQ_DEPOSIT_RELEASE_EMAILS_OFFER_KIND_SEQ",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_DECIDE_BY",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_EXPIRES",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_KEEP_DEADLINE",
        "IX_DEPOSIT_RELEASE_EMAILS_DUE",
    ):
        assert name in sql


def test_downgrade_drops_only_those_tables():
    sql = _offline("down").upper()
    assert "DROP TABLE DEPOSIT_RELEASE_EMAILS" in sql
    assert "DROP TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "ALTER TABLE" not in sql


def test_the_models_declare_what_the_migration_creates():
    offers = Base.metadata.tables["deposit_release_offers"]
    assert {c.name for c in offers.columns} == {
        "id", "tenant_id", "deposit_id", "released_appointment_id", "patient_id",
        "conversation_id", "deposit_paid", "decision", "refund_status", "refund_source",
        "released_at", "slot_start_at", "decide_by_at", "expires_at", "paid_notice_allowed",
        "first_contact_at", "refund_due_at", "refund_marked_at", "patient_answered_at",
        "last_reminder_at", "after_start_card_at", "keep_requested_at", "keep_deadline_at",
        "keep_outcome", "keep_resolved_at", "keep_resolved_by", "new_appointment_id", "closed_at",
        "carry_check", "carry_checked_at", "deposit_reused_at", "created_at",
    }  # fmt: skip
    fk = {fk.parent.name: fk.ondelete for fk in offers.foreign_keys}
    assert fk["tenant_id"] == "CASCADE" and fk["released_appointment_id"] == "CASCADE"
    assert fk["patient_id"] == "SET NULL" and fk["new_appointment_id"] == "SET NULL"

    mails = Base.metadata.tables["deposit_release_emails"]
    assert {c.name for c in mails.columns} == {
        "id", "tenant_id", "offer_id", "kind", "seq", "due_at", "attempts", "claimed_at",
        "outcome", "created_at",
    }  # fmt: skip
    assert {fk.parent.name: fk.ondelete for fk in mails.foreign_keys}["offer_id"] == "CASCADE"
    assert "has_secretary" not in Base.metadata.tables["tenants"].c  # no manual field
