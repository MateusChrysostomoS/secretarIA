"""Asaas SANDBOX proof for TASK-016 (F1). Automatic-or-skip; never runs in CI.

    $env:ASAAS_SANDBOX_API_KEY        = '<sandbox key with every permission (the sandbox default)>'
    $env:ASAAS_SANDBOX_API_KEY_SCOPED = '<OPTIONAL: Clientes+Cobrancas+Webhooks, READ_WRITE>'
    uv run python scripts/asaas_sandbox_proof.py

Keys are read ONLY from the process environment. No key -> the script prints one `skipped_all`
line and exits 0 (a skip is not a failure). A key that is not a sandbox key (`$aact_hmlg_`) is
also a skip and is never used. Without the SCOPED key the qualification checks (S12, S13, S14)
are skipped and everything else runs with the default key. The script talks only to
api-sandbox.asaas.com and prints one JSON line per check with a status code and a short
symbolic error code. It never prints a key, the `access_token` header, a response body, a
customer document or a QR payload.
"""

import json
import os
import sys
import uuid
from datetime import date

import httpx

BASE = "https://api-sandbox.asaas.com/v3"
# Synthetic CPF with valid check digits (no real person). Sent only to the SANDBOX.
SYNTHETIC_CPF = "52998224725"
_ALLOWED_EXTRA_TYPES = (bool, int, type(None))
_ALLOWED_EXTRA_TEXT = {"role", "group", "path", "method", "verdict"}


SANDBOX_PREFIX = "$aact_hmlg_"


def require_sandbox_key(value: str | None) -> str:
    if not value or not value.startswith(SANDBOX_PREFIX):
        raise SystemExit("refusing to run: need a SANDBOX key ($aact_hmlg_...) in the environment")
    return value


def resolve_keys(environ) -> tuple[str | None, str | None, str | None]:
    """(default key, scoped key, skip reason). Never raises: a missing key or a key that is not
    a sandbox key is a SKIP, so the executing session never has to stop and ask."""
    full = environ.get("ASAAS_SANDBOX_API_KEY") or ""
    scoped = environ.get("ASAAS_SANDBOX_API_KEY_SCOPED") or ""
    if not full:
        return None, None, "no_sandbox_key"
    if not full.startswith(SANDBOX_PREFIX):
        return None, None, "key_not_sandbox"
    return full, (scoped if scoped.startswith(SANDBOX_PREFIX) else None), None


def _code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        return code[:64] if isinstance(code, str) else None
    return None


def emit(check: str, response: httpx.Response, **extra: object) -> None:
    line: dict[str, object] = {
        "check": check,
        "status": response.status_code,
        "code": _code(response),
    }
    for key, value in extra.items():
        is_short_word = isinstance(value, str) and " " not in value and len(value) <= 40
        if isinstance(value, _ALLOWED_EXTRA_TYPES):
            line[key] = value
        elif key in _ALLOWED_EXTRA_TEXT and is_short_word:
            line[key] = value
        else:
            raise ValueError(f"extra field {key!r} is not allowed to carry free text")
    print(json.dumps(line))


