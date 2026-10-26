"""Decide whether an Asaas API key may be accepted, by PROBING its permissions (decision D14).

Asaas has no endpoint that returns a key's own permissions. The only evidence is behaviour:
HTTP 200 means "this permission group is granted"; HTTP 403 with `errors[0].code =
insufficient_permission` means "it is not". So we run a closed set of read-only probes and
decide by the matrix of the spec (section 12.3A):

* the three groups the product needs (Clientes, Cobrancas, Webhooks) must all answer 200;
* every group the product must NOT hold (transfers, bills, anticipations, the statement, ...)
  must answer 403 - a 200 there means the key can do more than the clinic was told, so the key
  is REFUSED and never stored;
* anything that is not a clear 200/403 (timeouts, 429, 5xx, unreadable bodies) after a bounded
  retry is INCONCLUSIVE, and an inconclusive check NEVER accepts a key: the clinic is told to
  try again and nothing is saved.

Two guards keep a wrong probe from wrongly blocking a good clinic or wrongly accepting a bad
key. `QUALIFIED_EXCESS_GROUPS` lists the excess probes that were PROVED in the Asaas sandbox with
two real keys (a full key answers 200, a three-permission key answers 403); an unproved probe is
never executed. And unless all four MINIMUM groups (the ones that move or expose money) are
qualified, the whole suite refuses to decide - the connection stays closed until the proof is
recorded here, in code review, with its evidence, OR the operator turns on the
`ASAAS_PROBES_QUALIFIED` setting (off by default) to trust the four minimum probes.

TASK-053 put the whole excess REFUSAL behind `ASAAS_REFUSE_EXCESS_PERMISSIONS` (off by default):
off, only the three required groups decide, the four minimum excess groups are probed and a
granted one is returned as INFORMATION (`decide_informational`), and the qualification guards
above do not apply. On, everything above holds exactly as written.

What this cannot see (declared to the clinic in the guide): the READ vs READ_WRITE level of a
required group, groups with no safe probe (paying a QR code moves money, so `PIX_DEBIT` has none),
and whether `split` inside Cobrancas can route money to another wallet (NOT VERIFIED; our code
never sends `split`, and an architecture test enforces it).
"""

import asyncio
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.payments import asaas_provider
from secretaria.services.payments.asaas import ProbeResponse

logger = get_logger(__name__)

CONCURRENCY = 4
BUDGET_SECONDS = 20.0
_RETRY_WAITS = (0.5, 1.5)  # two retries per probe
_RETRY_AFTER_CAP = 5.0


class ProbeKind(str, enum.Enum):  # noqa: UP042 - StrEnum would change str(member)
    GRANTED = "granted"  # 200 with the expected body: the group IS granted
    DENIED = "denied"  # 403 insufficient_permission: the group is NOT granted
    KEY_INVALID = "key_invalid"  # 401
    FORBIDDEN_OTHER = "forbidden_other"  # 403 for another reason (IP allowlist, ...)
    RATE_LIMITED = "rate_limited"  # 429
    TRANSIENT = "transient"  # 5xx, timeout, DNS, TLS, refused connection
    UNEXPECTED = "unexpected"  # 400/404/422, or a 200 whose body was not readable


class VerdictKind(str, enum.Enum):  # noqa: UP042
    ACCEPTED = "accepted"
    REFUSED = "refused"
    INCONCLUSIVE = "inconclusive"


CODE_KEY_INVALID = "key_invalid"
CODE_KEY_RESTRICTED = "key_restricted"
CODE_MISSING = "missing_permission"
CODE_EXCESS = "key_excess_permissions"
CODE_MIXED = "key_permissions_mixed"
CODE_UNAVAILABLE = "verification_unavailable"
CODE_NOT_QUALIFIED = "probes_not_qualified"


