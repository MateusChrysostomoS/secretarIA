"""No secret and no payer data ever leaves through a log, a job argument, a response or a table
(spec sections 14 and 16: "the pasted key never leaks"; tenant-secrets-encryption skill).

Proof style: each check first shows the redactor DOES act (so the absence is not vacuous),
then that the real flows never hand it a secret in the first place.
"""

import ast
import inspect
import json
import pathlib
import re
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient

import secretaria.core.logging as core_logging
from secretaria.api.hub.deps import get_current_tenant, require_clinic_manager
from secretaria.config import get_settings
from secretaria.core.database import Base, get_session
from secretaria.core.logging import get_logger, redact_free_text, redact_secrets, setup_logging
from secretaria.models import Tenant
from secretaria.schemas.payment_accounts import AsaasConnectionResultOut, PaymentAccountStatusOut
from secretaria.services.payments import asaas_connection, asaas_provider
from secretaria.services.payments.asaas_probes import CODE_EXCESS, Verdict, VerdictKind
from secretaria.services.payments.provider import PaymentAccount
from secretaria.workers import payments_tasks
from tests.payments_support import FakeArqPool, FakeAsaasClient, make_sessionmaker

KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
TOKEN = "webhook-token-secret-0123456789-0123456789"
ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "secretaria"
PAYMENT_MODULES = sorted(
    [*(ROOT / "services" / "payments").glob("*.py"), ROOT / "workers" / "payments_tasks.py",
     ROOT / "workers" / "payments_cron.py", ROOT / "api" / "webhook_payments.py",
     ROOT / "api" / "hub" / "payment_accounts.py"]
)


# --------------------------------------------------------------------------
# the redactor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["payer", "customer", "cpf", "cpf_cnpj", "cpfcnpj", "email", "document", "payer_document",
     "payer_email", "api_key", "webhook_token", "access_token", "authToken",
     "asaas_api_key_encrypted"],
)
def test_payment_personal_and_secret_keys_are_blanked(key):
    out = redact_secrets(None, "info", {"event": "x", key: "SENSITIVE-VALUE", "tenant_id": "t-1"})
    assert out[key] == "***REDACTED***"
    assert out["tenant_id"] == "t-1" and out["event"] == "x"


def test_an_asaas_key_inside_any_string_value_is_redacted_even_under_an_innocent_field():
    out = redact_secrets(
        None, "warning", {"event": "x", "error": f"call failed for {KEY} at step 2", "n": 3}
    )
    assert KEY not in out["error"] and "***REDACTED***" in out["error"]
    assert out["n"] == 3 and "call failed" in out["error"]


def test_the_arq_job_log_filter_also_scrubs_an_asaas_key():
    line = f"→ some_job({{'note': 'hello {KEY}'}}, 'plain')"
    assert KEY not in redact_free_text(line)


def test_a_payment_account_never_renders_its_secrets_when_a_caller_logs_it():
    account = PaymentAccount(tenant_id=uuid4(), provider="asaas", api_key=KEY, webhook_token=TOKEN)
    out = redact_secrets(None, "info", {"event": "x", "account": repr(account)})
    assert KEY not in json.dumps(out) and TOKEN not in json.dumps(out)


# --------------------------------------------------------------------------
# the real flows hand the logger no secret (real structlog pipeline, fd capture)
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def hub(monkeypatch):
    from fastapi import Depends

    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="pn-a")
        session.add(tenant)
        await session.commit()

    async def _session():
        async with maker() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    app.dependency_overrides[require_clinic_manager] = lambda: None  # role gate: Task 19 tests
    monkeypatch.setattr(app.state, "arq_pool", FakeArqPool(), raising=False)
    monkeypatch.setenv("PAYMENTS_WEBHOOK_BASE_URL", "https://secretaria.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: FakeAsaasClient())
    verdict = {"value": Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("TRANSFER",))}

    async def probes(api_key):
        return verdict["value"]

    monkeypatch.setattr(asaas_connection, "run_probe_suite", probes)
    yield verdict
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(require_clinic_manager, None)
    get_settings.cache_clear()
    await engine.dispose()


