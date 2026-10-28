"""AsaasProvider (services/payments/asaas_provider.py): the contract suite + Asaas specifics."""

import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from secretaria.services.payments import asaas_provider
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_provider import AsaasProvider
from secretaria.services.payments.provider import (
    ChargeRequest,
    PaymentAccount,
    PaymentStatus,
    ProviderError,
)
from tests.payment_provider_contract import (
    AMOUNT,
    CHARGE_ID,
    REFERENCE,
    ProviderContract,
)
from tests.payments_support import FakeAsaasClient

TOKEN = "webhook-token-secret-0123456789"
KEY = "api-key-secret-0123456789"


def _account() -> PaymentAccount:
    return PaymentAccount(
        tenant_id=uuid4(), provider="asaas", webhook_slug="slug-1", api_key=KEY, webhook_token=TOKEN
    )


def _install(monkeypatch, fake: FakeAsaasClient) -> FakeAsaasClient:
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    return fake


def _payment(state: str, *, amount_cents: int = AMOUNT, reference: str = REFERENCE) -> dict:
    status = {
        "open": "PENDING",
        "paid": "RECEIVED",
        "expired": "OVERDUE",
        "refunded": "REFUNDED",
    }[state]
    body = {
        "id": CHARGE_ID,
        "status": status,
        "value": amount_cents / 100,
        "externalReference": reference,
        "billingType": "PIX",
        "deleted": False,
    }
    if state == "paid":
        body["paymentDate"] = "2026-09-29"
    return body


class TestAsaasProviderContract(ProviderContract):
    provider = AsaasProvider()
    account = _account()

    def webhook_request(
        self,
        kind,
        *,
        charge_id=CHARGE_ID,
        amount_cents=AMOUNT,
        reference=REFERENCE,
        event_id="evt_1",
    ):
        event = {
            "paid": "PAYMENT_RECEIVED",
            "expired": "PAYMENT_OVERDUE",
            "refunded": "PAYMENT_REFUNDED",
            "other": "PAYMENT_CREATED",
        }[kind]
        body = {
            "id": event_id,
            "event": event,
            "dateCreated": "2026-09-29 10:00:00",
            "payment": {
                "id": charge_id,
                "value": amount_cents / 100,
                "externalReference": reference,
                "billingType": "PIX",
            },
        }
        headers = {"asaas-access-token": TOKEN, "content-type": "application/json"}
        return headers, json.dumps(body).encode(), {}

    def tampered_headers(self, headers):
        return {**headers, "asaas-access-token": TOKEN + "x"}

    def stage_charge(self, monkeypatch, *, state, amount_cents=AMOUNT, reference=REFERENCE):
        payment = _payment(state, amount_cents=amount_cents, reference=reference)
        _install(monkeypatch, FakeAsaasClient(payment=payment))

    def stage_http_failure(self, monkeypatch, status):
        errors = {"get_payment": AsaasError(status), "delete_payment": AsaasError(status)}
        _install(monkeypatch, FakeAsaasClient(errors=errors))

    def stage_cancel(self, monkeypatch, *, outcome):
        errors = {}
        if outcome == "already_gone":
            errors["delete_payment"] = AsaasError(404)
        if outcome == "fails":
            errors["delete_payment"] = AsaasError(500)
        _install(monkeypatch, FakeAsaasClient(errors=errors, payment=_payment("open")))


# --------------------------------------------------------------------------
# create_pix_charge
# --------------------------------------------------------------------------


def _request(**overrides) -> ChargeRequest:
    fields = dict(
        amount_cents=6000,
        external_reference="dep-1",
        description="Sinal - Consulta - Clinica",
        due_date=date(2026, 9, 29),
        payer_name="Maria",
        payer_phone="5511999999999",
    )
    fields.update(overrides)
    return ChargeRequest(**fields)


async def test_create_charge_creates_customer_then_charge_then_qr(monkeypatch):
    fake = _install(monkeypatch, FakeAsaasClient(customer_id="cus_9", payment_id="pay_9"))
    created = await AsaasProvider().create_pix_charge(_account(), _request())

    assert created.provider_charge_id == "pay_9"
    assert created.copy_paste == "00020126copiaecola6304ABCD"
    assert created.provider_customer_id == "cus_9"
    assert [c[0] for c in fake.calls] == ["create_customer", "create_pix_payment", "get_pix_qr"]
    assert fake.calls[0] == ("create_customer", "Maria", "5511999999999", None)
    assert fake.calls[1] == ("create_pix_payment", "cus_9", 6000, "dep-1", "2026-09-29")


async def test_create_charge_reuses_a_known_customer(monkeypatch):
    fake = _install(monkeypatch, FakeAsaasClient())
    created = await AsaasProvider().create_pix_charge(
        _account(), _request(provider_customer_id="cus_known")
    )
    assert "create_customer" not in [c[0] for c in fake.calls]
    assert created.provider_customer_id == "cus_known"


async def test_create_charge_forwards_the_payer_document_only_to_the_customer_call(monkeypatch):
    fake = _install(monkeypatch, FakeAsaasClient())
    await AsaasProvider().create_pix_charge(_account(), _request(payer_document="52998224725"))
    assert fake.calls[0] == ("create_customer", "Maria", "5511999999999", "52998224725")
    assert "52998224725" not in json.dumps(fake.calls[1:])