@dataclass(frozen=True)
class ProbeSpec:
    group: str  # Asaas permission-group code
    path: str  # v3 path, without the base URL
    role: str  # "required" | "excess"
    method: str = "GET"
    expect_list: bool = True  # the 200 body must be a list envelope ({"data": [...]})
    # A candidate probe (the refund group) reads a 404 on a non-existent id as "the permission
    # check passed": it may only ever be qualified after the sandbox proof shows 403 vs 404.
    granted_statuses: frozenset = frozenset({200})


REQUIRED_PROBES: tuple[ProbeSpec, ...] = (
    ProbeSpec("CUSTOMER", "/customers", "required"),
    ProbeSpec("PAYMENT", "/payments", "required"),
    ProbeSpec("WEBHOOK", "/webhooks", "required"),
)

EXCESS_PROBES: tuple[ProbeSpec, ...] = (
    # --- the four MINIMUM groups: money leaving, or the account's financial exposure ---
    ProbeSpec("TRANSFER", "/transfers", "excess"),
    ProbeSpec("BILL", "/bill", "excess"),
    ProbeSpec("ANTICIPATION", "/anticipations", "excess"),
    ProbeSpec("FINANCIAL_TRANSACTION", "/financialTransactions", "excess"),
    # --- second wave: qualified one by one, only when the sandbox proof passes ---
    ProbeSpec("PIX_TRANSACTION", "/pix/transactions", "excess"),
    ProbeSpec("PIX_ADDRESS_KEY", "/pix/addressKeys", "excess"),
    ProbeSpec("SUBSCRIPTION", "/subscriptions", "excess"),
    ProbeSpec("PAYMENT_LINK", "/paymentLinks", "excess"),
    ProbeSpec("INSTALLMENT", "/installments", "excess"),
    ProbeSpec("CHARGEBACK", "/chargebacks", "excess"),
    ProbeSpec("SUB_ACCOUNT", "/accounts", "excess"),
    ProbeSpec("MOBILE_PHONE_RECHARGE", "/mobilePhoneRecharges", "excess"),
    ProbeSpec("ACCOUNT_INFO", "/myAccount/commercialInfo", "excess", expect_list=False),
    # --- candidate: refund permission. A POST on a NON-EXISTENT id that only reads the error
    # code (403 without the permission, 404 with it - NOT VERIFIED). Off unless proved. ---
    ProbeSpec(
        "PAYMENT_REFUND",
        "/payments/pay_000000000000/refund",
        "excess",
        method="POST",
        expect_list=False,
        granted_statuses=frozenset({404}),
    ),
)

# The groups whose permission moves or exposes money: F1 does not close, and no key is ever
# accepted, unless ALL FOUR are qualified.
MINIMUM_EXCESS_GROUPS = frozenset({"TRANSFER", "BILL", "ANTICIPATION", "FINANCIAL_TRANSACTION"})

# Excess probes PROVED in the Asaas sandbox (Task 3 -> Task 22): a key with every permission
# answered 200 and a key with only Clientes+Cobrancas+Webhooks answered 403
# insufficient_permission. EMPTY until that proof is recorded, which keeps the connection
# fail-closed (`probes_not_qualified`).
QUALIFIED_EXCESS_GROUPS: frozenset = frozenset()


def effective_qualified_groups() -> frozenset:
    """The excess groups the suite may probe right now: the recorded proof, plus the four minimum
    groups when the operator turned on `ASAAS_PROBES_QUALIFIED` (default OFF = fail-closed)."""
    if get_settings().ASAAS_PROBES_QUALIFIED:
        return QUALIFIED_EXCESS_GROUPS | MINIMUM_EXCESS_GROUPS
    return QUALIFIED_EXCESS_GROUPS


