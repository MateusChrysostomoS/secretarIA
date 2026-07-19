"""Tests for the `history` / `smb_app_state_sync` webhook fields (contract v1 §10):
sync-status transitions, `mode_resolved_at` / `connected_at`, and NO-PII logging.

Same in-memory-sqlite pattern as test_handover_echoes.py / test_resolve_tenant.py:
a real aiosqlite engine (StaticPool), monkeypatched in place of the
Postgres-backed `secretaria.workers.tasks.async_session_factory`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.schemas.webhook import (  # noqa: E402
    WebhookHistoryItem,
    WebhookValue,
    history_item_is_final,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"  # matches META_PHONE_NUMBER_ID above

# A patient full_name/phone_number that would NEVER be allowed to leak into a
# log line via smb_app_state_sync's `contact` sub-object (LGPD).
_PII_NAME = "Fulano da Silva Sauro"
_PII_PHONE = "5511988887777"


# --------------------------------------------------------------------------
# history_item_is_final — pure unit tests (schemas/webhook.py)
# --------------------------------------------------------------------------


def _history_item(**kw) -> WebhookHistoryItem:
    return WebhookHistoryItem.model_validate(kw)


def test_final_true_when_phase_complete():
    item = _history_item(metadata={"phase": "COMPLETE", "chunk_order": 3, "progress": 100})
    assert history_item_is_final(item) is True


def test_final_false_when_phase_complete_chunk():
    """COMPLETE_CHUNK means "this chunk is done, more are coming" - not final."""
    item = _history_item(metadata={"phase": "COMPLETE_CHUNK", "chunk_order": 0, "progress": 20})
    assert history_item_is_final(item) is False


def test_final_case_insensitive_phase():
    item = _history_item(metadata={"phase": "complete"})
    assert history_item_is_final(item) is True


def test_final_true_when_progress_100_without_complete_phase():
    """Unknown-variant tolerance: a numeric 100% is enough even without a
    literal "COMPLETE" phase string."""
    item = _history_item(metadata={"phase": "SOME_OTHER_VALUE", "progress": 100})
    assert history_item_is_final(item) is True


def test_final_false_when_progress_below_100():
    item = _history_item(metadata={"phase": None, "progress": 42})
    assert history_item_is_final(item) is False


def test_final_true_when_errors_present_declined_sharing():
    item = _history_item(
        metadata={"phase": "COMPLETE_CHUNK", "progress": 0},
        errors=[{"code": 2593109, "message": "History sync is turned off"}],
    )
    assert history_item_is_final(item) is True


def test_final_false_when_no_metadata_no_errors():
    item = _history_item()
    assert history_item_is_final(item) is False


def test_final_tolerates_unknown_progress_type():
    """A non-numeric `progress` (unknown variant) must never raise."""
    item = _history_item(metadata={"phase": None, "progress": "unknown"})
    assert history_item_is_final(item) is False


# --------------------------------------------------------------------------
# DB fixture (shared in-memory sqlite, StaticPool)
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed_tenant(db, **overrides) -> Tenant:
    fields = dict(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=PHONE_NUMBER_ID,
        is_active=True,
    )
    fields.update(overrides)
    async with db() as session:
        tenant = Tenant(**fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _refresh(db, tenant: Tenant) -> Tenant:
    async with db() as session:
        return await session.get(Tenant, tenant.id)


class _CapturingLogger:
    """Records every log call (event name + args/kwargs) instead of printing.

    Used to assert NO log call this handler makes ever contains a PII string
    (LGPD - contract v1 §10), regardless of which level it logged at.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple, dict]] = []

    def _record(self, level: str, event: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((level, event, args, kwargs))

    def info(self, event, *a, **kw):
        self._record("info", event, a, kw)

    def warning(self, event, *a, **kw):
        self._record("warning", event, a, kw)

    def error(self, event, *a, **kw):
        self._record("error", event, a, kw)

    def rendered_text(self) -> str:
        """Every captured call flattened to one string, for a substring check."""
        return "".join(f"{lvl}{event}{args}{kwargs}" for lvl, event, args, kwargs in self.calls)


def _history_value(*, chunks: list[dict]) -> WebhookValue:
    return WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "history": chunks,
        }
    )