async def test_customer_rejection_without_a_document_is_payer_document_required(monkeypatch):
    _install(monkeypatch, FakeAsaasClient(errors={"create_customer": AsaasError(400, "invalid")}))
    with pytest.raises(ProviderError) as missing:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert missing.value.code == "payer_document_required"
    assert missing.value.retryable is False

    _install(monkeypatch, FakeAsaasClient(errors={"create_customer": AsaasError(400, "invalid")}))
    with pytest.raises(ProviderError) as with_document:
        await AsaasProvider().create_pix_charge(_account(), _request(payer_document="1"))
    assert with_document.value.code == "customer_failed"


async def test_charge_rejected_for_a_missing_document_is_payer_document_required(monkeypatch):
    # Sandbox fact (TASK-067): POST /customers ACCEPTS a customer without cpfCnpj, then
    # POST /payments answers 400 invalid_object "preencher o CPF ou CNPJ do cliente".
    missing = AsaasError(400, "invalid_object", document_missing=True)
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": missing}))
    with pytest.raises(ProviderError) as asked:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert asked.value.code == "payer_document_required"
    assert asked.value.retryable is False

    # With a document already sent, the same answer is just a failed charge (no loop of questions).
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": missing}))
    with pytest.raises(ProviderError) as sent:
        await AsaasProvider().create_pix_charge(_account(), _request(payer_document="52998224725"))
    assert sent.value.code == "charge_failed"

    # A 400 that is NOT about the document stays a failed charge.
    other = AsaasError(400, "invalid_object")
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": other}))
    with pytest.raises(ProviderError) as unrelated:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert unrelated.value.code == "charge_failed"


async def test_charge_failure_is_a_provider_error_and_5xx_is_retryable(monkeypatch):
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": AsaasError(503)}))
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert (failure.value.code, failure.value.retryable) == ("charge_failed", True)


async def test_a_charge_answer_without_an_id_is_refused(monkeypatch):
    fake = FakeAsaasClient(payment_id="")
    _install(monkeypatch, fake)
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert failure.value.code == "charge_missing_id"


async def test_a_qr_failure_keeps_the_charge_and_reports_no_copy_paste(monkeypatch):
    _install(monkeypatch, FakeAsaasClient(errors={"get_pix_qr": RuntimeError("qr down")}))
    created = await AsaasProvider().create_pix_charge(_account(), _request())
    assert created.provider_charge_id == "pay_fake"
    assert created.copy_paste is None


async def test_description_is_cut_to_the_255_characters_asaas_accepts(monkeypatch):
    captured = {}

    class Capturing(FakeAsaasClient):
        async def create_pix_payment(
            self, customer_id, value_cents, external_reference, description, due_date
        ):
            captured["description"] = description
            return await super().create_pix_payment(
                customer_id, value_cents, external_reference, description, due_date
            )

    _install(monkeypatch, Capturing())
    await AsaasProvider().create_pix_charge(_account(), _request(description="x" * 400))
    assert len(captured["description"]) == 255


# --------------------------------------------------------------------------
# get_charge / parse_webhook specifics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payment", "expected"),
    [
        ({"status": "CONFIRMED", "value": 60}, PaymentStatus.PAID),
        ({"status": "RECEIVED_IN_CASH", "value": 60}, PaymentStatus.OTHER),
        ({"status": "PENDING", "deleted": True}, PaymentStatus.EXPIRED),
        ({"status": "SOMETHING_NEW"}, PaymentStatus.OTHER),
        ({"status": "REFUND_REQUESTED"}, PaymentStatus.REFUNDED),
    ],
)
async def test_get_charge_status_mapping(monkeypatch, payment, expected):
    _install(monkeypatch, FakeAsaasClient(payment=payment))
    assert (await AsaasProvider().get_charge(_account(), "pay_1")).status is expected


async def test_get_charge_reads_amount_in_cents_without_float_drift(monkeypatch):
    _install(monkeypatch, FakeAsaasClient(payment={"status": "RECEIVED", "value": 59.9}))
    assert (await AsaasProvider().get_charge(_account(), "pay_1")).paid_amount_cents == 5990


@pytest.mark.parametrize(
    ("event", "status"),
    [
        ("PAYMENT_CONFIRMED", PaymentStatus.PAID),
        ("PAYMENT_DELETED", PaymentStatus.EXPIRED),
        ("PAYMENT_PARTIALLY_REFUNDED", PaymentStatus.REFUNDED),
        ("PAYMENT_RESTORED", PaymentStatus.OTHER),
    ],
)
def test_parse_event_type_mapping(event, status):
    body = json.dumps({"id": "e", "event": event, "payment": {"id": "pay_1", "value": 60}})
    assert AsaasProvider().parse_webhook(body.encode())[0].status is status


def test_parse_builds_a_deterministic_event_id_when_asaas_sends_none():
    body = json.dumps({"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1", "value": 60}})
    assert AsaasProvider().parse_webhook(body.encode())[0].event_id == "PAYMENT_RECEIVED:pay_1"


