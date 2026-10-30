"""The "Checklist para ter seu sinal e evitar no-show" (TASK-071, Plan 4, task 4B).

Eight items, in a fixed order. The SYSTEM fills what it can see (the connection, the prices, the
switch, the notices, the e-mails, the proof of a real charge). Two things it cannot see through the
Asaas API key are the MANAGER's clicks, kept on the account row (`setup_checklist`):

* `pix_key_registered` - a Pix key registered at Asaas (the API key cannot list keys). It stays
  the manager's own call, but the evidence beats the click: while the last charge came back
  without a Pix code, the item is a warning even when ticked;
* `onboarding_done`    - the Asaas registration is complete, used ONLY when the API key cannot
  read the registration states (what Asaas says always wins over a click).

A click is a whitelisted key with `{"done", "at", "by"}`; a reconnection never touches them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.models.clinic_payment_account import (
    ACCOUNT_CONNECTED,
    ACCOUNT_REVOKED,
    PROVIDER_ASAAS,
    ClinicPaymentAccount,
)
from secretaria.services.payments import accounts, readiness
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.payments.onboarding import AccountOnboarding, stored_onboarding

MANUAL_ITEMS = ("pix_key_registered", "onboarding_done")

ItemState = Literal["done", "pending", "warning", "unknown"]
ItemSource = Literal["system", "user"]

# Failures that point at a missing Pix key (Asaas created the charge but gave no Pix code, or said
# the account has no key).
_PIX_KEY_REASONS = frozenset(
    {FailureReason.qr_unavailable.value, FailureReason.pix_key_missing.value}
)


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    state: ItemState
    source: ItemSource
    hint: str | None = None
    link: str | None = None
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecklistOut:
    items: list[ChecklistItem]
    badge: readiness.Badge
    done: int
    total: int
    asaas_panel_url: str


async def set_manual_item(
    session: AsyncSession, *, tenant_id: UUID, item: str, done: bool, by: str
) -> None:
    """Record the manager's click. Only `MANUAL_ITEMS` are accepted (anything else is a
    `ValueError`, nothing written). Creates the account row when the clinic has none yet - a
    `pending` row gates nothing - because the manager may do these steps before pasting the API
    key. Flushes; the caller commits. `by` is the role of the acting user (the hub token carries
    no user id)."""
    if item not in MANUAL_ITEMS:
        raise ValueError(f"not a manual checklist item: {item!r}")
    row = await accounts.get_or_create_account_row(
        session, tenant_id=tenant_id, provider=PROVIDER_ASAAS
    )
    # A new dict, never an in-place edit: SQLAlchemy does not track changes inside a JSON value.
    current = dict(row.setup_checklist or {})
    current[item] = {"done": bool(done), "at": datetime.now(UTC).isoformat(), "by": by[:32]}
    row.setup_checklist = current
    await session.flush()


def _clicked(row: ClinicPaymentAccount | None, item: str) -> bool:
    entry = ((row.setup_checklist or {}) if row is not None else {}).get(item)
    return isinstance(entry, dict) and entry.get("done") is True


def checklist_view(
    row: ClinicPaymentAccount | None,
    items: list[readiness.ReadinessItem],
    onboarding: AccountOnboarding | None,
) -> list[ChecklistItem]:
    """The eight items of one clinic from its account row, its readiness list and the Asaas
    registration states (`None` = the key cannot read them). Pure: no I/O."""
    by_code = {item.code: item for item in items}
    panel = get_settings().ASAAS_PANEL_URL
    health = accounts.charge_health(row) if row is not None else "untested"
    connected = (
        row is not None and row.status == ACCOUNT_CONNECTED and row.last_verified_at is not None
    )

    # 1. Connect Asaas
    if connected:
        connect = ChecklistItem("connect_asaas", "Conectar o Asaas", "done", "system")
    elif "account_incomplete" in by_code:
        connect = ChecklistItem(
            "connect_asaas", "Conectar o Asaas", "warning", "system",
            by_code["account_incomplete"].text,
        )
    else:
        connect = ChecklistItem(
            "connect_asaas", "Conectar o Asaas", "pending", "system",
            "Cole a chave de API do Asaas na configuração do sinal.",
        )

    # 2. Asaas registration (documents and analysis)
    label2 = "Concluir o cadastro no Asaas (documentos e análise)"
    if onboarding is not None:
        if onboarding.all_approved:
            registration = ChecklistItem("asaas_registration", label2, "done", "system", link=panel)
        else:
            registration = ChecklistItem(
                "asaas_registration", label2, "pending", "system",
                "O Asaas ainda não aprovou todas as etapas do cadastro.", panel,
            )
    elif _clicked(row, "onboarding_done"):
        registration = ChecklistItem("asaas_registration", label2, "done", "user", link=panel)
    else:
        registration = ChecklistItem(
            "asaas_registration", label2, "unknown", "user",
            "Confira no painel do Asaas se o cadastro está concluído.", panel,
        )

    # 3. Pix key (the manager's own click)
    label3 = "Cadastrar uma chave Pix no Asaas"
    pix_failure = (
        health == "failing"
        and row is not None
        and row.last_charge_failure_reason in _PIX_KEY_REASONS
    )
    if _clicked(row, "pix_key_registered"):
        if pix_failure:
            pix = ChecklistItem(
                "pix_key_registered", label3, "warning", "user",
                "Você marcou como feito, mas o Asaas ainda não devolveu o código Pix; "
                "confira a chave.", panel,
            )
        else:
            pix = ChecklistItem("pix_key_registered", label3, "done", "user", link=panel)
    else:
        hint = (
            "O Asaas não devolveu o código Pix: cadastre uma chave Pix na sua conta do Asaas."
            if pix_failure
            else "Cadastre uma chave Pix na sua conta do Asaas. Não conseguimos ver isso daqui."
        )
        pix = ChecklistItem("pix_key_registered", label3, "pending", "user", hint, panel)

    # 4. Prices
    label4 = "Colocar preço em todos os serviços"
    if "no_prices" in by_code:
        prices = ChecklistItem(
            "service_prices", label4, "pending", "system", by_code["no_prices"].text
        )
    elif "services_without_price" in by_code:
        missing = by_code["services_without_price"]
        prices = ChecklistItem(
            "service_prices", label4, "warning", "system",
            f"{missing.text} {', '.join(missing.names)}.", names=missing.names,
        )
    else:
        prices = ChecklistItem("service_prices", label4, "done", "system")

    # 5. Deposit switched on
    sinal = (
        ChecklistItem(
            "sinal_on", "Ligar o sinal e escolher a porcentagem", "pending", "system",
            by_code["sinal_off"].text,
        )
        if "sinal_off" in by_code
        else ChecklistItem("sinal_on", "Ligar o sinal e escolher a porcentagem", "done", "system")
    )

    # 6. Payment notices (webhook): "connected" means it was created and read back
    notices = ChecklistItem(
        "payment_notices",
        "Avisos de pagamento ativos",
        "done" if connected else "pending",
        "system",
        None if connected else "Os avisos de pagamento só ficam ativos com a conta conectada.",
    )

    # 7. E-mails that receive the notices
    label7 = "E-mail da clínica (e dos médicos) para receber avisos"
    no_clinic = by_code.get("no_contact_email")
    no_doctors = by_code.get("professionals_without_email")
    if no_clinic is not None or no_doctors is not None:
        parts = [item.text for item in (no_clinic, no_doctors) if item is not None]
        emails = ChecklistItem(
            "notice_emails", label7, "warning", "system", " ".join(parts),
            names=no_doctors.names if no_doctors is not None else (),
        )
    else:
        emails = ChecklistItem("notice_emails", label7, "done", "system")

    # 8. Proof: a real test charge worked
    label8 = "Testar cobrança"
    if health == "ok":
        proof = ChecklistItem("test_charge", label8, "done", "system")
    elif health == "failing":
        proof = ChecklistItem(
            "test_charge", label8, "warning", "system", by_code["last_charge_failing"].text
        )
    else:
        proof = ChecklistItem(
            "test_charge", label8, "pending", "system",
            "Nenhuma cobrança deu certo ainda. Use “Testar cobrança”.",
        )

    return [connect, registration, pix, prices, sinal, notices, emails, proof]


async def build_checklist_items(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    professional_emails: dict[str, str] | None = None,
) -> list[ChecklistItem]:
    """Read the clinic's rows and build the eight items (no call to Asaas)."""
    items = await readiness.evaluate_deposit_readiness(
        session,
        tenant_id=tenant_id,
        professional_emails=professional_emails,
        check_when_off=True,
    )
    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    onboarding = (
        stored_onboarding(row) if row is not None and row.status != ACCOUNT_REVOKED else None
    )
    return checklist_view(row, items, onboarding)


async def build_checklist(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    professional_emails: dict[str, str] | None = None,
) -> ChecklistOut:
    """The items plus the badge and the "N de 8" progress, for the Portal."""
    items = await readiness.evaluate_deposit_readiness(
        session,
        tenant_id=tenant_id,
        professional_emails=professional_emails,
        check_when_off=True,
    )
    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    onboarding = (
        stored_onboarding(row) if row is not None and row.status != ACCOUNT_REVOKED else None
    )
    view = checklist_view(row, items, onboarding)
    health = accounts.charge_health(row) if row is not None else "untested"
    return ChecklistOut(
        items=view,
        badge=readiness.badge_for(items, health),
        done=sum(1 for item in view if item.state == "done"),
        total=len(view),
        asaas_panel_url=get_settings().ASAAS_PANEL_URL,
    )