def _state_sync_value(*, entries: list[dict]) -> WebhookValue:
    return WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "state_sync": entries,
        }
    )


# --------------------------------------------------------------------------
# _handle_history — sync-status transitions
# --------------------------------------------------------------------------


async def test_first_in_progress_chunk_sets_status_and_mode_resolved(db):
    tenant = await _seed_tenant(db)
    value = _history_value(
        chunks=[{"metadata": {"phase": "COMPLETE_CHUNK", "chunk_order": 0, "progress": 10}}]
    )

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "in_progress"
    assert refreshed.history_synced_at is None
    assert refreshed.mode_resolved_at is not None


async def test_final_chunk_sets_done_and_synced_at(db):
    tenant = await _seed_tenant(db)
    value = _history_value(chunks=[{"metadata": {"phase": "COMPLETE", "progress": 100}}])

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "done"
    assert refreshed.history_synced_at is not None


async def test_single_payload_going_straight_from_none_to_done(db):
    """A single-chunk sync (or an immediate decline) can skip in_progress
    entirely - the final state must be "done", not stuck at "in_progress"."""
    tenant = await _seed_tenant(db)
    value = _history_value(chunks=[{"metadata": {"phase": "COMPLETE", "progress": 100}}])

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "done"


async def test_declined_history_sharing_marks_done(db):
    tenant = await _seed_tenant(db)
    value = _history_value(
        chunks=[
            {
                "metadata": {"phase": "COMPLETE_CHUNK", "progress": 0},
                "errors": [{"code": 2593109, "message": "declined"}],
            }
        ]
    )

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "done"
    assert refreshed.history_synced_at is not None


async def test_status_never_regresses_from_done_to_in_progress(db):
    tenant = await _seed_tenant(db)
    await tasks._handle_history(
        _history_value(chunks=[{"metadata": {"phase": "COMPLETE", "progress": 100}}])
    )
    assert (await _refresh(db, tenant)).history_sync_status == "done"

    # A stray/late in-progress-only chunk must never move it backwards.
    await tasks._handle_history(
        _history_value(chunks=[{"metadata": {"phase": "COMPLETE_CHUNK", "progress": 50}}])
    )

    assert (await _refresh(db, tenant)).history_sync_status == "done"


async def test_multiple_chunks_in_one_payload_any_final_wins(db):
    tenant = await _seed_tenant(db)
    value = _history_value(
        chunks=[
            {"metadata": {"phase": "COMPLETE_CHUNK", "progress": 40}},
            {"metadata": {"phase": "COMPLETE", "progress": 100}},
        ]
    )

    await tasks._handle_history(value)

    assert (await _refresh(db, tenant)).history_sync_status == "done"


async def test_empty_history_list_is_a_no_op(db):
    tenant = await _seed_tenant(db)
    value = _history_value(chunks=[])

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "none"
    assert refreshed.mode_resolved_at is None


async def test_unknown_tenant_does_not_raise(db):
    await _seed_tenant(db, phone_number_id="some-other-number")
    value = _history_value(chunks=[{"metadata": {"phase": "COMPLETE"}}])

    await tasks._handle_history(value)  # must not raise


async def test_history_marks_connected_at_when_null(db):
    tenant = await _seed_tenant(db, connected_at=None)
    value = _history_value(chunks=[{"metadata": {"phase": "COMPLETE_CHUNK", "progress": 5}}])

    await tasks._handle_history(value)

    assert (await _refresh(db, tenant)).connected_at is not None


async def test_history_does_not_overwrite_existing_connected_at(db):
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    tenant = await _seed_tenant(db, connected_at=fixed)
    value = _history_value(chunks=[{"metadata": {"phase": "COMPLETE_CHUNK", "progress": 5}}])

    await tasks._handle_history(value)

    refreshed = await _refresh(db, tenant)
    assert refreshed.connected_at.replace(tzinfo=UTC) == fixed


