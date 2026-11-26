"""Shared pytest fixtures and deterministic test environment."""
import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from tests._patching import workers_ns

# Configure a deterministic environment BEFORE any `secretaria` import, so that
# Settings() picks these values up. Real env vars take precedence over `.env`.
os.environ["APP_ENV"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["META_APP_SECRET"] = "test-app-secret"
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
os.environ["META_ACCESS_TOKEN"] = "test-access-token"
os.environ["META_PHONE_NUMBER_ID"] = "1234567890"
# Valid Fernet key (test-only) so the encrypted-credential seams work in tests.
os.environ["ENCRYPTION_KEY"] = "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w="
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://secretaria:secretaria@localhost:5432/secretaria",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# The 12h default minimum booking notice (production default) would make every older
# test whose fake agenda offers slots relative to the REAL clock pass or fail by the time
# of day. The suite runs with no default; tests/test_booking_min_notice.py turns it on.
os.environ["BOOKING_NOTICE_DEFAULT_HOURS"] = "0"
# The Pix QR is fetched twice with a 1.5 s pause in production; the suite runs it with no pause.
os.environ["ASAAS_QR_RETRY_DELAY_SECONDS"] = "0"


@pytest.fixture(autouse=True)
def _no_turn_apology(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the "patient never unanswered" net out of tests that fake the sender.

    The net counts messages that really leave (`WhatsAppClient._post` /
    `BrainMessageSender._record`). Most older tests replace the client with a
    fake that never reaches those two points, so their perfectly good turns look
    silent and would gain a second, unexpected apology. Tests of the net itself
    (tests/test_turn_safety_net.py) call the real function they captured at
    import time and exercise it explicitly.
    """

    async def _noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(workers_ns, "_send_turn_fallback", _noop)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_unit_gate: run with the REAL usable-unit rule (TASK-090 Adendo 1 §A) instead of "
        "the suite default that treats every active unit as usable and never gates a reply",
    )


@pytest.fixture(autouse=True)
def _units_usable_by_default(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """TASK-090 Adendo 1 §A: the secretária answers only a clinic with a USABLE unit (active +
    complete address), and patients are offered only usable units. Almost every older test
    builds a clinic with no unit, or units with a bare `address` text and no CEP/street/number
    parts, and exercises something else entirely - under the real rule they would all be
    silenced or lose their units.

    So, like `_no_turn_apology` above, the suite default keeps the pre-Adendo behaviour at the
    two seams: the predicate (`professional_units.unit_is_usable`) counts every ACTIVE unit, and
    the reply gate (`workers/shared/unit_gate.reply_blocked_no_usable_unit`) never blocks.
    Tests of the rule itself carry `@pytest.mark.real_unit_gate` and run the real code.
    """
    if request.node.get_closest_marker("real_unit_gate") is not None:
        return
    from secretaria.services import professional_units

    async def _never_blocked(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(professional_units, "unit_is_usable", lambda unit: bool(unit.is_active))
    monkeypatch.setattr(workers_ns, "reply_blocked_no_usable_unit", _never_blocked)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An httpx AsyncClient bound to the FastAPI app via ASGITransport.

    The app lifespan does NOT run under ASGITransport, so these tests need no
    Redis or Postgres connection.
    """
    from secretaria.config import get_settings

    get_settings.cache_clear()

    from secretaria.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _clinic_wide_agenda_viewer():
    """TASK-044 R7: who is looking at the hub agenda comes from brain-api's token
    introspection (api/hub/deps.py::get_agenda_viewer). Tests that override
    `get_current_tenant` never present a token, so by default they see the whole
    clinic - exactly the pre-R7 behaviour. Scoping tests call
    tests/_agenda_viewer.py::view_as; tests of the dependency call it directly.
    """
    from secretaria.api.hub.deps import get_agenda_viewer
    from secretaria.main import app
    from secretaria.services.agenda_visibility import CLINIC_WIDE

    app.dependency_overrides[get_agenda_viewer] = lambda: CLINIC_WIDE
    yield
    app.dependency_overrides.pop(get_agenda_viewer, None)


@pytest.fixture(autouse=True)
def _manager_staff_claim_when_tenant_is_overridden():
    """TASK-091 T4: the configuration routes judge the caller's role from the claim the real
    `get_current_tenant` stored (api/hub/deps.py::get_staff_claim, which fails CLOSED in
    production). Tests that override `get_current_tenant` never present a token, so no claim
    exists: by default such a session is the clinic MANAGER's - exactly the role-blind
    behaviour those tests were written against. When the real `get_current_tenant` ran (tests
    that fake only brain-api's introspection, e.g. tests/test_t091_role_guards.py), the real
    claim is returned untouched, so role tests see the role they asked for.
    """
    from fastapi import Depends, Request

    from secretaria.api.hub.deps import get_current_tenant, get_staff_claim
    from secretaria.core.subscription import SubscriptionClaim
    from secretaria.main import app

    async def _claim(request: Request, tenant=Depends(get_current_tenant)) -> SubscriptionClaim:
        claim = getattr(request.state, "staff_subscription_claim", None)
        if claim is not None:
            return claim
        return SubscriptionClaim(tenant_id=tenant.id, active=True, role="manager")

    app.dependency_overrides[get_staff_claim] = _claim
    yield
    if app.dependency_overrides.get(get_staff_claim) is _claim:
        app.dependency_overrides.pop(get_staff_claim, None)