async def test_connecting_a_key_never_puts_it_or_the_token_in_any_log_line(
    client: AsyncClient, hub, capfd
):
    core_logging._configured = False
    setup_logging()
    log = get_logger("hygiene-canary")
    log.warning("canary_redacted_by_key", api_key=KEY)
    log.warning("canary_redacted_by_value", error=f"boom {KEY}")

    headers = {"content-type": "application/json"}
    path = "/tenants/me/payment-accounts/asaas/connection"
    await client.put(path, content=json.dumps({"api_key": KEY}), headers=headers)  # refused
    hub["value"] = Verdict(VerdictKind.ACCEPTED, None, (), (), {"customers": True})
    await client.put(path, content=json.dumps({"api_key": KEY}), headers=headers)  # connected
    await client.put(path, content=b"{broken " + KEY.encode(), headers=headers)  # malformed
    await client.delete("/tenants/me/payment-accounts/asaas")

    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "canary_redacted_by_key" in output and "canary_redacted_by_value" in output
    assert "asaas_key_probe_refused" in output  # the flow logged: the absence below is real
    assert KEY not in output
    assert KEY[len("$aact_prod_"):] not in output


# --------------------------------------------------------------------------
# structural guards
# --------------------------------------------------------------------------

_LOG_LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
_FORBIDDEN_LOG_FIELDS = {
    "api_key", "apikey", "webhook_token", "access_token", "refresh_token", "authtoken",
    "auth_token", "webhook_secret", "payer_document", "payer_email", "cpf", "cpf_cnpj",
    "cpfcnpj", "document", "email", "payer", "account", "request",
}


@pytest.mark.parametrize("path", PAYMENT_MODULES, ids=lambda p: p.name)
def test_no_payment_module_passes_a_secret_or_payer_field_to_the_logger(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if node.func.attr in _LOG_LEVELS and getattr(receiver, "id", "") == "logger":
            for keyword in node.keywords:
                assert (keyword.arg or "").lower() not in _FORBIDDEN_LOG_FIELDS, (
                    f"{path.name}:{node.lineno} logs {keyword.arg}"
                )


@pytest.mark.parametrize("path", PAYMENT_MODULES, ids=lambda p: p.name)
def test_no_payment_module_prints(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "print"]


def test_job_arguments_are_ids_only():
    assert list(inspect.signature(payments_tasks.process_payment_event).parameters) == [
        "ctx", "event_row_id",
    ]
    assert list(inspect.signature(payments_tasks.fulfill_paid_charge_job).parameters) == [
        "ctx", "deposit_id",
    ]


def test_no_payment_table_has_a_column_for_a_document_an_email_or_a_payer():
    tables = [
        "clinic_payment_accounts", "payment_webhook_events", "patient_payment_customers",
        "payment_attention_items", "pix_deposits",
    ]
    pattern = re.compile(r"cpf|cnpj|document|e_?mail|payer|holder", re.IGNORECASE)
    for name in tables:
        for column in Base.metadata.tables[name].columns:
            assert not pattern.search(column.name), f"{name}.{column.name}"


def test_secret_columns_are_all_named_encrypted():
    columns = [c.name for c in Base.metadata.tables["clinic_payment_accounts"].columns]
    secretish = [c for c in columns if re.search(r"key|token|secret|verifier", c)]
    assert secretish
    assert all(c.endswith("_encrypted") or c.endswith("_expires_at") for c in secretish)


def test_response_schemas_have_no_field_that_could_carry_a_secret():
    for schema in (PaymentAccountStatusOut, AsaasConnectionResultOut):
        for name in schema.model_fields:
            assert not re.search(r"key|token|secret|encrypted|password|cipher", name), name
