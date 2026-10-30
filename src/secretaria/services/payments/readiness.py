"""What stands between a clinic and a working Pix deposit (TASK-071, Plan 4, task 3).

`evaluate_deposit_readiness` reads ROWS only (the tenant, its professionals' services, the Asaas
account row): it never calls Asaas, so it is cheap enough for the settings screen. The prices are
read with the very `parse_brl_to_cents` and percentage `deposit_gate.quote_deposit` uses, so the
screen and the gate cannot disagree about what is charged.

Items are a CLOSED list of codes with a severity:

* `blocker`  - nobody can be charged until this is fixed (or it is off on purpose);
* `warning`  - something is probably wrong or incomplete, a charge may still work;
* `info`     - worth knowing, no action required.

`badge_for` turns the items plus the account's `charge_health` into the badge of the screen. It is
green ("ok") ONLY with proof - a real charge that worked - never because nothing is pending.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, Tenant
from secretaria.models.clinic_payment_account import (
    ACCOUNT_CONNECTED,
    ACCOUNT_PENDING,
    ACCOUNT_REVOKED,
    PROVIDER_ASAAS,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, failure_reason
from secretaria.services.payments.deposit_gate import timing_is_before
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.payments.onboarding import stored_onboarding
from secretaria.services.service_catalog import load_service_catalog

Severity = Literal["blocker", "warning", "info"]
Badge = Literal["ok", "failing", "untested"]

# Smallest Pix charge Asaas accepts, in cents. NOT known from the documentation: it is filled in
# only after `scripts/asaas_measure.py` measured it (docs/ASAAS_MEDICOES.md). While it is None the
# `amount_below_minimum` item never appears - the screen does not guess a rule.
MIN_PIX_CENTS: int | None = None

# Where the clinic fixes an item. The Portal maps these to a link or a section of its screen.
WHERE_ASAAS_PANEL = "asaas_panel"
WHERE_PORTAL_DEPOSIT = "portal_deposit"
WHERE_PORTAL_SERVICES = "portal_services"
WHERE_PORTAL_CONTACT = "portal_contact"

_INCOMPLETE_TEXT = {
    "key_expired_or_disabled": (
        "A chave do Asaas guardada não vale mais. Gere uma nova e reconecte."
    ),
    "excess_permissions": (
        "A chave do Asaas tem permissões demais. Gere uma nova só com as pedidas."
    ),
    "webhook_create_failed": (
        "Não conseguimos ativar os avisos de pagamento do Asaas. Tente de novo."
    ),
    "webhook_readback_failed": "Os avisos de pagamento do Asaas não estão ativos. Tente de novo.",
    "legacy_unverified": "A conexão com o Asaas ainda não foi conferida. Reconecte a conta.",
}
_INCOMPLETE_DEFAULT = "A conexão com o Asaas está incompleta. Reconecte a conta."
_MISSING_PERMISSION_TEXT = "A chave do Asaas não tem todas as permissões pedidas. Gere uma nova."


@dataclass(frozen=True)
class ReadinessItem:
    """One pending thing. `text` is plain Portuguese (no provider text, no patient data);
    `names` lists the services / professionals concerned, when there are some."""

    code: str
    severity: Severity
    text: str
    where: str | None = None
    names: tuple[str, ...] = ()


def _incomplete_text(reason: str | None) -> str:
    if reason and reason.startswith("missing_permission:"):
        return _MISSING_PERMISSION_TEXT
    return _INCOMPLETE_TEXT.get(reason or "", _INCOMPLETE_DEFAULT)


def _deposit_cents(price_text: object, percent: int) -> int | None:
    """The deposit this price would produce, in cents, or None when nothing would be charged
    (unreadable price, or a deposit of R$ 0,00). Same arithmetic as `quote_deposit`."""
    # `quote_deposit` reads `str(price)` (a JSON number is a price too), so this does as well.
    price_cents = parse_brl_to_cents(str(price_text) if price_text else None)
    if price_cents is None:
        return None
    amount = round(price_cents * percent / 100)
    return amount if amount > 0 else None


async def _active_professionals(session: AsyncSession, tenant_id: UUID) -> list[Professional]:
    return list(
        (
            await session.scalars(
                select(Professional)
                .where(Professional.tenant_id == tenant_id, Professional.is_active.is_(True))
                .order_by(Professional.name)
            )
        ).all()
    )


async def _service_prices(
    session: AsyncSession, tenant: Tenant, professionals: list[Professional]
) -> list[tuple[str, int | None]]:
    """(label, deposit cents or None) of every active service somebody can book: each active
    professional's own list (labelled "Professional — Serviço"), or the clinic's list when it
    has no professional."""
    catalog = await load_service_catalog(session, tenant.id)
    percent = tenant.pix_deposit_percent
    found: list[tuple[str, int | None]] = []
    if professionals:
        for professional in professionals:
            for entry in cfg.professional_appointment_types(professional, tenant, catalog):
                label = f"{professional.name} — {entry.get('name') or 'Consulta'}"
                found.append((label, _deposit_cents(entry.get("price"), percent)))
    else:
        for entry in cfg.active_appointment_types(tenant, catalog):
            name = entry.get("name") or "Consulta"
            found.append((name, _deposit_cents(entry.get("price"), percent)))
    return found


async def evaluate_deposit_readiness(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    professional_emails: dict[str, str] | None = None,
    check_when_off: bool = False,
) -> list[ReadinessItem]:
    """The pending items of one clinic's deposit setup.

    `professional_emails` is brain-api's `{professional_id: email}` (the caller fetches it, this
    function does no I/O of its own beyond the database); `None` means "unknown" and the doctor
    e-mail item is simply left out.

    With the deposit switched off the answer is `sinal_off` alone: a clinic that does not charge
    is not told about its Asaas account or prices. The setup checklist wants the other items
    even then (a clinic prepares everything BEFORE switching the deposit on), so it passes
    `check_when_off=True` and gets `sinal_off` first, followed by the rest."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return []
    items: list[ReadinessItem] = []
    if not (tenant.pix_deposit_enabled and timing_is_before(tenant)):
        charged_after = bool(tenant.pix_deposit_enabled)  # on, but only after the booking
        items.append(
            ReadinessItem(
                "sinal_off",
                "blocker",
                "O sinal está configurado para ser cobrado depois do agendamento. Este checklist "
                "vale para o sinal cobrado antes de confirmar a consulta."
                if charged_after
                else "O sinal está desligado: nenhum paciente será cobrado.",
                WHERE_PORTAL_DEPOSIT,
            )
        )
        if not check_when_off:
            return items

    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)

    # --- the Asaas account -------------------------------------------------------------
    if row is None or row.status in (ACCOUNT_REVOKED, ACCOUNT_PENDING):
        items.append(
            ReadinessItem(
                "no_account",
                "blocker",
                "A conta do Asaas ainda não foi conectada.",
                WHERE_PORTAL_DEPOSIT,
            )
        )
    elif row.status != ACCOUNT_CONNECTED or row.last_verified_at is None:
        items.append(
            ReadinessItem(
                "account_incomplete",
                "blocker",
                _incomplete_text(row.incomplete_reason),
                WHERE_PORTAL_DEPOSIT,
            )
        )

    # --- prices ------------------------------------------------------------------------
    professionals = await _active_professionals(session, tenant_id)
    prices = await _service_prices(session, tenant, professionals)
    if not any(cents is not None for _, cents in prices):
        items.append(
            ReadinessItem(
                "no_prices",
                "blocker",
                "Sem preço: ninguém será cobrado. Coloque o preço dos serviços.",
                WHERE_PORTAL_SERVICES,
            )
        )
    else:
        unpriced = tuple(dict.fromkeys(label for label, cents in prices if cents is None))
        if unpriced:
            items.append(
                ReadinessItem(
                    "services_without_price",
                    "warning",
                    "Estes serviços não têm preço e não cobram sinal.",
                    WHERE_PORTAL_SERVICES,
                    unpriced,
                )
            )
        if MIN_PIX_CENTS is not None:
            low = tuple(
                dict.fromkeys(
                    label for label, cents in prices if cents is not None and cents < MIN_PIX_CENTS
                )
            )
            if low:
                items.append(
                    ReadinessItem(
                        "amount_below_minimum",
                        "warning",
                        "O sinal destes serviços fica abaixo do mínimo do Asaas "
                        f"({format_brl(MIN_PIX_CENTS)}).",
                        WHERE_PORTAL_DEPOSIT,
                        low,
                    )
                )

    # --- the Asaas registration (informational: it does not block charging) -------------
    if row is not None and row.status not in (ACCOUNT_REVOKED, ACCOUNT_PENDING):
        onboarding = stored_onboarding(row)
        if onboarding is None:
            items.append(
                ReadinessItem(
                    "onboarding_unreadable",
                    "info",
                    "Confira no painel do Asaas se o cadastro está concluído.",
                    WHERE_ASAAS_PANEL,
                )
            )
        elif not onboarding.all_approved:
            items.append(
                ReadinessItem(
                    "onboarding_pending",
                    "warning",
                    "O cadastro no Asaas ainda não foi aprovado por completo.",
                    WHERE_ASAAS_PANEL,
                )
            )

    # --- proof -------------------------------------------------------------------------
    health = accounts.charge_health(row) if row is not None else "untested"
    if health == "failing":
        text = failure_reason.text_for(row.last_charge_failure_reason) or failure_reason.user_text(
            failure_reason.FailureReason.unknown
        )
        items.append(ReadinessItem("last_charge_failing", "blocker", text, WHERE_ASAAS_PANEL))
    elif health == "untested" and row is not None:
        items.append(
            ReadinessItem(
                "never_tested",
                "warning",
                "Nenhuma cobrança deu certo ainda. Use “Testar cobrança”.",
                WHERE_PORTAL_DEPOSIT,
            )
        )

    # --- who receives the failure notices ----------------------------------------------
    if not (tenant.contact_email or "").strip():
        items.append(
            ReadinessItem(
                "no_contact_email",
                "warning",
                "A clínica não tem e-mail de contato para receber os avisos do sinal.",
                WHERE_PORTAL_CONTACT,
            )
        )
    if professional_emails is not None:
        without = tuple(
            p.name for p in professionals if not (professional_emails.get(str(p.id)) or "").strip()
        )
        if without:
            items.append(
                ReadinessItem(
                    "professionals_without_email",
                    "info",
                    "Estes médicos não têm e-mail e não receberão os avisos do sinal.",
                    WHERE_PORTAL_CONTACT,
                    without,
                )
            )
    return items


def badge_for(items: list[ReadinessItem], health: str) -> Badge:
    """The badge of the screen. Green ONLY with proof (`health == "ok"`) and no blocker; a failing
    charge is red; everything else - including "nothing pending but never tried" - is grey."""
    if any(item.code == "sinal_off" for item in items):
        return "untested"
    if health == "failing":
        return "failing"
    if health == "ok" and not any(item.severity == "blocker" for item in items):
        return "ok"
    return "untested"
