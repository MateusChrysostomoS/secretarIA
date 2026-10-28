"""Merge main's booking-notice column into the out-of-MVP history; no schema or data operation.

`c3d9e1f5a7b2` (`professionals.min_booking_notice_hours`, shipped on main from `b7e2c4f9a1d6`)
and the feature head `c5a9e2d7f4b1` both descend from `b7e2c4f9a1d6`. Both parents are kept
unchanged so a database on either history upgrades to this single head. Deploy order stays:
migration -> `secretaria_api` + `secretaria-worker` together -> Brain-Message front.

Rollback: restore the database backup. `alembic downgrade` of a merge only reaches a branch tip.
"""

revision = "e8a3c6f1b4d9"
down_revision = ("c5a9e2d7f4b1", "c3d9e1f5a7b2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
