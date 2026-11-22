"""TASK-090 B4: the Sede backfill and the paid-notice reset are data-only and idempotent."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVISION = "f2b8d4c6e0a3"
DOWN_REVISION = "e1a7c3b9d5f2"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_backfill_sede_and_reset_paid_notices.py"
    spec = importlib.util.spec_from_file_location("backfill_sede_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _migration()


def test_single_head_is_this_revision():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in scripts.walk_revisions(base="base", head=heads[0])}
    assert MIGRATION.down_revision == DOWN_REVISION


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Av. Paulista, 1000", ("Av. Paulista", "1000")),
        ("Rua A 45", ("Rua A", "45")),
        ("Rua sem numero", ("Rua sem numero", None)),
        (None, (None, None)),
        ("", (None, None)),
        ("   ", (None, None)),
        ("  Rua  das Flores ,  12B ", ("Rua das Flores", "12B")),
        ("Rua A, s/n", ("Rua A", "s/n")),
        ("Rua 7 de Setembro", ("Rua 7 de Setembro", None)),
        ("Rua 15", ("Rua 15", None)),
        ("Rua A, 45, apto 2", ("Rua A, 45, apto 2", None)),
        ("Rua B 10, apto 2", ("Rua B 10, apto 2", None)),
        ("Rua B 10, sala 3", ("Rua B 10, sala 3", None)),
        ("Quadra 10 Lote 5", ("Quadra 10 Lote 5", None)),
        ("Rua B Bloco 2", ("Rua B Bloco 2", None)),
        ("Av. Brasil 100 Apto. 7", ("Av. Brasil 100 Apto. 7", None)),
    ],
)
def test_split_line(line, expected):
    assert MIGRATION.split_line(line) == expected


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'sede.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE tenants (id CHAR(32) PRIMARY KEY, address JSON, "
                "paid_notices_auto_approved BOOLEAN)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE units (id CHAR(32) PRIMARY KEY, tenant_id CHAR(32) NOT NULL, "
                "name VARCHAR(255) NOT NULL, address VARCHAR(512), cep VARCHAR(8), "
                "street VARCHAR(255), number VARCHAR(30), complement VARCHAR(255), "
                "neighborhood VARCHAR(255), city VARCHAR(255), state VARCHAR(2), "
                "is_active BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    return engine


def _tenant(conn, address=None, paid=None, *, with_unit=False) -> str:
    tid = uuid.uuid4().hex
    conn.execute(
        sa.text("INSERT INTO tenants VALUES (:id, :address, :paid)"),
        {"id": tid, "address": json.dumps(address) if address is not None else None, "paid": paid},
    )
    if with_unit:
        conn.execute(
            sa.text(
                "INSERT INTO units (id, tenant_id, name, address, is_active) "
                "VALUES (:id, :t, 'Centro', 'Rua X, 9', 0)"
            ),
            {"id": uuid.uuid4().hex, "t": tid},
        )
    return tid


def _units(conn, tid):
    return [
        tuple(row)
        for row in conn.execute(
            sa.text(
                "SELECT name, is_active, address, cep, street, number, complement, "
                "neighborhood, city, state FROM units WHERE tenant_id = :t ORDER BY name"
            ),
            {"t": tid},
        )
    ]


def _run(conn, fn):
    with Operations.context(MigrationContext.configure(conn)):
        fn()


FULL = {
    "line": "Av. Paulista, 1000",
    "complement": "Sala 3",
    "neighborhood": "Bela Vista",
    "city": "São Paulo",
    "state": "SP",
    "postal_code": "01310-100",
}


def test_creates_one_sede_from_the_legacy_address(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(conn, FULL)
        _run(conn, MIGRATION.upgrade)
        assert _units(conn, tid) == [
            (
                "Sede",
                1,
                "Av. Paulista, 1000 - Sala 3, Bela Vista, São Paulo - SP, 01310-100",
                "01310100",
                "Av. Paulista",
                "1000",
                "Sala 3",
                "Bela Vista",
                "São Paulo",
                "SP",
            )
        ]


def test_incomplete_address_still_creates_the_sede_with_what_exists(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        no_number = _tenant(conn, {"line": "Rua sem numero", "city": " Recife ", "state": "pe"})
        bad_cep = _tenant(conn, {"line": "Rua B, 5", "postal_code": "123", "state": "Bahia"})
        city_only = _tenant(conn, {"city": "Curitiba", "state": "ZZ", "postal_code": ""})
        _run(conn, MIGRATION.upgrade)
        assert _units(conn, no_number) == [
            (
                "Sede",
                1,
                "Rua sem numero, Recife - PE",
                None,
                "Rua sem numero",
                None,
                None,
                None,
                "Recife",
                "PE",
            )
        ]
        assert _units(conn, bad_cep) == [
            ("Sede", 1, "Rua B, 5, BA", None, "Rua B", "5", None, None, None, "BA")
        ]
        assert _units(conn, city_only) == [
            ("Sede", 1, "Curitiba", None, None, None, None, None, "Curitiba", None)
        ]


def test_oversized_legacy_values_are_capped_to_the_unit_columns(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(
            conn,
            {
                "line": "R" * 400 + ", 12",
                "complement": "c" * 400,
                "neighborhood": "b" * 400,
                "city": "x" * 400,
                "state": "SP",
                "postal_code": "01310-100",
            },
        )
        # No trailing number: the WHOLE line is the street, and must still be cut to 255.
        whole = _tenant(conn, {"line": "S" * 600, "city": "Recife"})
        _run(conn, MIGRATION.upgrade)
        for owner in (tid, whole):
            ((_name, _active, address, _cep, street, number, complement, hood, city, _uf),) = (
                _units(conn, owner)
            )
            assert len(address) <= 512
            assert len(street) <= 255 and len(complement or "") <= 255
            assert len(hood or "") <= 255 and len(city) <= 255 and len(number or "") <= 30
        ((_n, _a, _addr, _c, street, number, complement, hood, city, _u),) = _units(conn, tid)
        assert (street, number) == ("R" * 255, "12")
        assert (complement, hood, city) == ("c" * 255, "b" * 255, "x" * 255)


def test_tenant_with_a_unit_is_untouched_even_if_inactive(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(conn, FULL, with_unit=True)
        _run(conn, MIGRATION.upgrade)
        assert [row[0] for row in _units(conn, tid)] == ["Centro"]


@pytest.mark.parametrize(
    "address",
    [None, {}, {"complement": "x", "neighborhood": "y"}, {"line": "  ", "city": ""}, "text"],
)
def test_tenant_without_a_usable_address_gets_no_unit(tmp_path, address):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(conn, address)
        _run(conn, MIGRATION.upgrade)
        assert _units(conn, tid) == []


def test_running_twice_or_downgrade_then_upgrade_keeps_one_sede(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(conn, FULL, paid=True)
        _run(conn, MIGRATION.upgrade)
        _run(conn, MIGRATION.upgrade)
        _run(conn, MIGRATION.downgrade)
        _run(conn, MIGRATION.upgrade)
        assert [row[0] for row in _units(conn, tid)] == ["Sede"]


def test_paid_notices_reset_only_the_true_ones(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        on = _tenant(conn, paid=True)
        off = _tenant(conn, paid=False)
        unset = _tenant(conn, paid=None)
        _run(conn, MIGRATION.upgrade)
        flags = dict(
            conn.execute(sa.text("SELECT id, paid_notices_auto_approved FROM tenants")).all()
        )
        assert flags[on] == 0
        assert flags[off] == 0
        assert flags[unset] is None


def test_downgrade_is_a_no_op(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        tid = _tenant(conn, FULL, paid=True)
        _run(conn, MIGRATION.upgrade)
        _run(conn, MIGRATION.downgrade)
        assert len(_units(conn, tid)) == 1