# --------------------------------------------------------------------------
# _handle_smb_app_state_sync — mode resolution, no-PII logging
# --------------------------------------------------------------------------


async def test_state_sync_sets_mode_resolved_when_null(db):
    tenant = await _seed_tenant(db)
    value = _state_sync_value(entries=[{"type": "contact", "action": "add"}])

    await tasks._handle_smb_app_state_sync(value)

    assert (await _refresh(db, tenant)).mode_resolved_at is not None


async def test_state_sync_never_touches_history_sync_status(db):
    tenant = await _seed_tenant(db)
    value = _state_sync_value(entries=[{"type": "contact", "action": "add"}])

    await tasks._handle_smb_app_state_sync(value)

    assert (await _refresh(db, tenant)).history_sync_status == "none"


async def test_state_sync_empty_entries_still_resolves_mode(db):
    """The FIELD itself arriving (even with an empty body) is already the
    signal that Coexistence mode resolved."""
    tenant = await _seed_tenant(db)
    value = _state_sync_value(entries=[])

    await tasks._handle_smb_app_state_sync(value)

    assert (await _refresh(db, tenant)).mode_resolved_at is not None


async def test_state_sync_unknown_tenant_does_not_raise(db):
    await _seed_tenant(db, phone_number_id="some-other-number")
    value = _state_sync_value(entries=[{"type": "contact", "action": "add"}])

    await tasks._handle_smb_app_state_sync(value)  # must not raise


async def test_state_sync_never_logs_contact_pii(db, monkeypatch: pytest.MonkeyPatch):
    """A `contact` sub-object carrying a real name/phone must never appear in
    any log call this handler makes (LGPD - contract v1 §10)."""
    await _seed_tenant(db)
    capture = _CapturingLogger()
    monkeypatch.setattr(tasks, "logger", capture)

    value = _state_sync_value(
        entries=[
            {
                "type": "contact",
                "action": "add",
                "contact": {"full_name": _PII_NAME, "phone_number": _PII_PHONE},
            }
        ]
    )
    await tasks._handle_smb_app_state_sync(value)

    assert capture.calls  # sanity: something was actually logged
    rendered = capture.rendered_text()
    assert _PII_NAME not in rendered
    assert _PII_PHONE not in rendered


async def test_history_never_logs_thread_content(db, monkeypatch: pytest.MonkeyPatch):
    """Same LGPD guarantee for `history` - message bodies inside `threads`
    must never appear in a log call."""
    await _seed_tenant(db)
    capture = _CapturingLogger()
    monkeypatch.setattr(tasks, "logger", capture)

    secret_message = "Minha consulta é sobre um problema confidencial de saúde"
    value = _history_value(
        chunks=[
            {
                "metadata": {"phase": "COMPLETE", "progress": 100},
                "threads": [
                    {
                        "id": "5511999999999",
                        "messages": [{"type": "text", "text": {"body": secret_message}}],
                    }
                ],
            }
        ]
    )
    await tasks._handle_history(value)

    assert capture.calls  # sanity: something was actually logged
    assert secret_message not in capture.rendered_text()


# --------------------------------------------------------------------------
# process_webhook_event — field routing end-to-end
# --------------------------------------------------------------------------


async def test_process_webhook_event_routes_history_field(db):
    tenant = await _seed_tenant(db)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "history",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "history": [{"metadata": {"phase": "COMPLETE", "progress": 100}}],
                        },
                    }
                ],
            }
        ],
    }

    await tasks.process_webhook_event({}, payload)

    refreshed = await _refresh(db, tenant)
    assert refreshed.history_sync_status == "done"
    assert refreshed.mode_resolved_at is not None


async def test_process_webhook_event_routes_smb_app_state_sync_field(db):
    tenant = await _seed_tenant(db)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "smb_app_state_sync",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "state_sync": [{"type": "contact", "action": "add"}],
                        },
                    }
                ],
            }
        ],
    }

    await tasks.process_webhook_event({}, payload)

    refreshed = await _refresh(db, tenant)
    assert refreshed.mode_resolved_at is not None