def test_parse_reads_the_paid_moment_in_utc_from_the_event_timestamp():
    body = json.dumps(
        {
            "id": "e",
            "event": "PAYMENT_RECEIVED",
            "dateCreated": "2026-09-29 10:00:00",  # Asaas timestamps are Brasilia time
            "payment": {"id": "pay_1", "value": 60},
        }
    )
    paid_at = AsaasProvider().parse_webhook(body.encode())[0].paid_at
    assert paid_at == datetime(2026, 9, 29, 13, 0, tzinfo=UTC)


def test_parse_a_body_without_a_payment_is_nothing_to_do():
    assert AsaasProvider().parse_webhook(json.dumps({"event": "ACCOUNT_STATUS"}).encode()) == []


def test_verify_webhook_tolerates_a_token_of_a_different_length():
    provider, account = AsaasProvider(), _account()
    assert provider.verify_webhook(account, {"Asaas-Access-Token": "short"}, b"{}") is False
    assert provider.verify_webhook(account, {"Asaas-Access-Token": TOKEN}, b"{}") is True


async def test_cancel_treats_a_400_on_an_already_deleted_charge_as_success(monkeypatch):
    _install(
        monkeypatch,
        FakeAsaasClient(
            errors={"delete_payment": AsaasError(400, "invalid_action")},
            payment={"status": "PENDING", "deleted": True},
        ),
    )
    assert await AsaasProvider().cancel_charge(_account(), "pay_1") is None


async def test_the_api_key_is_handed_to_the_client_factory_and_nowhere_else(monkeypatch):
    seen = []

    def factory(api_key):
        seen.append(api_key)
        return FakeAsaasClient(payment=_payment("open"))

    monkeypatch.setattr(asaas_provider, "client_for", factory)
    await AsaasProvider().get_charge(_account(), "pay_1")
    assert seen == [KEY]


def test_a_bare_payment_date_today_is_read_as_now_not_midnight():
    today = datetime.now(asaas_provider._ASAAS_TZ).date().isoformat()
    moment = asaas_provider._paid_moment(today)
    assert moment is not None
    assert abs((datetime.now(UTC) - moment).total_seconds()) < 5


def test_a_bare_payment_date_in_the_past_is_noon_of_that_day_in_brasilia():
    moment = asaas_provider._paid_moment("2026-01-10")
    local = moment.astimezone(asaas_provider._ASAAS_TZ)
    assert local.strftime("%Y-%m-%d %H:%M") == "2026-01-10 12:00"


def test_a_payment_date_with_a_time_is_kept_as_is():
    moment = asaas_provider._paid_moment("2026-01-10 08:15:30")
    assert moment.astimezone(asaas_provider._ASAAS_TZ).strftime("%H:%M:%S") == "08:15:30"
    assert asaas_provider._paid_moment(None) is None


# --------------------------------------------------------------------------
# Plano 2, Task 2: a safe reason + stage on every create failure
# --------------------------------------------------------------------------


async def test_a_customer_failure_carries_stage_reason_and_code_hint(monkeypatch):
    _install(monkeypatch, FakeAsaasClient(errors={"create_customer": AsaasError(400, "bad")}))
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(_account(), _request(payer_document="1"))
    assert failure.value.code == "customer_failed"
    assert (failure.value.stage, failure.value.reason) == ("customer", "customer_rejected")
    assert (failure.value.status_code, failure.value.code_hint) == (400, "bad")


async def test_a_customer_document_complaint_is_customer_document_invalid(monkeypatch):
    error = AsaasError(400, "invalid_cpfCnpj", text_reason="document")
    _install(monkeypatch, FakeAsaasClient(errors={"create_customer": error}))
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(_account(), _request(payer_document="1"))
    assert failure.value.reason == "customer_document_invalid"


@pytest.mark.parametrize(
    "status, reason",
    [
        (400, "charge_rejected"),
        (401, "api_key_invalid"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
        (0, "provider_unavailable"),
    ],
)
async def test_a_charge_failure_carries_stage_and_reason(monkeypatch, status, reason):
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": AsaasError(status)}))
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(_account(), _request(payer_document="1"))
    assert failure.value.code == "charge_failed"
    assert (failure.value.stage, failure.value.reason) == ("charge", reason)


async def test_a_missing_document_keeps_its_code_and_gains_a_reason(monkeypatch):
    missing = AsaasError(400, "invalid_object", document_missing=True)
    _install(monkeypatch, FakeAsaasClient(errors={"create_pix_payment": missing}))
    with pytest.raises(ProviderError) as asked:
        await AsaasProvider().create_pix_charge(_account(), _request())
    assert asked.value.code == "payer_document_required"
    assert (asked.value.stage, asked.value.reason) == ("charge", "charge_rejected")


async def test_a_missing_api_key_is_a_config_failure():
    account = PaymentAccount(tenant_id=uuid4(), provider="asaas", api_key=None)
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().create_pix_charge(account, _request())
    assert failure.value.code == "no_api_key"
    assert (failure.value.stage, failure.value.reason) == ("config", "api_key_invalid")
