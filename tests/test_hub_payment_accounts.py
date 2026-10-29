"""Hub endpoints of the payment connection (api/hub/payment_accounts.py), spec section 12.3.

The single exception to "a credential never enters through a form": ONE write-only field, the
Asaas API key, in `PUT /tenants/me/payment-accounts/asaas/connection`. Every test here that
sends a key also proves the key is in NO response - success, refusal, validation error.
"""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub.deps import (
    get_current_tenant,
    require_clinic_clinician,
    require_clinic_manager,
)
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, asaas_connection, asaas_provider
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_probes import (
    CODE_EXCESS,
    CODE_UNAVAILABLE,
    Verdict,
    VerdictKind,
)
from tests.payments_support import FakeArqPool, FakeAsaasClient, make_sessionmaker

BASE = "https://secretaria.example.test"
KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
CAPS = {"customers": True, "payments": True, "webhooks": True}
ENDPOINT = "/tenants/me/payment-accounts"
CONNECTION = f"{ENDPOINT}/asaas/connection"
ACCEPTED = Verdict(VerdictKind.ACCEPTED, None, (), (), dict(CAPS))


@pytest_asyncio.fixture
async def world(monkeypatch):
    from fastapi import Depends

    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="pn-a")
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id="pn-b")
        session.add_all([tenant, other])
        await session.commit()

    async def _session():
        async with maker() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant.id)

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    # The role gate (TASK-020) is proved in tests/test_hub_payment_accounts_roles.py, through the
    # real dependency chain; in THIS file it is a pass so every test exercises the endpoint itself.
    app.dependency_overrides[require_clinic_manager] = lambda: None
    app.dependency_overrides[require_clinic_clinician] = lambda: None
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    monkeypatch.setenv("PAYMENTS_WEBHOOK_BASE_URL", BASE)
    get_settings.cache_clear()

    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    verdict = {"value": ACCEPTED}

    async def probes(api_key):
        return verdict["value"]

    monkeypatch.setattr(asaas_connection, "run_probe_suite", probes)
    yield tenant, other, maker, pool, fake, verdict
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(require_clinic_manager, None)
    app.dependency_overrides.pop(require_clinic_clinician, None)
    get_settings.cache_clear()
    await engine.dispose()


async def _put(client: AsyncClient, body):
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    headers = {"content-type": "application/json"}
    return await client.put(CONNECTION, content=content, headers=headers)


async def test_a_good_key_connects_and_the_response_carries_only_the_status(client, world):
    tenant, _, maker, _, fake, _ = world
    response = await _put(client, {"api_key": KEY})

    assert response.status_code == 200
    assert response.json() == {
        "provider": "asaas",
        "status": "connected",
        "incomplete_reason": None,
        "capabilities": CAPS,
        "excess_groups": [],
        "excess_labels": [],
    }
    assert KEY not in response.text and "authToken" not in response.text
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.status == "connected" and row.api_key_encrypted != KEY


async def test_an_excess_permission_refuses_with_422_group_codes_and_pt_br_labels(client, world):
    tenant, _, maker, _, fake, verdict = world
    verdict["value"] = Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("BILL", "TRANSFER"))
    response = await _put(client, {"api_key": KEY})

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "key_excess_permissions",
            "missing_groups": [],
            "excess_groups": ["BILL", "TRANSFER"],
            "missing_labels": [],
            "excess_labels": ["Pagamento de contas", "Transferências"],
        }
    }
    assert KEY not in response.text
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []
    assert fake.calls == []


@pytest.mark.parametrize("code", [CODE_UNAVAILABLE, "probes_not_qualified"])
async def test_an_inconclusive_check_is_503_and_stores_nothing(client, world, code):
    _, _, maker, _, _, verdict = world
    verdict["value"] = Verdict(VerdictKind.INCONCLUSIVE, code)
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": code}}
    assert KEY not in response.text
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_a_key_of_the_wrong_shape_is_a_clean_422(client, world):
    response = await _put(client, {"api_key": "$aact_hmlg_" + "a" * 40})  # sandbox key vs prod
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "key_invalid",
            "missing_groups": [],
            "excess_groups": [],
            "missing_labels": [],
            "excess_labels": [],
        }
    }


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": 12345},
        {},
        {"api_key": KEY, "extra": "field"},
        {"key": KEY},
        [KEY],
        "not json " + KEY,
        b"{broken " + KEY.encode(),
        {"api_key": KEY + "x" * 5000},
    ],
)
async def test_a_malformed_body_is_422_and_the_response_never_echoes_what_was_sent(
    client, world, body
):
    response = await _put(client, body)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_body"}}
    assert KEY not in response.text and "x" * 50 not in response.text


async def test_the_eleventh_attempt_in_the_window_is_429(client, world):
    _, _, _, pool, _, verdict = world
    verdict["value"] = Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("BILL",))
    statuses = [(await _put(client, {"api_key": KEY})).status_code for _ in range(11)]
    assert statuses == [422] * 10 + [429]
    assert list(pool.ttls.values()) == [600]