# pt-BR labels for the clinic-facing messages (the front renders M2-M4 from code + labels).
GROUP_LABELS: dict[str, str] = {
    "CUSTOMER": "Clientes",
    "PAYMENT": "Cobranças",
    "WEBHOOK": "Webhooks",
    "TRANSFER": "Transferências",
    "BILL": "Pagamento de contas",
    "ANTICIPATION": "Antecipações",
    "FINANCIAL_TRANSACTION": "Extrato e informações financeiras",
    "PIX_TRANSACTION": "Transações Pix",
    "PIX_ADDRESS_KEY": "Gerenciamento de chaves Pix",
    "SUBSCRIPTION": "Assinaturas",
    "PAYMENT_LINK": "Link de pagamento",
    "INSTALLMENT": "Parcelamentos",
    "CHARGEBACK": "Chargeback",
    "SUB_ACCOUNT": "Subcontas",
    "MOBILE_PHONE_RECHARGE": "Recarga de celular",
    "ACCOUNT_INFO": "Informações da conta",
    "PAYMENT_REFUND": "Estornos de cobranças",
}

_CAPABILITY_OF = {"CUSTOMER": "customers", "PAYMENT": "payments", "WEBHOOK": "webhooks"}


@dataclass(frozen=True)
class Verdict:
    """The decision. Never carries the key or any provider content - only group NAMES."""

    kind: VerdictKind
    code: str | None = None
    missing_groups: tuple[str, ...] = ()
    excess_groups: tuple[str, ...] = ()
    capabilities: dict = field(default_factory=dict)


def classify(
    response: ProbeResponse, *, granted_statuses: frozenset = frozenset({200})
) -> ProbeKind:
    status = response.status_code
    if status in granted_statuses:
        # A 200 must carry the expected JSON envelope; a candidate's 404 has no body to read.
        return ProbeKind.GRANTED if status != 200 or response.json_ok else ProbeKind.UNEXPECTED
    if status == 401:
        return ProbeKind.KEY_INVALID
    if status == 403:
        if response.code_hint == "insufficient_permission":
            return ProbeKind.DENIED
        return ProbeKind.FORBIDDEN_OTHER
    if status == 429:
        return ProbeKind.RATE_LIMITED
    if status == 0 or status >= 500:
        return ProbeKind.TRANSIENT
    return ProbeKind.UNEXPECTED


def decide(required: dict[str, ProbeKind], excess: dict[str, ProbeKind]) -> Verdict:
    """The verdict matrix (spec 12.3A), in precedence order: definitive refusals first, even
    when other probes were inconclusive; ACCEPTED only when everything is clear."""
    every = [*required.values(), *excess.values()]
    if ProbeKind.KEY_INVALID in every:
        return Verdict(VerdictKind.REFUSED, CODE_KEY_INVALID)
    if ProbeKind.FORBIDDEN_OTHER in every:
        return Verdict(VerdictKind.REFUSED, CODE_KEY_RESTRICTED)

    missing = tuple(sorted(g for g, kind in required.items() if kind is ProbeKind.DENIED))
    extra = tuple(sorted(g for g, kind in excess.items() if kind is ProbeKind.GRANTED))
    if missing and extra:
        return Verdict(VerdictKind.REFUSED, CODE_MIXED, missing, extra)
    if missing:
        return Verdict(VerdictKind.REFUSED, CODE_MISSING, missing, ())
    if extra:
        return Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), extra)

    clear = all(kind is ProbeKind.GRANTED for kind in required.values()) and all(
        kind is ProbeKind.DENIED for kind in excess.values()
    )
    if not clear:
        return Verdict(VerdictKind.INCONCLUSIVE, CODE_UNAVAILABLE)
    capabilities = {_CAPABILITY_OF[group]: True for group in required if group in _CAPABILITY_OF}
    return Verdict(VerdictKind.ACCEPTED, None, (), (), capabilities)


