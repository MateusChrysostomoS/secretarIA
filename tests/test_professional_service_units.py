# ruff: noqa: F811
"""Which services a professional does at which unit (TASK-083 follow-up T1): domain layer.

No row for (professional, service) = every unit of the doctor; rows = only those units. The
table never stores "none". `apply_marks` validates everything before writing anything.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import event  # noqa: E402

from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalServiceUnit,
    ProfessionalUnit,
    Service,
    Unit,
)
from secretaria.services.professional_service_units import (  # noqa: E402
    ServiceMarkError,
    allowed_unit_ids,
    apply_marks,
    read_marks,
    read_marks_for,
)
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401


def _service(tenant_id, name: str) -> Service:
    return Service(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        normalized_name=name.casefold(),
        is_active=True,
    )


@pytest_asyncio.fixture
async def world(db):
    tenant = await seed_tenant(db, name="Clínica A")
    other = await seed_tenant(db, name="Clínica B")
    async with db() as session:
        limpeza = _service(tenant.id, "Limpeza")
        clareamento = _service(tenant.id, "Clareamento")
        extracao = _service(tenant.id, "Extração")  # in the catalog, NOT offered by the doctor
        foreign_service = _service(other.id, "Limpeza")
        session.add_all([limpeza, clareamento, extracao, foreign_service])
        await session.flush()
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[
                {"name": "Limpeza", "service_id": str(limpeza.id), "is_active": True},
                {"name": "Clareamento", "service_id": str(clareamento.id), "is_active": True},
                {"name": "Consulta", "is_active": True},  # no service_id: never restricted
            ],
        )
        colleague = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. Beto",
            is_active=True,
            appointment_types=[
                {"name": "Limpeza", "service_id": str(limpeza.id), "is_active": True},
                {"name": "Clareamento", "service_id": str(clareamento.id), "is_active": True},
            ],
        )
        units = {
            name: Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=f"Rua {name}")
            for name in ("Centro", "Norte", "Sul")
        }
        foreign_unit = Unit(id=uuid4(), tenant_id=other.id, name="Alheia", address="Rua Alheia")
        session.add_all([doctor, colleague, foreign_unit, *units.values()])
        await session.commit()
    return SimpleNamespace(
        tenant=tenant,
        doctor=doctor,
        colleague=colleague,
        limpeza=limpeza,
        clareamento=clareamento,
        extracao=extracao,
        foreign_service=foreign_service,
        centro=units["Centro"],
        norte=units["Norte"],
        sul=units["Sul"],
        foreign_unit=foreign_unit,
    )


async def _store(db, professional, marks: dict) -> None:
    async with db() as session:
        for service_id, unit_ids in marks.items():
            for unit_id in unit_ids:
                session.add(
                    ProfessionalServiceUnit(
                        professional_id=professional.id, service_id=service_id, unit_id=unit_id
                    )
                )
        await session.commit()


async def _stored(db, professional) -> dict:
    async with db() as session:
        return await read_marks(session, professional.id)


def test_allowed_unit_ids_none_means_all():
    a, b = uuid4(), uuid4()
    marks = {a: frozenset({uuid4()})}
    assert allowed_unit_ids(marks, b) is None  # no rows for this service
    assert allowed_unit_ids(marks, None) is None  # entry without service_id
    assert allowed_unit_ids({}, a) is None


def test_allowed_unit_ids_restricted():
    service, u1, u2 = uuid4(), uuid4(), uuid4()
    marks = {service: frozenset({u1, u2})}
    assert allowed_unit_ids(marks, service) == frozenset({u1, u2})


@pytest.mark.asyncio
async def test_apply_marks_persists_subset(db, world):
    w = world
    linked = [w.centro.id, w.norte.id, w.sul.id]
    async with db() as session:
        await apply_marks(
            session,
            w.tenant.id,
            w.doctor,
            linked,
            {w.limpeza.id: [w.centro.id, w.norte.id], w.clareamento.id: [w.sul.id]},
        )
        await session.commit()
    assert await _stored(db, w.doctor) == {
        w.limpeza.id: frozenset({w.centro.id, w.norte.id}),
        w.clareamento.id: frozenset({w.sul.id}),
    }

    # Sending the full desired state again replaces it; a service left out goes back to "all".
    async with db() as session:
        await apply_marks(session, w.tenant.id, w.doctor, linked, {w.limpeza.id: [w.sul.id]})
        await session.commit()
    assert await _stored(db, w.doctor) == {w.limpeza.id: frozenset({w.sul.id})}

    # An empty mapping clears every restriction (everything is "all" again).
    async with db() as session:
        await apply_marks(session, w.tenant.id, w.doctor, linked, {})
        await session.commit()
    assert await _stored(db, w.doctor) == {}


@pytest.mark.asyncio
async def test_apply_marks_none_keeps_stored_and_drops_unlinked_unit(db, world):
    w = world
    await _store(db, w.doctor, {w.limpeza.id: [w.centro.id, w.norte.id]})
    async with db() as session:
        # Norte is no longer linked; the stored mark on it goes, the one on Centro stays.
        await apply_marks(session, w.tenant.id, w.doctor, [w.centro.id, w.sul.id], None)
        await session.commit()
    assert await _stored(db, w.doctor) == {w.limpeza.id: frozenset({w.centro.id})}

    # Linking a NEW unit never changes a restricted service.
    async with db() as session:
        await apply_marks(session, w.tenant.id, w.doctor, [w.centro.id, w.sul.id, w.norte.id], None)
        await session.commit()
    assert await _stored(db, w.doctor) == {w.limpeza.id: frozenset({w.centro.id})}


@pytest.mark.asyncio
async def test_apply_marks_refuses_service_left_without_unit(db, world):
    w = world
    await _store(db, w.doctor, {w.limpeza.id: [w.centro.id], w.clareamento.id: [w.norte.id]})
    async with db() as session:
        with pytest.raises(ServiceMarkError) as exc:
            # Centro (the ONLY unit of Limpeza) is unlinked; Norte stays.
            await apply_marks(session, w.tenant.id, w.doctor, [w.norte.id, w.sul.id], None)
        await session.rollback()
    assert exc.value.code == "service_without_unit"
    assert exc.value.service_ids == [w.limpeza.id]
    # Nothing changed.
    assert await _stored(db, w.doctor) == {
        w.limpeza.id: frozenset({w.centro.id}),
        w.clareamento.id: frozenset({w.norte.id}),
    }


@pytest.mark.asyncio
async def test_apply_marks_refuses_empty_unit_set(db, world):
    w = world
    async with db() as session:
        with pytest.raises(ServiceMarkError) as exc:
            await apply_marks(session, w.tenant.id, w.doctor, [w.centro.id], {w.limpeza.id: []})
    assert exc.value.code == "service_without_unit"
    assert exc.value.service_ids == [w.limpeza.id]
    assert await _stored(db, w.doctor) == {}


@pytest.mark.asyncio
async def test_apply_marks_refuses_service_not_offered(db, world):
    w = world
    linked = [w.centro.id]
    for sid in (w.extracao.id, w.foreign_service.id, uuid4()):
        async with db() as session:
            with pytest.raises(ServiceMarkError) as exc:
                await apply_marks(session, w.tenant.id, w.doctor, linked, {sid: [w.centro.id]})
        assert exc.value.code == "service_not_offered"
        assert exc.value.service_ids == [sid]
    assert await _stored(db, w.doctor) == {}

    # An entry the doctor switched off is not offered either.
    async with db() as session:
        doctor = await session.get(Professional, w.doctor.id)
        doctor.appointment_types = [
            {"name": "Limpeza", "service_id": str(w.limpeza.id), "is_active": False}
        ]
        await session.commit()
        with pytest.raises(ServiceMarkError) as exc:
            await apply_marks(session, w.tenant.id, doctor, linked, {w.limpeza.id: [w.centro.id]})
    assert exc.value.code == "service_not_offered"


@pytest.mark.asyncio
async def test_apply_marks_refuses_other_tenant_unit(db, world):
    w = world
    async with db() as session:
        with pytest.raises(ServiceMarkError) as exc:
            await apply_marks(
                session, w.tenant.id, w.doctor, [w.centro.id], {w.limpeza.id: [w.foreign_unit.id]}
            )
    assert exc.value.code == "unit_not_linked"
    assert exc.value.service_ids == [w.limpeza.id]

    # A unit of the clinic that is simply not linked to the doctor is refused the same way.
    async with db() as session:
        with pytest.raises(ServiceMarkError) as exc:
            await apply_marks(
                session, w.tenant.id, w.doctor, [w.centro.id], {w.limpeza.id: [w.norte.id]}
            )
    assert exc.value.code == "unit_not_linked"
    assert await _stored(db, w.doctor) == {}


@pytest.mark.asyncio
async def test_apply_marks_does_not_touch_another_professional(db, world):
    w = world
    await _store(db, w.colleague, {w.limpeza.id: [w.centro.id]})
    async with db() as session:
        await apply_marks(
            session, w.tenant.id, w.doctor, [w.centro.id], {w.limpeza.id: [w.centro.id]}
        )
        await session.commit()
    assert await _stored(db, w.colleague) == {w.limpeza.id: frozenset({w.centro.id})}
    assert await _stored(db, w.doctor) == {w.limpeza.id: frozenset({w.centro.id})}


@pytest.mark.asyncio
async def test_apply_marks_leaves_unit_links_alone(db, world):
    w = world
    async with db() as session:
        session.add(ProfessionalUnit(professional_id=w.doctor.id, unit_id=w.centro.id))
        await session.commit()
    await _store(db, w.doctor, {w.limpeza.id: [w.centro.id]})
    async with db() as session:
        await apply_marks(session, w.tenant.id, w.doctor, [w.centro.id], None)
        await session.commit()
        assert await session.get(ProfessionalUnit, (w.doctor.id, w.centro.id)) is not None


@pytest.mark.asyncio
async def test_read_marks_for_is_one_query(db, world):
    w = world
    await _store(db, w.doctor, {w.limpeza.id: [w.centro.id, w.norte.id]})
    await _store(db, w.colleague, {w.clareamento.id: [w.sul.id]})
    ghost = uuid4()
    async with db() as session:
        statements: list[str] = []

        def _count(conn, cursor, statement, *_):
            statements.append(statement)

        engine = session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            result = await read_marks_for(session, [w.doctor.id, w.colleague.id, ghost])
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert len(statements) == 1
        assert await read_marks_for(session, []) == {}
    assert result == {
        w.doctor.id: {w.limpeza.id: frozenset({w.centro.id, w.norte.id})},
        w.colleague.id: {w.clareamento.id: frozenset({w.sul.id})},
        ghost: {},
    }


def test_migration_is_the_single_head_and_upgrade_downgrade_round_trips(tmp_path):
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1  # later revisions may sit on top, never a second head
    assert "c6e1a9f4d2b7" in {rev.revision for rev in scripts.walk_revisions()}

    path = root / "migrations" / "versions" / "c6e1a9f4d2b7_professional_service_units.py"
    spec = importlib.util.spec_from_file_location("psu_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "b5d9e3f7a1c4"

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'psu.db'}")
    with engine.begin() as conn:
        for table in ("professionals", "services", "units"):
            conn.execute(sa.text(f"CREATE TABLE {table} (id CHAR(32) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            inspector = sa.inspect(conn)
            assert "professional_service_units" in inspector.get_table_names()
            columns = {c["name"] for c in inspector.get_columns("professional_service_units")}
            assert columns == {"professional_id", "service_id", "unit_id", "created_at"}
            pk = inspector.get_pk_constraint("professional_service_units")
            assert set(pk["constrained_columns"]) == {"professional_id", "service_id", "unit_id"}
            assert [
                i["column_names"] for i in inspector.get_indexes("professional_service_units")
            ] == [["service_id"]]
            migration.downgrade()
            assert "professional_service_units" not in sa.inspect(conn).get_table_names()