def _call(
    key: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> httpx.Response:
    with httpx.Client(timeout=15.0) as client:
        return client.request(
            method,
            f"{BASE}{path}",
            params=params,
            json=body,
            headers={"access_token": key, "Content-Type": "application/json"},
        )


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# (group, path) - the EXCESS probes of the spec's section 12.3A table. Every one must be
# 200 with the FULL key and 403 insufficient_permission with the MIN key to be qualified.
EXCESS_PROBES = [
    ("TRANSFER", "/transfers"),
    ("BILL", "/bill"),
    ("ANTICIPATION", "/anticipations"),
    ("FINANCIAL_TRANSACTION", "/financialTransactions"),
    ("PIX_TRANSACTION", "/pix/transactions"),
    ("PIX_ADDRESS_KEY", "/pix/addressKeys"),
    ("SUBSCRIPTION", "/subscriptions"),
    ("PAYMENT_LINK", "/paymentLinks"),
    ("INSTALLMENT", "/installments"),
    ("CHARGEBACK", "/chargebacks"),
    ("SUB_ACCOUNT", "/accounts"),
    ("MOBILE_PHONE_RECHARGE", "/mobilePhoneRecharges"),
    ("ACCOUNT_INFO", "/myAccount/commercialInfo"),
]
REQUIRED_PROBES = [("CUSTOMER", "/customers"), ("PAYMENT", "/payments"), ("WEBHOOK", "/webhooks")]


def _charge_lifecycle(key_min: str, customer: str, today: str, ref: str) -> None:
    """S03-S08: create a Pix charge, read it, read its QR, DELETE it twice, read it again."""
    body = {
        "customer": customer,
        "billingType": "PIX",
        "value": 5.0,
        "dueDate": today,
        "externalReference": ref,
        "description": "Sinal F1 proof",
    }
    r = _call(key_min, "POST", "/payments", body=body)
    emit("S03_create_pix_payment", r, has_id=bool(_json(r).get("id")))
    payment = _json(r).get("id")
    if not payment:
        return
    r = _call(key_min, "GET", f"/payments/{payment}")
    d = _json(r)
    emit(
        "S04_get_payment",
        r,
        has_status=("status" in d),
        has_value=("value" in d),
        ref_roundtrips=(d.get("externalReference") == ref),
        has_deleted=("deleted" in d),
    )
    r = _call(key_min, "GET", f"/payments/{payment}/pixQrCode")
    d = _json(r)
    emit(
        "S05_pix_qr",
        r,
        has_payload=bool(d.get("payload")),
        has_expiration=("expirationDate" in d),
    )
    r = _call(key_min, "DELETE", f"/payments/{payment}")
    emit("S06_delete_unpaid", r, deleted=bool(_json(r).get("deleted")))
    r = _call(key_min, "DELETE", f"/payments/{payment}")
    emit("S07_delete_again_idempotency", r)
    r = _call(key_min, "GET", f"/payments/{payment}")
    emit("S08_get_after_delete", r, deleted=bool(_json(r).get("deleted")))


def _paid_charge(key_min: str, key_full: str, customer: str, today: str, ref: str) -> None:
    """S09/S10: pay a QR in the sandbox (paying needs the FULL key) and re-read the charge."""
    body = {
        "customer": customer,
        "billingType": "PIX",
        "value": 5.0,
        "dueDate": today,
        "externalReference": f"{ref}-paid",
        "description": "Sinal F1 proof paid",
    }
    r = _call(key_min, "POST", "/payments", body=body)
    paid_id = _json(r).get("id")
    if not paid_id:
        return
    qr = _json(_call(key_min, "GET", f"/payments/{paid_id}/pixQrCode"))
    pay_body = {"qrCode": {"payload": qr.get("payload")}, "value": 5.0}
    r = _call(key_full, "POST", "/pix/qrCodes/pay", body=pay_body)
    emit("S09_pay_qr_sandbox", r)  # body shape: confirm against docs.asaas.com if this is 400
    r = _call(key_min, "GET", f"/payments/{paid_id}")
    d = _json(r)
    emit(
        "S10_get_paid_payment",
        r,
        status_is_received=(d.get("status") in ("RECEIVED", "CONFIRMED")),
        value_present=("value" in d),
        has_payment_date=("paymentDate" in d or "confirmedDate" in d),
    )


def _webhook_lifecycle(key_min: str) -> None:
    """S11 + S15: GET /webhooks shape, then create -> read back -> PUT -> delete."""
    r = _call(key_min, "GET", "/webhooks", params={"limit": 1})
    d = _json(r)
    items = d.get("data") if isinstance(d.get("data"), list) else []
    emit(
        "S11_webhooks_list",
        r,
        has_data_list=isinstance(d.get("data"), list),
        item_has_interrupted=bool(items and "interrupted" in items[0]),
    )
    token = uuid.uuid4().hex + uuid.uuid4().hex[:12]  # 44 chars, inside 32..255
    body = {
        "name": "f1-proof",
        "url": "https://example.invalid/webhooks/asaas/f1-proof",
        "enabled": True,
        "interrupted": False,
        "apiVersion": 3,
        "authToken": token,
        "sendType": "SEQUENTIALLY",
        "events": ["PAYMENT_RECEIVED", "PAYMENT_OVERDUE"],
    }
    r = _call(key_min, "POST", "/webhooks", body=body)
    emit("S15_webhook_create", r, has_id=bool(_json(r).get("id")))
    hook = _json(r).get("id")
    if not hook:
        return
    r = _call(key_min, "GET", "/webhooks", params={"limit": 100})
    rows = _json(r).get("data") or []
    listed = [w for w in rows if isinstance(w, dict) and w.get("id") == hook]
    enabled = bool(listed and listed[0].get("enabled"))
    emit("S15_webhook_readback", r, found=bool(listed), enabled=enabled)
    r = _call(key_min, "PUT", f"/webhooks/{hook}", body={"interrupted": False, "enabled": True})
    emit("S15_webhook_put", r)
    r = _call(key_min, "DELETE", f"/webhooks/{hook}")
    emit("S15_webhook_delete", r)


def main() -> int:
    key_full, key_scoped, skip_reason = resolve_keys(os.environ)
    if skip_reason:
        print(json.dumps({"check": "skipped_all", "reason": skip_reason}))
        return 0
    qualify = key_scoped is not None
    key_min = key_scoped or key_full  # without a scoped key the default key plays that role
    print(json.dumps({"check": "mode", "scoped_key": qualify}))
    today = date.today().isoformat()
    ref = f"f1-proof-{uuid.uuid4().hex[:12]}"

    # S01 / S02 - C4: is cpfCnpj required by POST /customers?
    base = {"name": "Paciente Teste F1", "mobilePhone": "11999999999"}
    r = _call(key_min, "POST", "/customers", body=base)
    emit("S01_customer_without_cpf", r, has_id=bool(_json(r).get("id")))
    r = _call(key_min, "POST", "/customers", body={**base, "cpfCnpj": SYNTHETIC_CPF})
    emit("S02_customer_with_cpf", r, has_id=bool(_json(r).get("id")))
    customer = _json(r).get("id")
    if not customer:
        print(json.dumps({"check": "abort", "reason": "no_customer"}))
        return 1

    _charge_lifecycle(key_min, customer, today, ref)
    _paid_charge(key_min, key_full, customer, today, ref)

    if qualify:
        # S12 - the qualification matrix: each probe with MIN and with FULL.
        for group, path in REQUIRED_PROBES + EXCESS_PROBES:
            role = "required" if (group, path) in REQUIRED_PROBES else "excess"
            for label, key in (("min", key_min), ("full", key_full)):
                r = _call(key, "GET", path, params={"limit": 1})
                slug = path.strip("/").replace("/", "_")
                emit(f"S12_probe_{label}", r, group=group, role=role, path=slug)

        # S13 - refund candidate: POST on a non-existent id, MIN vs FULL (hypothesis: 403 vs 404).
        for label, key in (("min", key_min), ("full", key_full)):
            r = _call(key, "POST", "/payments/pay_000000000000/refund", body={})
            emit(f"S13_refund_candidate_{label}", r, group="PAYMENT_REFUND", role="excess")

        # S14 - does a Cobrancas-only key accept `split` to a (fake) wallet? Nothing is
        # transferred: the wallet id does not exist, so a permission pass shows up as a wallet
        # error, not a 403.
        split_body = {
            "customer": customer,
            "billingType": "PIX",
            "value": 5.0,
            "dueDate": today,
            "externalReference": f"{ref}-split",
            "split": [{"walletId": "00000000-0000-0000-0000-000000000000", "fixedValue": 1.0}],
        }
        r = _call(key_min, "POST", "/payments", body=split_body)
        emit("S14_split_with_min_key", r, created=bool(_json(r).get("id")))
        if _json(r).get("id"):
            _call(key_min, "DELETE", f"/payments/{_json(r)['id']}")
    else:
        for skipped in ("S12_qualification", "S13_refund_candidate", "S14_split_with_min_key"):
            print(json.dumps({"check": skipped, "skipped": True, "reason": "no_scoped_key"}))

    _webhook_lifecycle(key_min)

    # S16 - erasure: can the MIN key delete a customer?
    r = _call(key_min, "DELETE", f"/customers/{customer}")
    emit("S16_customer_delete", r, deleted=bool(_json(r).get("deleted")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