async def test_without_a_working_rate_limiter_the_endpoint_fails_closed(client, world, monkeypatch):
    from secretaria.main import app

    monkeypatch.setattr(app.state, "arq_pool", None, raising=False)
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "rate_limiter_unavailable"}}


async def test_listing_shows_status_only_and_never_a_secret(client, world):
    tenant, other, maker, _, _, _ = world
    await _put(client, {"api_key": KEY})
    async with maker() as session:
        legacy = await accounts.get_or_create_account_row(
            session, tenant_id=other.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(legacy, api_key=KEY + "legacy")
        legacy.status = "connected"  # another clinic: must never appear
        await session.commit()

    response = await client.get(ENDPOINT)
    body = response.json()
    assert response.status_code == 200 and len(body) == 1
    (item,) = body
    assert set(item) == {
        "provider", "status", "incomplete_reason", "capabilities", "excess_groups",
        "excess_labels", "connected_at", "last_verified_at", "ready_for_before",
        # TASK-071 Plan 3 (task 4): the charge health the Portal's badge reads.
        "charge_health", "last_charge_failure_reason", "last_charge_failure_text",
        "last_charge_failure_at",
    }
    assert item["provider"] == "asaas" and item["ready_for_before"] is True
    assert KEY not in response.text and "legacy" not in response.text


async def test_the_retry_endpoint_finishes_an_incomplete_connection_without_the_key(
    client, world, monkeypatch
):
    _, _, _, _, fake, _ = world
    fake.errors["create_webhook"] = AsaasError(500)
    first = await _put(client, {"api_key": KEY})
    assert first.json()["status"] == "incomplete"
    assert first.json()["incomplete_reason"] == "webhook_create_failed"

    fake.errors.clear()
    second = await client.post(f"{ENDPOINT}/asaas/retry-webhook")
    assert second.status_code == 200
    assert second.json()["status"] == "connected" and KEY not in second.text


async def test_retry_without_a_stored_key_is_422(client, world):
    response = await client.post(f"{ENDPOINT}/asaas/retry-webhook")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "no_key"


async def test_disconnect_revokes_the_account_and_a_second_call_is_404(client, world):
    tenant, _, maker, _, _, _ = world
    await _put(client, {"api_key": KEY})
    first = await client.delete(f"{ENDPOINT}/asaas")
    assert first.status_code == 200 and first.json()["status"] == "revoked"
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.api_key_encrypted is None
    assert (await client.delete(f"{ENDPOINT}/asaas")).status_code == 404


async def test_the_endpoints_require_the_hub_token(client, world):
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)
    for method, path in (("GET", ENDPOINT), ("PUT", CONNECTION), ("DELETE", f"{ENDPOINT}/asaas")):
        response = await client.request(method, path, json={"api_key": KEY})
        assert response.status_code == 401, (method, path)
        assert KEY not in response.text


async def test_the_paths_are_in_the_openapi_schema(client, world):
    schema = (await client.get("/openapi.json")).json()
    assert ENDPOINT in schema["paths"] and CONNECTION in schema["paths"]
    assert f"{ENDPOINT}/asaas/retry-webhook" in schema["paths"]


# --------------------------------------------------------------------------
# TASK-053 (spec 2026-10-11 section 7): extra permissions are information while
# ASAAS_REFUSE_EXCESS_PERMISSIONS is off; the frozen JSONs of session 03 come from here.
# --------------------------------------------------------------------------


async def test_an_accepted_key_with_extra_permissions_answers_200_with_the_labels(client, world):
    """While ASAAS_REFUSE_EXCESS_PERMISSIONS is off the key is accepted and the screen gets the
    extra groups as INFORMATION (a note, never a block)."""
    tenant, _, maker, _, _, verdict = world
    verdict["value"] = Verdict(VerdictKind.ACCEPTED, None, (), ("TRANSFER",), dict(CAPS))
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 200
    assert response.json() == {
        "provider": "asaas",
        "status": "connected",
        "incomplete_reason": None,
        "capabilities": CAPS,
        "excess_groups": ["TRANSFER"],
        "excess_labels": ["Transferências"],
    }
    assert KEY not in response.text
    listed = (await client.get(ENDPOINT)).json()
    assert listed[0]["excess_labels"] == ["Transferências"] and listed[0]["status"] == "connected"


def _real_probes(monkeypatch, fake, *, refuse=None, qualified=None, **replies):
    """Undo the fixture's stub: the REAL probe suite runs against the fake Asaas client."""
    from secretaria.services.payments import asaas_probes
    from secretaria.services.payments.asaas import ProbeResponse

    granted = ProbeResponse(200, None, True, None)
    fake.probe_map = {spec.path: granted for spec in asaas_probes.REQUIRED_PROBES}
    every = [*asaas_probes.REQUIRED_PROBES, *asaas_probes.EXCESS_PROBES]
    for group, reply in replies.items():
        fake.probe_map[next(s.path for s in every if s.group == group)] = reply
    monkeypatch.setattr(asaas_connection, "run_probe_suite", asaas_probes.run_probe_suite)
    monkeypatch.setattr(asaas_probes, "QUALIFIED_EXCESS_GROUPS", frozenset())
    for name, value in (
        ("ASAAS_REFUSE_EXCESS_PERMISSIONS", refuse),
        ("ASAAS_PROBES_QUALIFIED", qualified),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    return granted


async def test_flag_off_the_real_probes_connect_a_key_holding_extras(client, world, monkeypatch):
    """The G1 fix end to end: with BOTH flags at their defaults (off), a key that can do what the
    product needs is connected - before TASK-053 every key was a 503 probes_not_qualified."""
    import structlog

    tenant, _, maker, _, fake, _ = world
    granted = _real_probes(monkeypatch, fake, TRANSFER=None)
    fake.probe_map["/transfers"] = granted
    with structlog.testing.capture_logs() as logs:
        response = await _put(client, {"api_key": KEY})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["excess_groups"]) == ("connected", ["TRANSFER"])
    assert KEY not in response.text and KEY not in repr(logs)


