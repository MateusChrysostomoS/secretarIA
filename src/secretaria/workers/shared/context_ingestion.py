"""arq job: read the source, extract a verified proposal, store it, erase the input (TASK-026)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Professional, Tenant
from secretaria.models.context_source import (
    KIND_URL,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    ContextSource,
)
from secretaria.services.context_extraction import (
    DRAFT_KEYS,
    ContextExtraction,
    DraftedMessages,
    ExtractionFailed,
    build_proposal,
    draft_missing_messages,
    extract_context,
)
from secretaria.services.greeting_template import clinic_description_budget
from secretaria.services.insurance_catalog import list_catalog, list_tenant_plans
from secretaria.services.safe_fetch import FetchError, UnsafeUrlError, fetch_public_page
from secretaria.services.service_catalog import load_service_catalog
from secretaria.services.tenant_config import list_active_professionals
from secretaria.services.text_extract import (
    MIN_USEFUL_CHARS,
    ExtractionError,
    clean_text,
    html_to_text,
)

logger = get_logger(__name__)
_models: dict[str, object] = {}


def _structured_model(name: str, schema):
    """A structured-output model, built lazily so importing this module needs no API key."""
    if name not in _models:
        from secretaria.ai.chat_model import build_structured_model

        try:
            # Claude, or OpenAI without a Claude key; a missing key is a model failure for the job.
            _models[name] = build_structured_model(
                schema,
                openai_max_tokens=get_settings().OPENAI_CONTEXT_EXTRACTION_MAX_TOKENS,
                max_retries=2,
                timeout=90,
                # Too big for Claude's constrained output ("Schema is too complex").
                native_schema=False,
            )
        except RuntimeError as exc:
            raise ExtractionFailed("model_error") from exc
    return _models[name]


def _extraction_model():
    return _structured_model("extraction", ContextExtraction)


def _draft_model():
    return _structured_model("draft", DraftedMessages)


async def _finish(source_id: UUID, *, status: str, error_code: str | None, proposal) -> None:
    async with async_session_factory() as session:
        source = await session.get(ContextSource, source_id)
        if source is None:
            return
        # A source the manager deleted meanwhile keeps nothing: its job ends, its slot frees.
        source.status, source.error_code = status, error_code
        source.proposal = None if source.deleted_at is not None else proposal
        source.source_text = None  # SPEC G3: the material never outlives the job
        source.url = None
        await session.commit()


async def _claim(sid: UUID, ctx: dict) -> bool:
    """Take the source for THIS run. False = nothing to do (and, where it must, the row is closed).

    A source is only ever processed once: a re-delivery of a job whose first try died mid-flight
    (arq cancels at its timeout and on shutdown, then retries) closes the row instead of calling
    - and billing - the model a second time. A source deleted before it started never reaches it.
    """
    async with async_session_factory() as session:
        source = await session.get(ContextSource, sid)
        if source is None:
            logger.info("context_source_skipped", source_id=str(sid))
            return False
        interrupted = source.status == STATUS_PROCESSING and ctx.get("job_try", 1) > 1
        if source.status != STATUS_PENDING and not interrupted:
            logger.info("context_source_skipped", source_id=str(sid))
            return False
        if interrupted or source.deleted_at is not None:
            source.status, source.error_code = STATUS_FAILED, "model_error"
            source.source_text = source.url = None
            await session.commit()
            logger.warning(
                "context_source_closed_unrun", source_id=str(sid), interrupted=interrupted
            )
            return False
        source.status = STATUS_PROCESSING
        await session.commit()
        return True


async def _load_inputs(sid: UUID) -> dict:
    """Everything the run needs from the database, read once (the session is closed after)."""
    async with async_session_factory() as session:
        source = await session.get(ContextSource, sid)
        tenant = await session.get(Tenant, source.tenant_id)
        catalog = await load_service_catalog(session, source.tenant_id)
        professionals = await list_active_professionals(session, source.tenant_id)
        # Matching must also see a doctor who is switched off: registering "Dr. Carlos" again
        # next to his inactive row would be a duplicate the clinic cannot tell apart.
        roster = list(
            await session.scalars(
                select(Professional).where(Professional.tenant_id == source.tenant_id)
            )
        )
        insurance_catalog = await list_catalog(session)
        tenant_plans = await list_tenant_plans(session, source.tenant_id)
        return {
            "kind": source.kind,
            "url": source.url,
            "text": source.source_text,
            "services": [
                SimpleNamespace(
                    name=s.name, is_active=s.is_active, requirements=list(s.requirements or [])
                )
                for s in catalog
            ],
            "specialties": sorted(
                {(p.specialty or "").strip() for p in professionals if (p.specialty or "").strip()}
            ),
            "catalog_inputs": {
                "roster": [
                    SimpleNamespace(
                        id=p.id,
                        name=p.name,
                        specialty=p.specialty,
                        about=p.about,
                        business_hours=p.business_hours,
                    )
                    for p in roster
                ],
                "catalog": [SimpleNamespace(id=s.id, name=s.name) for s in catalog],
                "insurance_catalog": [
                    SimpleNamespace(id=e.id, name=e.name, aliases=list(e.aliases or []))
                    for e in insurance_catalog
                ],
                "clinic_plan_names": [
                    (entry.name if entry is not None else str(plan.custom_name or ""))
                    for plan, entry in tenant_plans
                ],
                "insurance_mode": tenant.insurance_mode,
                "default_duration": tenant.appointment_duration_min or 50,
            },
            "clinic_name": tenant.clinic_name,
            "facts": tenant.clinic_facts,
            "address": tenant.address,
            "description": tenant.clinic_description,
            "budget": clinic_description_budget(tenant.clinic_name),  # THIS clinic's greeting slot
            "saved_messages": {
                "returning_greeting_message": tenant.returning_greeting_message,
                "post_consult_message": tenant.post_consult_message,
                "post_consult_knowledge": tenant.post_consult_knowledge,
            },
        }


async def ingest_context_source(ctx: dict, source_id: str) -> None:
    sid = UUID(source_id)
    if not await _claim(sid, ctx):
        return
    try:
        inp = await _load_inputs(sid)
        kind, url, text = inp["kind"], inp["url"], inp["text"]
        services, specialties = inp["services"], inp["specialties"]
        clinic_name, facts, address = inp["clinic_name"], inp["facts"], inp["address"]
        description, budget = inp["description"], inp["budget"]
        saved_messages = inp["saved_messages"]
        host = urlsplit(url).hostname if url else None
        logger.info("context_source_started", source_id=source_id, kind=kind, host=host)
        if kind == KIND_URL:
            page = await fetch_public_page(url)
            is_plain = page.content_type == "text/plain"
            text = clean_text(page.text) if is_plain else html_to_text(page.text)
        if not text or len(text.strip()) < MIN_USEFUL_CHARS:
            raise ExtractionError("no_text")
        names = [s.name for s in services if s.is_active]
        extraction = await extract_context(text, names, model=_extraction_model())
        # Messages the clinic left empty and the material did not cover: the model may DRAFT
        # them (owner's decision 2026-10-02). A failure here never fails the analysis.
        wanted = [
            k
            for k in DRAFT_KEYS
            if getattr(extraction, k) is None and not (saved_messages.get(k) or "").strip()
        ]
        drafts: dict[str, str] = {}
        if wanted:
            try:
                drafts = await draft_missing_messages(
                    model=_draft_model(),
                    clinic_name=clinic_name,
                    specialties=specialties,
                    service_names=names,
                    source_text=text,
                    wanted=wanted,
                )
            except Exception as exc:  # noqa: BLE001 - drafts are optional
                logger.warning(
                    "context_source_drafts_failed",
                    source_id=source_id,
                    error_type=type(exc).__name__,
                )
        proposal = build_proposal(
            extraction,
            current_facts=facts,
            current_address=address,
            current_description=description,
            services=services,
            description_budget=budget,
            current_messages=saved_messages,
            drafts=drafts,
            catalog_inputs=inp["catalog_inputs"],
        )
        payload = {
            "fields": [asdict(f) for f in proposal.fields],
            "services": [asdict(s) for s in proposal.services],
            "professionals": [asdict(p) for p in proposal.professionals],
            "catalog_services": [asdict(s) for s in proposal.catalog_services],
            "insurance": asdict(proposal.insurance) if proposal.insurance else None,
            "notes": [asdict(n) for n in proposal.notes],
        }
        await _finish(sid, status=STATUS_DONE, error_code=None, proposal=payload)
        logger.info(
            "context_source_done",
            source_id=source_id,
            fields=len(payload["fields"]),
            services=len(payload["services"]),
            professionals=len(payload["professionals"]),
            catalog_services=len(payload["catalog_services"]),
        )
    except (UnsafeUrlError, FetchError, ExtractionError, ExtractionFailed) as exc:
        await _finish(sid, status=STATUS_FAILED, error_code=exc.code, proposal=None)
        logger.warning("context_source_failed", source_id=source_id, error_code=exc.code)
    except asyncio.CancelledError:
        # arq's timeout / a worker shutdown: close the row (and erase the input) before dying.
        await asyncio.shield(
            _finish(sid, status=STATUS_FAILED, error_code="model_error", proposal=None)
        )
        logger.warning("context_source_cancelled", source_id=source_id)
        raise
    except Exception as exc:  # noqa: BLE001 - never let a retry re-run (and re-bill) the model
        await _finish(sid, status=STATUS_FAILED, error_code="model_error", proposal=None)
        logger.error("context_source_crashed", source_id=source_id, error_type=type(exc).__name__)
