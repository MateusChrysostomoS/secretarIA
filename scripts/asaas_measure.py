"""Measure what the REAL Asaas answers, with the clinic owner's own account (TASK-071, Plan 4).

    uv run python scripts/asaas_measure.py

Local, one-off tool. It is NOT part of the production image and it is not run in CI. It asks,
with `getpass` (nothing is echoed or stored), for the Asaas API key and for a valid CPF or CNPJ of
the owner, then walks through, deleting everything it creates:

  1. GET /myAccount/status           -> the four onboarding states (fixed words only)
  2. a test customer
  3. Pix charges of R$ 1,00 / 4,99 / 5,00 / 9,99 / 10,00, each one created, its QR code asked
     for, and voided

For each step it prints ONE JSON line with the HTTP status, a short symbolic error code
(`code_hint`) and true/false flags: whether the answer's free text mentions a candidate keyword
(value, pix key, account, document) and whether the QR answer carried the Pix code. The free text
itself, the key, the document, ids and the QR payload are never printed (the `emit` guard below
refuses any other kind of field), so the report can be pasted into the chat as it is.

What it is for: the Asaas rules that are not in its documentation (smallest accepted Pix value,
which account states still let a charge work, what a missing Pix key looks like) must be measured
before they enter the code. See `docs/ASAAS_MEDICOES.md`.
"""

import getpass
import json
import re
import sys
from datetime import date, timedelta

import httpx

PROD_BASE = "https://api.asaas.com/v3"
SANDBOX_BASE = "https://api-sandbox.asaas.com/v3"
AMOUNTS_CENTS = (100, 499, 500, 999, 1000)
ONBOARDING_FIELDS = ("general", "documentation", "commercialInfo", "bankAccountInfo")
ONBOARDING_STATES = {"PENDING", "APPROVED", "REJECTED", "AWAITING_APPROVAL"}
# Candidate keywords of the free text, tested (never printed). Accents are stripped first.
KEYWORDS = {
    "value": ("valor", "value", "minimo"),
    "pix_key": ("chave", "pix key", "pixkey"),
    "account": ("conta", "account", "aprov", "habilit"),
    "document": ("cpf", "cnpj", "document"),
}
_ALLOWED_EXTRA_TYPES = (bool, int, type(None))
_STATE_KEYS = ("state_general", "state_documents", "state_commercial", "state_bank")
_STATE_WORD = re.compile(r"[A-Z_]{1,24}")


def base_url_for(api_key: str) -> str:
    return SANDBOX_BASE if api_key.startswith("$aact_hmlg_") else PROD_BASE


def _strip_accents(text: str) -> str:
    table = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
    return text.lower().translate(table)


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


def keyword_flags(response: httpx.Response) -> dict[str, bool]:
    """For each candidate keyword, whether the error text mentions it. The text is only tested."""
    try:
        data = response.json()
    except ValueError:
        data = None
    errors = data.get("errors") if isinstance(data, dict) else None
    blob = ""
    if isinstance(errors, list):
        blob = _strip_accents(
            " ".join(
                str(item.get("description") or "") for item in errors if isinstance(item, dict)
            )
        )
    return {
        f"kw_{name}": any(word in blob for word in words) for name, words in KEYWORDS.items()
    }


def emit(step: str, response: httpx.Response | None = None, **extra: object) -> None:
    line: dict[str, object] = {"step": step}
    if response is not None:
        line["status"] = response.status_code
        line["code"] = _code(response)
        if response.status_code >= 300:
            line.update(keyword_flags(response))
    for key, value in extra.items():
        if isinstance(value, _ALLOWED_EXTRA_TYPES):
            line[key] = value
        elif key in _STATE_KEYS and isinstance(value, str) and _STATE_WORD.fullmatch(value):
            line[key] = value
        else:
            raise ValueError(f"extra field {key!r} is not allowed to carry free text")
    print(json.dumps(line), flush=True)


def onboarding_states(payload: object) -> dict[str, str]:
    """The four onboarding states of a /myAccount/status body, as fixed words only: one of the
    four known states, OTHER for any other value, NOT_READABLE when the field is absent."""
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    for field, out_key in zip(ONBOARDING_FIELDS, _STATE_KEYS, strict=True):
        value = data.get(field)
        if isinstance(value, str) and value in ONBOARDING_STATES:
            out[out_key] = value
        else:
            out[out_key] = "OTHER" if value is not None else "NOT_READABLE"
    return out


def _json_field(response: httpx.Response, name: str) -> object:
    try:
        data = response.json()
    except ValueError:
        return None
    return data.get(name) if isinstance(data, dict) else None


def run(api_key: str, document: str) -> None:
    with httpx.Client(
        base_url=base_url_for(api_key),
        headers={"access_token": api_key, "Content-Type": "application/json"},
        timeout=20.0,
    ) as client:
        # 1. onboarding states (a 403 here is a finding, not an error: the key may lack the group)
        status = client.get("/myAccount/status")
        states: dict[str, str] = {}
        if status.status_code == 200:
            try:
                states = onboarding_states(status.json())
            except ValueError:
                states = onboarding_states(None)
        emit("account_status", status, **states)

        # 2. test customer
        created = client.post(
            "/customers", json={"name": "Medicao Brain - pode apagar", "cpfCnpj": document}
        )
        emit("customer", created)
        customer_id = _json_field(created, "id") if created.status_code < 300 else None
        if not customer_id:
            return

        # 3. charges
        due = (date.today() + timedelta(days=1)).isoformat()
        try:
            for cents in AMOUNTS_CENTS:
                charge = client.post(
                    "/payments",
                    json={
                        "customer": customer_id,
                        "billingType": "PIX",
                        "value": round(cents / 100, 2),
                        "dueDate": due,
                        "description": "Medicao Brain - pode apagar",
                    },
                )
                emit(f"charge_{cents}", charge, cents=cents)
                payment_id = _json_field(charge, "id") if charge.status_code < 300 else None
                if not payment_id:
                    continue
                try:
                    qr = client.get(f"/payments/{payment_id}/pixQrCode")
                    payload = _json_field(qr, "payload") if qr.status_code == 200 else None
                    has_code = isinstance(payload, str) and bool(payload)
                    emit(f"qr_{cents}", qr, cents=cents, qr_has_pix_code=has_code)
                finally:
                    emit(
                        f"charge_{cents}_deleted",
                        client.delete(f"/payments/{payment_id}"),
                        cents=cents,
                    )
        finally:
            emit("customer_deleted", client.delete(f"/customers/{customer_id}"))


def main() -> int:
    api_key = getpass.getpass("Chave de API do Asaas (nao aparece na tela): ").strip()
    if not api_key.startswith("$aact_"):
        print("A chave nao parece uma chave do Asaas ($aact_...). Nada foi feito.")
        return 1
    document = re.sub(r"\D", "", getpass.getpass("Seu CPF ou CNPJ, so numeros (nao aparece): "))
    if len(document) not in (11, 14):
        print("CPF/CNPJ invalido. Nada foi feito.")
        return 1
    run(api_key, document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
