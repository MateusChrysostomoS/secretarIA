"""TASK-090 Adendo 1 (contract C6): `custom_payment_note` STAYS - the DROP revision is gone.

The owner turned the "Outro" payment text into an optional payment rule for ANY convênio line
(read only by the AI). Nothing was deployed, so the DROP revision `b4d0f6e8a2c5` was deleted:
the chain ends at the last additive TASK-090 revision and both plan tables keep the column
(added by `d4f8a2c6e913`), which both models map again.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from secretaria.models.insurance import ProfessionalInsurancePlan, TenantInsurancePlan

ROOT = Path(__file__).resolve().parents[1]
# C7 (2026-11-24): the "Outro" ON/OFF column revision sits on top of a3c9e5d7f1b4. Junção
# 090+091+093 (2026-11-25): the single head is the empty merge of c7e3a9f1d5b8 (C7), a3c7e1f9b5d2
# (TASK-091) and e7b3d1f9a5c2 (TASK-093 Plano 3).
HEAD = "ac6a0f8102de"
DELETED_DROP = "b4d0f6e8a2c5"
ADDED_BY = "d4f8a2c6e913"


def _scripts() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_single_alembic_head_is_the_last_additive_revision():
    assert _scripts().get_heads() == [HEAD]


def test_the_drop_revision_no_longer_exists():
    scripts = _scripts()
    assert DELETED_DROP not in {rev.revision for rev in scripts.walk_revisions()}
    assert not list((ROOT / "migrations" / "versions").glob(f"{DELETED_DROP}_*.py"))


def test_the_chain_from_base_reaches_the_head_and_adds_the_column_once():
    """Every revision is reachable from the head (no orphan branch) and the only revision that
    names the column is the one that created it (its downgrade drops it - that is all)."""
    scripts = _scripts()
    chain = [rev.revision for rev in scripts.walk_revisions("base", HEAD)]
    assert chain[0] == HEAD and ADDED_BY in chain
    assert len(chain) == len(set(chain)) == len(list(scripts.walk_revisions()))
    namers = [
        path.name
        for path in (ROOT / "migrations" / "versions").glob("*.py")
        if "custom_payment_note" in path.read_text(encoding="utf-8")
    ]
    assert namers == [next(p.name for p in (ROOT / "migrations" / "versions").glob(
        f"{ADDED_BY}_*.py"
    ))]


def test_both_plan_tables_map_the_payment_note_as_nullable_text():
    for model in (TenantInsurancePlan, ProfessionalInsurancePlan):
        column = model.__table__.columns["custom_payment_note"]
        assert column.nullable is True
        assert type(column.type).__name__ == "Text"