def decide_informational(required: dict[str, ProbeKind], excess: dict[str, ProbeKind]) -> Verdict:
    """The verdict while the excess refusal is OFF (TASK-053, spec 2026-10-11 section 7).

    Only the REQUIRED groups decide; a granted excess group is REPORTED in `excess_groups`, never
    refused, and an excess probe that was denied, inconclusive or forbidden is ignored (it is
    information, not a gate). ACCEPTED still needs every required group clearly granted.
    """
    kinds = list(required.values())
    if ProbeKind.KEY_INVALID in kinds:
        return Verdict(VerdictKind.REFUSED, CODE_KEY_INVALID)
    if ProbeKind.FORBIDDEN_OTHER in kinds:
        return Verdict(VerdictKind.REFUSED, CODE_KEY_RESTRICTED)
    missing = tuple(sorted(g for g, kind in required.items() if kind is ProbeKind.DENIED))
    if missing:
        return Verdict(VerdictKind.REFUSED, CODE_MISSING, missing, ())
    if not all(kind is ProbeKind.GRANTED for kind in kinds):
        return Verdict(VerdictKind.INCONCLUSIVE, CODE_UNAVAILABLE)
    extra = tuple(sorted(g for g, kind in excess.items() if kind is ProbeKind.GRANTED))
    capabilities = {_CAPABILITY_OF[group]: True for group in required if group in _CAPABILITY_OF}
    return Verdict(VerdictKind.ACCEPTED, None, (), extra, capabilities)


async def _probe_with_retries(
    client, spec: ProbeSpec, sleep: Callable[[float], Awaitable[None]]
) -> ProbeKind:
    kind = ProbeKind.TRANSIENT
    for attempt in range(len(_RETRY_WAITS) + 1):
        response = await client.probe(spec.path, method=spec.method, expect_list=spec.expect_list)
        kind = classify(response, granted_statuses=spec.granted_statuses)
        retryable = kind in (ProbeKind.RATE_LIMITED, ProbeKind.TRANSIENT)
        if not retryable or attempt == len(_RETRY_WAITS):
            return kind
        wait = _RETRY_WAITS[attempt]
        if response.retry_after:
            wait = min(response.retry_after, _RETRY_AFTER_CAP)
        await sleep(wait)
    return kind


async def run_probe_suite(
    api_key: str,
    *,
    client=None,
    qualified: frozenset | None = None,
    refuse_excess: bool | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    budget_seconds: float = BUDGET_SECONDS,
) -> Verdict:
    """Probe the key and decide. Runs at most 4 probes at once inside a ~20 s budget. The key
    goes only to the client factory; the returned verdict never contains it.

    `refuse_excess` (default: the `ASAAS_REFUSE_EXCESS_PERMISSIONS` setting) picks the rule:
    ON = decision D14 (excess refused; the suite refuses to decide unless the four minimum groups
    are qualified); OFF = `decide_informational` (the four minimum excess groups are probed and
    reported, never refused; qualification plays no part)."""
    refuse = (
        get_settings().ASAAS_REFUSE_EXCESS_PERMISSIONS if refuse_excess is None else refuse_excess
    )
    if refuse:
        qualified = effective_qualified_groups() if qualified is None else qualified
        if not MINIMUM_EXCESS_GROUPS <= qualified:
            unproved = len(MINIMUM_EXCESS_GROUPS - qualified)
            logger.warning("asaas_key_probe_not_qualified", unproved_groups=unproved)
            return Verdict(VerdictKind.INCONCLUSIVE, CODE_NOT_QUALIFIED)
        excess_groups = qualified
    else:
        excess_groups = MINIMUM_EXCESS_GROUPS  # information only: read-only list GETs

    client = client or asaas_provider.client_for(api_key)
    specs = [*REQUIRED_PROBES, *(p for p in EXCESS_PROBES if p.group in excess_groups)]
    results: dict[str, ProbeKind] = {}
    gate = asyncio.Semaphore(CONCURRENCY)

    async def _one(spec: ProbeSpec) -> None:
        async with gate:
            results[spec.group] = await _probe_with_retries(client, spec, sleep)

    try:
        async with asyncio.timeout(budget_seconds):
            await asyncio.gather(*(_one(spec) for spec in specs))
    except TimeoutError:
        logger.warning("asaas_key_probe_budget_exceeded")
    for spec in specs:
        results.setdefault(spec.group, ProbeKind.TRANSIENT)  # never finished: not a "denied"

    required = {s.group: results[s.group] for s in specs if s.role == "required"}
    excess = {s.group: results[s.group] for s in specs if s.role == "excess"}
    return decide(required, excess) if refuse else decide_informational(required, excess)