async def test_flag_off_the_real_probes_still_refuse_a_key_missing_a_needed_permission(
    client, world, monkeypatch
):
    from secretaria.services.payments.asaas import ProbeResponse

    tenant, _, maker, _, fake, _ = world
    _real_probes(
        monkeypatch, fake, WEBHOOK=ProbeResponse(403, "insufficient_permission", False, None)
    )
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "missing_permission",
            "missing_groups": ["WEBHOOK"],
            "excess_groups": [],
            "missing_labels": ["Webhooks"],
            "excess_labels": [],
        }
    }
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_flag_off_a_provider_outage_is_a_503_and_stores_nothing(client, world, monkeypatch):
    from secretaria.services.payments.asaas import ProbeResponse

    tenant, _, maker, _, fake, _ = world
    _real_probes(monkeypatch, fake, PAYMENT=ProbeResponse(503, None, False, None))
    monkeypatch.setattr("secretaria.services.payments.asaas_probes._RETRY_WAITS", (0.0, 0.0))
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "verification_unavailable"}}
    assert KEY not in response.text
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_flag_on_without_qualification_keeps_the_old_fail_closed_503(
    client, world, monkeypatch
):
    tenant, _, maker, _, fake, _ = world
    _real_probes(monkeypatch, fake, refuse="true")
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "probes_not_qualified"}}
    assert fake.calls == []


async def test_flag_on_and_qualified_refuses_the_extras_as_d14(client, world, monkeypatch):
    tenant, _, maker, _, fake, _ = world
    granted = _real_probes(monkeypatch, fake, refuse="true", qualified="true")
    fake.probe_map["/transfers"] = granted
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "key_excess_permissions"
    assert response.json()["detail"]["excess_labels"] == ["Transferências"]
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


# ---- TASK-071 Plan 3, task 4: the charge health of the account (the Portal's badge) ----------


async def _listed(client, world, *, failure=None, success=False):
    """Connect THIS clinic, optionally record a charge failure / success, return its item."""
    tenant, _, maker, _, _, _ = world
    assert (await _put(client, {"api_key": KEY})).status_code == 200
    async with maker() as session:
        if failure is not None:
            await accounts.record_charge_failure(
                session, tenant_id=tenant.id, provider="asaas", reason=failure, stage="charge"
            )
        if success:
            await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    (item,) = response.json()
    return item


async def test_charge_health_is_untested_before_any_charge(client, world):
    item = await _listed(client, world)
    assert item["charge_health"] == "untested"
    assert item["last_charge_failure_reason"] is None
    assert item["last_charge_failure_text"] is None
    assert item["last_charge_failure_at"] is None


async def test_charge_health_failing_carries_the_reason_its_text_and_when(client, world):
    from datetime import UTC, datetime

    from secretaria.services.payments.failure_reason import FailureReason, user_text

    item = await _listed(client, world, failure=FailureReason.pix_key_missing)

    assert item["charge_health"] == "failing"
    assert item["last_charge_failure_reason"] == "pix_key_missing"
    assert item["last_charge_failure_text"] == user_text(FailureReason.pix_key_missing)
    at = datetime.fromisoformat(item["last_charge_failure_at"])
    assert at.tzinfo is not None  # an ISO instant the Portal can show in the clinic's time
    assert abs((datetime.now(UTC) - at).total_seconds()) < 60
    assert KEY not in json.dumps(item)


async def test_charge_health_ok_only_after_a_successful_charge(client, world):
    item = await _listed(client, world, failure="rate_limited", success=True)

    assert item["charge_health"] == "ok"
    # The last failure stays as history (the badge shows it only while failing).
    assert item["last_charge_failure_reason"] == "rate_limited"
    assert item["last_charge_failure_text"]


async def test_a_stray_stored_reason_is_shown_as_unknown(client, world):
    from secretaria.services.payments.failure_reason import FailureReason, user_text

    tenant, _, maker, _, _, _ = world
    item = await _listed(client, world, failure="provider_unavailable")
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        row.last_charge_failure_reason = "Asaas: free text"  # never written by the code
        await session.commit()
    (item,) = (await client.get(ENDPOINT)).json()
    assert item["last_charge_failure_reason"] == "unknown"
    assert item["last_charge_failure_text"] == user_text(FailureReason.unknown)
    assert "free text" not in json.dumps(item)
