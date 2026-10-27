"""The ingestion job: success, every failure path, idempotency and what it must NOT leave behind."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import asyncio  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.context_source import ContextSource  # noqa: E402
from secretaria.services.context_extraction import (  # noqa: E402
    ContextExtraction,
    DraftedMessages,
    ExtractedFact,
)
from secretaria.services.safe_fetch import FetchedPage, FetchError, UnsafeUrlError  # noqa: E402
from secretaria.workers.shared import context_ingestion as job  # noqa: E402

PAGE = (
    "<html><body><p>Estacionamento gratuito para pacientes na garagem do prédio.</p></body></html>"
)
TEXT = "Estacionamento gratuito para pacientes na garagem do prédio. " * 2
GOOD = ContextExtraction(
    parking=ExtractedFact(
        value="Estacionamento gratuito", evidence="Estacionamento gratuito para pacientes"
    )
)


class _Drafter:
    def __init__(self, result=None) -> None:
        self.result, self.calls = result or DraftedMessages(), 0

    async def ainvoke(self, messages):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def drafter(monkeypatch):
    """No test may reach a real model for the draft step; tests that care replace it."""
    fake = _Drafter()
    monkeypatch.setattr(job, "_draft_model", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(job, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _source(db, *, specialty: str | None = None, **kw) -> ContextSource:
    async with db() as session:
        saved = {
            k: kw.pop(k) for k in ("returning_greeting_message", "post_consult_message") if k in kw
        }
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None, **saved)
        session.add(tenant)
        if specialty:
            session.add(
                Professional(
                    tenant_id=tenant.id, name="Dra. Ana", specialty=specialty, is_active=True
                )
            )
        source = ContextSource(tenant_id=tenant.id, kind=kw.pop("kind", "text"), label="x", **kw)
        session.add(source)
        await session.commit()
        return source


class _Model:
    def __init__(self, result) -> None:
        self.result = result

    async def ainvoke(self, messages):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _reload(db, source) -> ContextSource:
    async with db() as session:
        return await session.get(ContextSource, source.id)


async def test_text_source_becomes_a_verified_proposal_and_the_text_is_erased(db, monkeypatch):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    source = await _source(db, source_text=TEXT)
    await job.ingest_context_source({}, str(source.id))
    done = await _reload(db, source)
    assert done.status == "done" and done.error_code is None
    assert done.source_text is None and done.url is None
    first = done.proposal["fields"][0]
    assert first["key"] == "parking" and first["status"] == "new"


async def test_url_source_is_fetched_through_the_safe_fetcher_only(db, monkeypatch):
    seen = []

    async def fake_fetch(url, **kwargs):
        seen.append(url)
        return FetchedPage(url, "text/html", PAGE)

    monkeypatch.setattr(job, "fetch_public_page", fake_fetch)
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    source = await _source(db, kind="url", url="https://clinica.example.com/contato")
    await job.ingest_context_source({}, str(source.id))
    assert seen == ["https://clinica.example.com/contato"]
    assert (await _reload(db, source)).status == "done"


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (UnsafeUrlError("private_address"), "private_address"),
        (FetchError("timeout"), "timeout"),
        (FetchError("http_404"), "http_404"),
    ],
)
async def test_fetch_failures_become_stable_codes_and_still_erase_the_input(
    db, monkeypatch, raised, code
):
    async def boom(url, **kwargs):
        raise raised

    monkeypatch.setattr(job, "fetch_public_page", boom)
    source = await _source(db, kind="url", url="https://x.example.com/")
    await job.ingest_context_source({}, str(source.id))
    failed = await _reload(db, source)
    assert failed.status == "failed" and failed.error_code == code and failed.url is None


async def test_an_unexpected_crash_is_contained_and_the_text_is_still_erased(db, monkeypatch):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(ZeroDivisionError("boom")))
    source = await _source(db, source_text=TEXT)
    # Must not raise: a retried job would call (and bill) the model a second time.
    await job.ingest_context_source({}, str(source.id))
    failed = await _reload(db, source)
    assert failed.status == "failed" and failed.error_code == "model_error"
    assert failed.source_text is None


async def test_text_too_short_to_analyse_fails_with_no_text(db, monkeypatch):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    source = await _source(db, source_text="oi")
    await job.ingest_context_source({}, str(source.id))
    assert (await _reload(db, source)).error_code == "no_text"


async def test_only_pending_sources_are_processed(db, monkeypatch):
    called = []
    monkeypatch.setattr(job, "_extraction_model", lambda: called.append(1) or _Model(GOOD))
    source = await _source(db, source_text=TEXT, status="done")
    await job.ingest_context_source({}, str(source.id))
    await job.ingest_context_source({}, str(uuid4()))  # unknown id: a no-op
    assert called == []


async def test_logs_never_carry_the_text_or_the_full_url(db, monkeypatch):
    events = []

    class Recorder:
        def info(self, event, **kwargs):
            events.append((event, kwargs))

        warning = error = info

    monkeypatch.setattr(job, "logger", Recorder())

    async def fake_fetch(url, **kwargs):
        return FetchedPage(url, "text/html", PAGE)

    monkeypatch.setattr(job, "fetch_public_page", fake_fetch)
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    source = await _source(db, kind="url", url="https://clinica.example.com/segredo?token=abc123")
    await job.ingest_context_source({}, str(source.id))
    flat = repr(events)
    assert "abc123" not in flat and "segredo" not in flat
    assert "Estacionamento gratuito" not in flat
    assert "clinica.example.com" in flat


async def test_a_message_the_material_does_not_cover_is_drafted_for_the_doctor_to_review(
    db, monkeypatch, drafter
):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    care = "Cuide-se e siga a orientação do médico que atendeu você."
    drafter.result = DraftedMessages(post_consult_knowledge=care)
    source = await _source(db, source_text=TEXT, specialty="Oftalmologia")
    await job.ingest_context_source({}, str(source.id))
    done = await _reload(db, source)
    by_key = {f["key"]: f for f in done.proposal["fields"]}
    assert by_key["parking"]["origin"] == "source"
    assert by_key["post_consult_knowledge"]["origin"] == "draft"
    assert by_key["post_consult_knowledge"]["evidence"] == ""
    assert drafter.calls == 1


async def test_a_failing_draft_step_never_fails_the_analysis(db, monkeypatch, drafter):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    drafter.result = RuntimeError("model down")
    source = await _source(db, source_text=TEXT, specialty="Oftalmologia")
    await job.ingest_context_source({}, str(source.id))
    done = await _reload(db, source)
    assert done.status == "done" and done.source_text is None
    assert [f["key"] for f in done.proposal["fields"]] == ["parking"]


async def test_nothing_is_drafted_over_a_message_the_clinic_already_wrote(
    db, monkeypatch, drafter
):
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(GOOD))
    drafter.result = DraftedMessages(
        returning_greeting_message="Que bom ver você de volta!",
        post_consult_message="Obrigado pela visita.",
    )
    source = await _source(
        db,
        source_text=TEXT,
        returning_greeting_message="Texto da clínica",
        post_consult_message="Outro texto da clínica",
    )
    await job.ingest_context_source({}, str(source.id))
    keys = {f["key"] for f in (await _reload(db, source)).proposal["fields"]}
    assert "returning_greeting_message" not in keys and "post_consult_message" not in keys


async def test_a_cancelled_job_still_closes_the_row_and_erases_the_input(db, monkeypatch):
    # arq cancels a job at its timeout and on worker shutdown with CancelledError, which is NOT an
    # Exception: without a handler the row stays "processing" with the text kept (SPEC G3).
    class _Slow:
        async def ainvoke(self, messages):
            await asyncio.sleep(10)

    monkeypatch.setattr(job, "_extraction_model", lambda: _Slow())
    source = await _source(db, source_text=TEXT)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(job.ingest_context_source({}, str(source.id)), 0.3)
    closed = await _reload(db, source)
    assert closed.status == "failed" and closed.error_code == "model_error"
    assert closed.source_text is None and closed.url is None


async def test_a_retry_of_an_interrupted_job_closes_the_row_without_calling_the_model(
    db, monkeypatch
):
    called = []
    monkeypatch.setattr(job, "_extraction_model", lambda: called.append(1) or _Model(GOOD))
    source = await _source(db, source_text=TEXT, status="processing")
    await job.ingest_context_source({"job_try": 1}, str(source.id))  # first try: may be alive
    assert (await _reload(db, source)).status == "processing"
    await job.ingest_context_source({"job_try": 2}, str(source.id))  # arq re-delivered it
    closed = await _reload(db, source)
    assert closed.status == "failed" and closed.error_code == "model_error"
    assert closed.source_text is None
    assert called == []


async def test_a_source_deleted_before_the_job_starts_never_reaches_the_model(db, monkeypatch):
    called = []
    monkeypatch.setattr(job, "_extraction_model", lambda: called.append(1) or _Model(GOOD))
    source = await _source(db, source_text=TEXT, deleted_at=datetime.now(UTC))
    await job.ingest_context_source({}, str(source.id))
    row = await _reload(db, source)
    assert called == [] and row.source_text is None
    assert row.status == "failed"  # terminal: it no longer counts as in flight


async def test_a_source_deleted_while_running_stores_no_proposal(db, monkeypatch):
    class _DeletesMidway:
        async def ainvoke(self, messages):
            async with db() as session:
                row = (await session.execute(select(ContextSource))).scalar_one()
                row.deleted_at = datetime.now(UTC)
                await session.commit()
            return GOOD

    monkeypatch.setattr(job, "_extraction_model", lambda: _DeletesMidway())
    source = await _source(db, source_text=TEXT)
    await job.ingest_context_source({}, str(source.id))
    row = await _reload(db, source)
    assert row.proposal is None and row.source_text is None


async def test_the_draft_step_is_skipped_when_the_material_covered_every_message(
    db, monkeypatch, drafter
):
    covered = ContextExtraction(
        returning_greeting_message=ExtractedFact(value="a", evidence="Estacionamento gratuito"),
        post_consult_message=ExtractedFact(value="b", evidence="Estacionamento gratuito"),
        post_consult_knowledge=ExtractedFact(value="c", evidence="Estacionamento gratuito"),
    )
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(covered))
    source = await _source(db, source_text=TEXT)
    await job.ingest_context_source({}, str(source.id))
    assert drafter.calls == 0


CATALOG_TEXT = (
    "Dr. Carlos Mendes (Cardiologista) atende às segundas e quartas, das 08:00 às 18:00. "
    "Eletrocardiograma (20 min) - R$ 150,00 (Dr. Carlos). "
    "Aceitamos os convênios Bradesco Saúde e Amil."
)


async def test_catalog_items_reach_the_proposal_and_nothing_is_written(db, monkeypatch):
    from secretaria.models import Service, TenantInsurancePlan
    from secretaria.services.context_catalog_proposal import (
        ExtractedHours,
        ExtractedInsurancePlan,
        ExtractedProfessional,
        ExtractedService,
    )
    from secretaria.services.insurance_catalog import ensure_catalog_seeded

    extraction = ContextExtraction(
        professionals=[
            ExtractedProfessional(
                name="Dr. Carlos Mendes",
                specialty="Cardiologista",
                hours=[ExtractedHours(days=["monday", "wednesday"], start="08:00", end="18:00")],
                evidence=(
                    "Dr. Carlos Mendes (Cardiologista) atende às segundas e quartas, "
                    "das 08:00 às 18:00"
                ),
            )
        ],
        services=[
            ExtractedService(
                name="Eletrocardiograma",
                duration_min=20,
                price="R$ 150,00",
                professional_names=["Dr. Carlos"],
                evidence="Eletrocardiograma (20 min) - R$ 150,00 (Dr. Carlos)",
            )
        ],
        insurance_plans=[
            ExtractedInsurancePlan(
                name="Bradesco Saúde", evidence="Aceitamos os convênios Bradesco Saúde e Amil"
            ),
            ExtractedInsurancePlan(
                name="Amil", evidence="Aceitamos os convênios Bradesco Saúde e Amil"
            ),
        ],
    )
    monkeypatch.setattr(job, "_extraction_model", lambda: _Model(extraction))
    async with db() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    source = await _source(db, source_text=CATALOG_TEXT)

    await job.ingest_context_source({}, str(source.id))

    done = await _reload(db, source)
    assert done.status == "done"
    proposal = done.proposal
    assert [p["name"] for p in proposal["professionals"]] == ["Dr. Carlos Mendes"]
    assert proposal["professionals"][0]["matched_id"] is None
    assert proposal["professionals"][0]["hours"]["monday"] == [{"start": "08:00", "end": "18:00"}]
    assert [s["name"] for s in proposal["catalog_services"]] == ["Eletrocardiograma"]
    insurance = proposal["insurance"]
    assert insurance["default_mode"] == "shared" and insurance["applicable"] is True
    assert sorted(p["name"] for p in insurance["plans"]) == ["Amil", "Bradesco Saúde"]
    assert all(p["catalog_id"] for p in insurance["plans"])
    assert any(n["kind"] == "decision" for n in proposal["notes"])
    # The job proposes. It never registers a doctor, a service or a plan by itself.
    async with db() as session:
        assert (await session.scalars(select(Service))).first() is None
        assert (await session.scalars(select(TenantInsurancePlan))).first() is None
        assert len(list(await session.scalars(select(Professional)))) == 0
