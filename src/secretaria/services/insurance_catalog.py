"""The convênio catalog: seed, matching, per-clinic and per-doctor selection.

Tables and the product decisions behind them: models/insurance.py. Contract the
hub consumes: docs/CHECKPOINT_convenio_catalogo.md.

Everything that decides "which catalog entry is this text?" goes through
`match_plan` here - the legacy-string migration, the hub's legacy sync, the
booking flow's typed answer and the appointment's plan ids - so the four can
never disagree about what "unimed " means.

TASK-008 adds three ideas on top of TASK-006's global-catalog-plus-per-doctor
design:

- `Tenant.insurance_mode` (shared / clinic_with_exceptions / independent) -
  changes what "the plans a doctor can pick from" and "who a plan applies to"
  mean; see `load_tenant_insurance` and `set_professional_clinic_plans` /
  `set_professional_independent_plans` below.
- An admin-extensible catalog (`create_catalog_entry`) with automatic
  reconciliation of legacy free text that used to have no match
  (`reconcile_unmatched_insurances`).
- Convênio "Outro": a clinic or (in `independent` mode) a doctor can register a
  plan outside the catalog (`create_tenant_custom_plan` /
  `create_professional_custom_plan`).

PII: a patient's chosen plan identifies their coverage. Nothing in this module
logs a plan name next to a patient/conversation; counts and ids only.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import truncate_list_row_title
from secretaria.models.insurance import (
    InsuranceCatalog,
    InsuranceCatalogUnmatched,
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.professional import Professional
from secretaria.models.tenant import Tenant
from secretaria.services.service_catalog import normalize

logger = get_logger(__name__)

# uuid5 namespace for the seeded rows: every environment (prod, a disposable
# Postgres, the SQLite test DB) gives "unimed" the SAME id, so a fixture, a doc
# example and production can name a plan by id without a lookup. The migration
# (migrations/versions/c9f4e2a7b815_insurance_catalog.py) freezes its own copy
# of the seed and computes ids with this same namespace.
CATALOG_NAMESPACE = uuid.UUID("6c1f0e3a-8d2b-4f5e-9a7c-2b3d4e5f6a70")


def catalog_id_for_slug(slug: str) -> UUID:
    return uuid.uuid5(CATALOG_NAMESPACE, slug)


# The seed. Mechanism/note provenance (URL + access date per row) is recorded
# in docs/CHECKPOINT_convenio_catalogo.md - this is metadata about how each
# operator TYPICALLY relates to paying a consultation, never a price and never
# a rule the product enforces. Keep in step with the migration's frozen copy.
CATALOG_SEED: tuple[dict, ...] = (
    {
        "slug": "alice",
        "name": "Alice",
        "mechanism": "variavel_por_plano",
        "note": (
            "Modelo coordenado/verticalizado; sem reembolso em contratos pequenos, "
            "reembolso só em linhas específicas para empresas maiores."
        ),
    },
    {
        "slug": "amil",
        "name": "Amil",
        "mechanism": "variavel_por_plano",
        "note": (
            "Reembolso (livre escolha) só nas linhas Ouro/Black; planos de entrada usam só "
            "rede credenciada/própria."
        ),
    },
    {
        "slug": "assim-saude",
        "name": "Assim Saúde",
        "mechanism": "variavel_por_plano",
        "note": (
            "Rede credenciada própria (RJ) é o padrão; reembolso por livre escolha só no "
            "plano Assim Saúde Exclusivo."
        ),
    },
    {
        "slug": "bradesco-saude",
        "name": "Bradesco Saúde",
        "mechanism": "variavel_por_plano",
        "note": (
            "Rede referenciada; reembolso pela tabela THSM (CRS x múltiplo contratado), "
            "varia por linha de produto."
        ),
    },
    {
        "slug": "care-plus",
        "name": "Care Plus",
        "mechanism": "variavel_por_plano",
        "note": (
            "Premium/executivo; reembolso para consultas de livre escolha, e também rede "
            "credenciada sem desembolso."
        ),
    },
    {
        "slug": "cassi",
        "name": "CASSI",
        "mechanism": "variavel_por_plano",
        "note": (
            "Autogestão; rede referenciada e livre escolha com reembolso limitado à Tabela "
            "Geral de Auxílios, conforme o plano."
        ),
    },
    {
        "slug": "golden-cross",
        "name": "Golden Cross",
        "mechanism": "variavel_por_plano",
        "note": (
            "Uso típico é rede credenciada; reembolso fora da rede com percentual/valor por "
            "categoria de plano."
        ),
    },
    {
        "slug": "hapvida",
        "name": "Hapvida",
        "mechanism": "variavel_por_plano",
        "note": (
            "Verticalizado (grupo Hapvida NotreDame Intermédica); rede própria, "
            "coparticipação em parte dos planos, reembolso raro."
        ),
    },
    {
        "slug": "notredame-intermedica",
        "name": "NotreDame Intermédica",
        "mechanism": "variavel_por_plano",
        "note": (
            "Verticalizado (grupo Hapvida NotreDame Intermédica); coparticipação em parte "
            "dos planos, reembolso só na linha Advance."
        ),
    },
    {
        "slug": "omint",
        "name": "Omint",
        "mechanism": "variavel_por_plano",
        "note": (
            "Rede credenciada disponível; reembolso elevado por livre escolha é o "
            "diferencial da linha Premium, varia por linha."
        ),
    },
    {
        "slug": "porto-seguro-saude",
        "name": "Porto Seguro Saúde",
        "mechanism": "variavel_por_plano",
        "note": (
            "Linha Porto Saúde é de livre escolha com reembolso pela tabela própria, "
            "variando pela categoria contratada."
        ),
    },
    {
        "slug": "prevent-senior",
        "name": "Prevent Senior",
        "mechanism": "desconhecido",
        "note": (
            "Modelo verticalizado (rede própria); fontes públicas conflitam sobre reembolso "
            "de consulta, sem confirmação oficial."
        ),
    },
    {
        "slug": "sulamerica",
        "name": "SulAmérica",
        "mechanism": "variavel_por_plano",
        "note": (
            "Rede referenciada com reembolso na maioria das linhas; a linha Direto não "
            "reembolsa consulta eletiva."
        ),
    },
    {
        "slug": "unimed",
        "name": "Unimed",
        "mechanism": "variavel_por_plano",
        "note": (
            "Cooperativa; coparticipação comum e reembolso por livre escolha limitado, "
            "dependendo do produto/cooperativa."
        ),
    },
)


class NotClinicPlans(ValueError):
    """A doctor (clinic_with_exceptions) was asked to accept plans the clinic
    has not enabled."""

    def __init__(self, catalog_ids: list[str]) -> None:
        super().__init__(", ".join(catalog_ids))
        self.catalog_ids = catalog_ids


class UnknownCatalogIds(ValueError):
    """A clinic/doctor was asked to enable ids that are not in the (active) catalog."""

    def __init__(self, catalog_ids: list[str]) -> None:
        super().__init__(", ".join(catalog_ids))
        self.catalog_ids = catalog_ids


class PlanNotFound(ValueError):
    """A `tenant_insurance_plans` id named in a request does not belong to this tenant."""


class DuplicateCatalogEntry(ValueError):
    """An admin tried to create a catalog entry whose (normalized) name already exists."""


@dataclass(frozen=True)
class TenantInsurance:
    """What the pure flow router needs about one clinic's convênios.

    `plans` keeps catalog order (by name) as plain dicts, `{"id": str, "name":
    str, "note": str | None}`, so it travels on the tenant snapshot like
    `appointment_types` does. `id` is the row's own identity - a
    `tenant_insurance_plans.id` (shared/clinic_with_exceptions modes) or a
    `professional_insurance_plans.id`/catalog id (independent mode, see
    `_load_independent_insurance`) - never a bare catalog id, because a custom
    "Outro" plan has none. `note` is `custom_payment_note` for a custom plan,
    else None (catalog metadata is never patient-facing prose).
    `accepted_by` maps professional id (str) -> the plan ids (str, same `id`
    space as `plans`) that doctor accepts; a doctor with no rows maps to
    nothing, which the flow renders as "not marked" - never as hidden.
    """

    plans: list[dict] = field(default_factory=list)
    accepted_by: dict[str, frozenset[str]] = field(default_factory=dict)


def catalog_match_keys(entry: InsuranceCatalog) -> set[str]:
    """Every normalized spelling that should match this catalog entry.

    The canonical name plus every alias (TASK-008's extensible catalog) - an
    admin registering "GEAP Saúde" as an alias of "GEAP" makes a clinic's old
    free-text "GEAP Saúde" resolve without a second catalog row.
    """
    keys = {normalize(entry.name)}
    keys.update(normalize(str(alias)) for alias in (entry.aliases or []))
    return {key for key in keys if key}


def match_plan(plans: Iterable[dict], text: str | None) -> dict | None:
    """The plan whose name IS `text` (normalized), or its 24-char row title.

    Exact on the normalized form only - "Unimed BH" is not "Unimed". A fuzzy
    match here would decide a patient's coverage by guess; an unmatched answer
    is kept as free text instead (the flow's "Outro convênio" path).
    """
    target = normalize(text)
    if not target:
        return None
    for plan in plans:
        name = str(plan.get("name") or "")
        if normalize(name) == target or normalize(truncate_list_row_title(name)) == target:
            return plan
    return None


async def ensure_catalog_seeded(session: AsyncSession) -> None:
    """Insert any seed row missing from `insurance_catalog`. Idempotent.

    Production gets the rows from the migration; this exists for the SQLite
    test database (built with `create_all`, no migrations) and local dev.
    """
    existing = set(await session.scalars(select(InsuranceCatalog.slug)))
    for entry in CATALOG_SEED:
        if entry["slug"] in existing:
            continue
        session.add(
            InsuranceCatalog(
                id=catalog_id_for_slug(entry["slug"]),
                slug=entry["slug"],
                name=entry["name"],
                normalized_name=normalize(entry["name"]),
                mechanism=entry["mechanism"],
                note=entry["note"],
            )
        )
    await session.flush()


async def list_catalog(
    session: AsyncSession, *, active_only: bool = True
) -> list[InsuranceCatalog]:
    stmt = select(InsuranceCatalog).order_by(InsuranceCatalog.name)
    if active_only:
        stmt = stmt.where(InsuranceCatalog.is_active.is_(True))
    return list(await session.scalars(stmt))


def _plan_display_name(plan: TenantInsurancePlan, entry: InsuranceCatalog | None) -> str:
    return entry.name if entry is not None else str(plan.custom_name or "")


async def list_tenant_plans(
    session: AsyncSession, tenant_id: UUID
) -> list[tuple[TenantInsurancePlan, InsuranceCatalog | None]]:
    """The clinic's enabled plans (catalog-backed AND custom), by display name.

    LEFT JOIN because a custom "Outro" row (TASK-008) has no catalog entry.
    """
    rows = await session.execute(
        select(TenantInsurancePlan, InsuranceCatalog)
        .outerjoin(InsuranceCatalog, InsuranceCatalog.id == TenantInsurancePlan.catalog_id)
        .where(TenantInsurancePlan.tenant_id == tenant_id)
    )
    pairs = [(plan, entry) for plan, entry in rows.all()]
    pairs.sort(key=lambda pair: normalize(_plan_display_name(*pair)))
    return pairs


async def list_professional_plans(
    session: AsyncSession, professional_id: UUID
) -> list[tuple[ProfessionalInsurancePlan, InsuranceCatalog | None]]:
    """One doctor's OWN rows (independent mode: catalog picks + their "Outro").

    `clinic_with_exceptions` rows (`tenant_plan_id` set) are not returned here
    - use `professional_accepted_tenant_plan_ids` for those, since their
    display name/note live on `tenant_insurance_plans`, not here.
    """
    rows = await session.execute(
        select(ProfessionalInsurancePlan, InsuranceCatalog)
        .outerjoin(InsuranceCatalog, InsuranceCatalog.id == ProfessionalInsurancePlan.catalog_id)
        .where(
            ProfessionalInsurancePlan.professional_id == professional_id,
            ProfessionalInsurancePlan.tenant_plan_id.is_(None),
        )
    )
    pairs = [(plan, entry) for plan, entry in rows.all()]
    pairs.sort(key=lambda pair: normalize(pair[1].name if pair[1] else pair[0].custom_name or ""))
    return pairs


async def professional_accepted_tenant_plan_ids(
    session: AsyncSession, professional_id: UUID
) -> list[str]:
    """`clinic_with_exceptions`: which of the CLINIC's own plan ids this doctor accepts.

    Empty list is ambiguous on its own - see `Professional.insurance_plans_customized`
    for "never customized" (inherit all) vs "explicitly accepts none".
    """
    rows = await session.scalars(
        select(ProfessionalInsurancePlan.tenant_plan_id).where(
            ProfessionalInsurancePlan.professional_id == professional_id,
            ProfessionalInsurancePlan.tenant_plan_id.is_not(None),
        )
    )
    return sorted(str(plan_id) for plan_id in rows)


async def professional_plan_ids(session: AsyncSession, professional_id: UUID) -> list[str]:
    """Back-compat alias of `professional_accepted_tenant_plan_ids` (pre-TASK-008 name)."""
    return await professional_accepted_tenant_plan_ids(session, professional_id)


def _independent_plan_id(catalog_id: UUID | None, row_id: UUID) -> str:
    return str(catalog_id) if catalog_id is not None else str(row_id)


async def _load_independent_insurance(session: AsyncSession, tenant_id: UUID) -> TenantInsurance:
    """`independent` mode: the union of every active doctor's OWN plans.

    No clinic-level list is consulted at all (SPEC §4.2: "união dos
    professional_insurance_plans de todos os profissionais ativos"). A catalog
    pick is deduplicated across doctors by catalog id (two doctors who both
    take "Unimed" show ONE row); a doctor's own "Outro" is never deduplicated
    with anyone else's - it is identified by that row's own id.
    """
    rows = await session.execute(
        select(ProfessionalInsurancePlan, InsuranceCatalog)
        .join(Professional, Professional.id == ProfessionalInsurancePlan.professional_id)
        .outerjoin(InsuranceCatalog, InsuranceCatalog.id == ProfessionalInsurancePlan.catalog_id)
        .where(
            ProfessionalInsurancePlan.tenant_id == tenant_id,
            ProfessionalInsurancePlan.tenant_plan_id.is_(None),
            Professional.is_active.is_(True),
        )
    )
    plans_by_id: dict[str, dict] = {}
    accepted: dict[str, set[str]] = {}
    for plan, entry in rows.all():
        plan_id = _independent_plan_id(plan.catalog_id, plan.id)
        name = entry.name if entry is not None else str(plan.custom_name or "")
        if plan_id not in plans_by_id and name.strip():
            plans_by_id[plan_id] = {
                "id": plan_id,
                "name": name,
                "note": plan.custom_payment_note if entry is None else None,
            }
        accepted.setdefault(str(plan.professional_id), set()).add(plan_id)
    plans = sorted(plans_by_id.values(), key=lambda item: normalize(item["name"]))
    return TenantInsurance(
        plans=plans, accepted_by={key: frozenset(value) for key, value in accepted.items()}
    )


async def load_tenant_insurance(session: AsyncSession, tenant_id: UUID) -> TenantInsurance:
    """ONE read per turn of everything the flow needs (see TenantInsurance).

    Mode-aware (TASK-008): `independent` sources plans from every active
    doctor's own rows (`_load_independent_insurance`); `shared` and
    `clinic_with_exceptions` (and a tenant with no mode chosen yet, or a bare
    ORM row from a caller that predates the mode column) keep TASK-006's
    behaviour - the clinic's own `tenant_insurance_plans`, plus, as a bridging
    fallback, legacy `Tenant.insurances` strings that match nothing in the
    catalog (`_legacy_only_names`), so a clinic with "GEAP"/"Cabesp" never
    silently loses the convênio question.

    `accepted_by` (shared/clinic_with_exceptions branch) maps a professional id
    to their `tenant_insurance_plans` id set ONLY when
    `Professional.insurance_plans_customized` is true - a professional absent
    from the map has never customized, which `flow_router._accepts_plan`
    interprets per-mode (inherit all in clinic_with_exceptions, meaningless in
    shared, where the mode marks everyone regardless).
    """
    mode = await session.scalar(select(Tenant.insurance_mode).where(Tenant.id == tenant_id))
    if mode == "independent":
        return await _load_independent_insurance(session, tenant_id)

    plans = [
        {
            "id": str(plan.id),
            "name": _plan_display_name(plan, entry),
            "note": plan.custom_payment_note if entry is None else None,
        }
        for plan, entry in await list_tenant_plans(session, tenant_id)
    ]
    plans.extend(
        {"id": None, "name": name, "note": None}
        for name in await _legacy_only_names(session, tenant_id)
    )

    customized_ids = {
        str(pid)
        for pid in await session.scalars(
            select(Professional.id).where(
                Professional.tenant_id == tenant_id,
                Professional.insurance_plans_customized.is_(True),
            )
        )
    }
    accepted: dict[str, set[str]] = {pid: set() for pid in customized_ids}
    rows = await session.execute(
        select(
            ProfessionalInsurancePlan.professional_id, ProfessionalInsurancePlan.tenant_plan_id
        ).where(
            ProfessionalInsurancePlan.tenant_id == tenant_id,
            ProfessionalInsurancePlan.tenant_plan_id.is_not(None),
        )
    )
    for professional_id, tenant_plan_id in rows.all():
        accepted.setdefault(str(professional_id), set()).add(str(tenant_plan_id))
    return TenantInsurance(
        plans=plans,
        accepted_by={key: frozenset(value) for key, value in accepted.items()},
    )


async def resolve_booking_plan_ids(
    session: AsyncSession,
    tenant_id: UUID,
    insurance: str | None,
    professional_id: UUID | None = None,
) -> tuple[UUID | None, UUID | None]:
    """The (tenant_plan_id, professional_plan_id) pair `appointments` records.

    Applied once, when an appointment row is created. Exactly one of the two
    is non-None when `insurance` names a real plan; both None for
    "Particular", a typed "Outro convênio", or an unmatched answer.

    Mode-aware: `independent` matches against the BOOKING PROFESSIONAL's own
    rows only (there is no clinic list to match against); every other mode
    (including no mode chosen, for a booking made before the step existed)
    matches against the clinic's own plans, exactly as `resolve_tenant_plan_id`
    did pre-TASK-008.
    """
    if not insurance or not insurance.strip():
        return None, None
    mode = await session.scalar(select(Tenant.insurance_mode).where(Tenant.id == tenant_id))
    if mode == "independent" and professional_id is not None:
        candidates = [
            {"id": plan.id, "name": entry.name if entry else plan.custom_name}
            for plan, entry in await list_professional_plans(session, professional_id)
        ]
        matched = match_plan(candidates, insurance)
        return None, (matched["id"] if matched is not None else None)

    candidates = [
        {"id": plan.id, "name": _plan_display_name(plan, entry)}
        for plan, entry in await list_tenant_plans(session, tenant_id)
    ]
    matched = match_plan(candidates, insurance)
    return (matched["id"] if matched is not None else None), None


async def resolve_tenant_plan_id(
    session: AsyncSession, tenant_id: UUID, insurance: str | None
) -> UUID | None:
    """Back-compat wrapper of `resolve_booking_plan_ids` (pre-TASK-008 name/shape).

    Ignores `independent` mode's per-doctor resolution (no professional id to
    give it) - callers that can supply one should call
    `resolve_booking_plan_ids` directly instead.
    """
    tenant_plan_id, _professional_plan_id = await resolve_booking_plan_ids(
        session, tenant_id, insurance
    )
    return tenant_plan_id


async def _legacy_only_names(session: AsyncSession, tenant_id: UUID) -> list[str]:
    """The clinic's legacy strings that match no catalog entry (deduplicated)."""
    legacy = await session.scalar(select(Tenant.insurances).where(Tenant.id == tenant_id))
    if not legacy:
        return []
    catalog_entries = await list_catalog(session, active_only=False)
    known: set[str] = set()
    for entry in catalog_entries:
        known.update(catalog_match_keys(entry))
    seen: set[str] = set()
    names: list[str] = []
    for value in legacy:
        name = str(value).strip()
        key = normalize(name)
        if not key or key in known or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _mirror_legacy(tenant: Tenant, names: Sequence[str], catalog_names: Iterable[str]) -> None:
    """Keep `Tenant.insurances` readable by a hub frontend that predates the catalog.

    Canonical names of the enabled plans, then every legacy string that matches
    NO catalog entry at all, untouched - those are the operator's to reclassify
    (docs/CHECKPOINT_convenio_catalogo.md) and must not vanish on a save. A
    string naming a catalog plan the clinic just REMOVED is dropped, or it
    would come back as a legacy-only option (`load_tenant_insurance`).
    """
    known = {normalize(name) for name in catalog_names}
    preserved = [
        str(value)
        for value in (tenant.insurances or [])
        if str(value).strip() and normalize(str(value)) not in known
    ]
    tenant.insurances = list(names) + [value for value in preserved if value not in names]


async def _drop_tenant_plans(
    session: AsyncSession, tenant_id: UUID, catalog_ids: Iterable[UUID]
) -> None:
    ids = list(catalog_ids)
    if not ids:
        return
    # Explicit, not only the FK's CASCADE: SQLite (the test DB) does not
    # enforce foreign keys, and the subset rule must hold there too.
    tenant_plan_ids = list(
        await session.scalars(
            select(TenantInsurancePlan.id).where(
                TenantInsurancePlan.tenant_id == tenant_id,
                TenantInsurancePlan.catalog_id.in_(ids),
            )
        )
    )
    if tenant_plan_ids:
        await session.execute(
            delete(ProfessionalInsurancePlan).where(
                ProfessionalInsurancePlan.tenant_plan_id.in_(tenant_plan_ids)
            )
        )
    await session.execute(
        delete(TenantInsurancePlan).where(
            TenantInsurancePlan.tenant_id == tenant_id,
            TenantInsurancePlan.catalog_id.in_(ids),
        )
    )


async def set_tenant_plans(
    session: AsyncSession, tenant: Tenant, plans: Sequence[tuple[UUID, bool]]
) -> None:
    """Replace the clinic's CATALOG-BACKED plan set with `plans` = [(catalog_id,
    charge_deposit)].

    Only touches rows with `catalog_id IS NOT NULL` - the clinic's custom
    "Outro" rows (TASK-008) are managed separately
    (`create_tenant_custom_plan` / `update_tenant_plan_charge_deposit`) and are
    never wiped by this replace, since the frontend has no way to enumerate
    them by catalog id in the first place.

    A plan kept keeps its row (and its doctors); only `charge_deposit` is
    updated. A plan removed disappears from every doctor too. A plan added is
    accepted by NO doctor yet - the hub asks each doctor explicitly (decision
    2: a doctor's plans are chosen, not inherited - except the TASK-008 default
    for `clinic_with_exceptions`, which is about a doctor who never customized
    at all, not about a plan freshly added). Raises UnknownCatalogIds before
    writing anything. No commit.
    """
    wanted: dict[UUID, bool] = {}
    for catalog_id, charge_deposit in plans:
        wanted[UUID(str(catalog_id))] = bool(charge_deposit)
    catalog = {entry.id: entry for entry in await list_catalog(session)}
    unknown = [str(catalog_id) for catalog_id in wanted if catalog_id not in catalog]
    if unknown:
        raise UnknownCatalogIds(unknown)

    current = {
        plan.catalog_id: plan
        for plan, _entry in await list_tenant_plans(session, tenant.id)
        if plan.catalog_id is not None
    }
    await _drop_tenant_plans(session, tenant.id, [cid for cid in current if cid not in wanted])
    for catalog_id, charge_deposit in wanted.items():
        existing = current.get(catalog_id)
        if existing is not None:
            existing.charge_deposit = charge_deposit
        else:
            session.add(
                TenantInsurancePlan(
                    tenant_id=tenant.id, catalog_id=catalog_id, charge_deposit=charge_deposit
                )
            )
    await session.flush()
    _mirror_legacy(
        tenant,
        sorted((catalog[cid].name for cid in wanted), key=normalize),
        (entry.name for entry in catalog.values()),
    )


async def create_tenant_custom_plan(
    session: AsyncSession,
    tenant: Tenant,
    *,
    custom_name: str,
    custom_payment_note: str | None,
    charge_deposit: bool = True,
) -> TenantInsurancePlan:
    """The clinic's "Outro" convênio (TASK-008 §3.3) - outside the catalog.

    Never touched by `set_tenant_plans`' replace semantics. No commit.
    """
    plan = TenantInsurancePlan(
        tenant_id=tenant.id,
        custom_name=custom_name.strip()[:120],
        custom_payment_note=(custom_payment_note or "").strip()[:2000] or None,
        charge_deposit=charge_deposit,
    )
    session.add(plan)
    await session.flush()
    logger.info("hub_insurance_custom_plan_created", tenant_id=str(tenant.id))
    return plan


async def update_tenant_plan_charge_deposit(
    session: AsyncSession, tenant: Tenant, plan_id: UUID, charge_deposit: bool
) -> TenantInsurancePlan:
    """Toggle `charge_deposit` for ONE of the clinic's plans, catalog or custom.

    A single small mutation for a field `set_tenant_plans`' catalog-only
    replace cannot reach for custom rows (TASK-008 implementation decision -
    see docs/CHECKPOINT_convenio_catalogo.md). Raises PlanNotFound. No commit.
    """
    plan = await session.get(TenantInsurancePlan, plan_id)
    if plan is None or plan.tenant_id != tenant.id:
        raise PlanNotFound(str(plan_id))
    plan.charge_deposit = charge_deposit
    await session.flush()
    return plan


async def create_professional_custom_plan(
    session: AsyncSession,
    professional: Professional,
    *,
    custom_name: str,
    custom_payment_note: str | None,
    charge_deposit: bool = True,
) -> ProfessionalInsurancePlan:
    """A doctor's OWN "Outro" convênio - `independent` mode only (caller-gated).

    No commit.
    """
    plan = ProfessionalInsurancePlan(
        professional_id=professional.id,
        tenant_id=professional.tenant_id,
        custom_name=custom_name.strip()[:120],
        custom_payment_note=(custom_payment_note or "").strip()[:2000] or None,
        charge_deposit=charge_deposit,
    )
    session.add(plan)
    await session.flush()
    logger.info(
        "hub_professional_insurance_custom_plan_created",
        tenant_id=str(professional.tenant_id),
        professional_id=str(professional.id),
    )
    return plan


async def sync_legacy_insurances(
    session: AsyncSession, tenant: Tenant, names: Sequence[str] | None
) -> None:
    """A PUT of the legacy `insurances` strings, applied to the catalog tables.

    ORPHANED since TASK-008 removed `insurances` from `PUT /tenants/me/config`'s
    schema (docs/CHECKPOINT_convenio_catalogo.md) - no live endpoint calls this
    anymore. Kept, unmodified, for any admin tooling that might still want the
    "reclassify these legacy strings in bulk" behaviour; do not wire it back
    into a hub endpoint without re-reading why it was removed.

    Each string is matched against the catalog; matches become clinic plans,
    the rest are kept verbatim in `Tenant.insurances` and logged as a count.
    Because this path has no per-doctor control, a plan ADDED here is granted
    to every active doctor - exactly what "the clinic accepts X" meant before
    per-doctor acceptance existed. Plans already enabled keep their
    `charge_deposit` and doctors. No commit.
    """
    entries = [{"id": entry.id, "name": entry.name} for entry in await list_catalog(session)]
    matched_ids: list[UUID] = []
    unmatched = 0
    for raw in names or []:
        plan = match_plan(entries, str(raw))
        if plan is None:
            if str(raw).strip():
                unmatched += 1
            continue
        if plan["id"] not in matched_ids:
            matched_ids.append(plan["id"])
    if unmatched:
        logger.info(
            "insurance_legacy_unmatched", tenant_id=str(tenant.id), unmatched_count=unmatched
        )

    current = {
        plan.catalog_id: plan
        for plan, _entry in await list_tenant_plans(session, tenant.id)
        if plan.catalog_id is not None
    }
    await _drop_tenant_plans(session, tenant.id, [cid for cid in current if cid not in matched_ids])
    added = [cid for cid in matched_ids if cid not in current]
    tenant_plan_by_catalog_id: dict[UUID, TenantInsurancePlan] = {}
    for catalog_id in added:
        plan = TenantInsurancePlan(tenant_id=tenant.id, catalog_id=catalog_id)
        session.add(plan)
        tenant_plan_by_catalog_id[catalog_id] = plan
    await session.flush()
    if added:
        professional_ids = list(
            await session.scalars(
                select(Professional.id).where(
                    Professional.tenant_id == tenant.id, Professional.is_active.is_(True)
                )
            )
        )
        for professional_id in professional_ids:
            for catalog_id in added:
                session.add(
                    ProfessionalInsurancePlan(
                        professional_id=professional_id,
                        tenant_id=tenant.id,
                        tenant_plan_id=tenant_plan_by_catalog_id[catalog_id].id,
                    )
                )
        await session.flush()
    # Exactly what was typed (unmatched strings included): the old UI must
    # read back what it saved.
    tenant.insurances = [str(value) for value in (names or []) if str(value).strip()]


async def set_professional_clinic_plans(
    session: AsyncSession, professional: Professional, plan_ids: Sequence[str]
) -> list[str]:
    """`clinic_with_exceptions`: replace the clinic-plan subset THIS doctor accepts.

    REJECTS (never silently drops) an id that is not one of the clinic's own
    `tenant_insurance_plans` rows: the hub only offers the clinic's plans, so
    such an id means a stale screen or a forged request, and the save should
    fail visibly. Always marks `insurance_plans_customized = True`, even for an
    explicitly empty `plan_ids` - that is what makes "accepts nothing" durable
    against the TASK-008 inherit-all default (see Professional's docstring).
    Raises NotClinicPlans. No commit.
    """
    wanted: list[UUID] = []
    for raw in plan_ids:
        plan_id = UUID(str(raw))
        if plan_id not in wanted:
            wanted.append(plan_id)
    clinic_ids = {
        plan.id for plan, _entry in await list_tenant_plans(session, professional.tenant_id)
    }
    outside = [str(plan_id) for plan_id in wanted if plan_id not in clinic_ids]
    if outside:
        raise NotClinicPlans(outside)
    await session.execute(
        delete(ProfessionalInsurancePlan).where(
            ProfessionalInsurancePlan.professional_id == professional.id,
            ProfessionalInsurancePlan.tenant_plan_id.is_not(None),
        )
    )
    for plan_id in wanted:
        session.add(
            ProfessionalInsurancePlan(
                professional_id=professional.id,
                tenant_id=professional.tenant_id,
                tenant_plan_id=plan_id,
            )
        )
    professional.insurance_plans_customized = True
    await session.flush()
    return sorted(str(plan_id) for plan_id in wanted)


async def set_professional_independent_plans(
    session: AsyncSession, professional: Professional, catalog_ids: Sequence[str]
) -> list[str]:
    """`independent`: replace the GLOBAL catalog picks THIS doctor accepts.

    No clinic subset check - independent mode's whole point is that a doctor's
    list is not bounded by what the clinic itself enabled. REJECTS an id
    outside the active catalog (UnknownCatalogIds), same 422-before-write shape
    as every other id validation in this module. Never touches this doctor's
    "Outro" rows (`custom_name` set) or clinic-mode rows (`tenant_plan_id` set,
    which independent mode should not have anyway). No commit.
    """
    wanted: list[UUID] = []
    for raw in catalog_ids:
        catalog_id = UUID(str(raw))
        if catalog_id not in wanted:
            wanted.append(catalog_id)
    known = {entry.id for entry in await list_catalog(session)}
    unknown = [str(catalog_id) for catalog_id in wanted if catalog_id not in known]
    if unknown:
        raise UnknownCatalogIds(unknown)
    await session.execute(
        delete(ProfessionalInsurancePlan).where(
            ProfessionalInsurancePlan.professional_id == professional.id,
            ProfessionalInsurancePlan.catalog_id.is_not(None),
        )
    )
    for catalog_id in wanted:
        session.add(
            ProfessionalInsurancePlan(
                professional_id=professional.id,
                tenant_id=professional.tenant_id,
                catalog_id=catalog_id,
            )
        )
    await session.flush()
    return sorted(str(catalog_id) for catalog_id in wanted)


# ---------------------------------------------------------------------------
# Extensible catalog (admin) + reconciliation of legacy unmatched strings
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    base = normalize(name).replace(" ", "-")
    return "".join(ch for ch in base if ch.isalnum() or ch == "-") or "convenio"


async def create_catalog_entry(
    session: AsyncSession,
    *,
    name: str,
    aliases: Sequence[str] | None = None,
    mechanism: str,
    note: str | None,
) -> InsuranceCatalog:
    """Admin: add ONE operator to the global catalog. No code migration needed.

    Raises DuplicateCatalogEntry when the normalized name collides with an
    existing entry (active or not - reactivating a disabled row is a distinct,
    explicit operation this function does not perform). Commits nothing.
    """
    normalized = normalize(name)
    if not normalized:
        raise ValueError("name must not be empty")
    clash = await session.scalar(
        select(InsuranceCatalog.id).where(InsuranceCatalog.normalized_name == normalized)
    )
    if clash is not None:
        raise DuplicateCatalogEntry(normalized)

    slug = _slugify(name)
    existing_slugs = set(await session.scalars(select(InsuranceCatalog.slug)))
    candidate = slug
    suffix = 2
    while candidate in existing_slugs:
        candidate = f"{slug}-{suffix}"
        suffix += 1

    entry = InsuranceCatalog(
        slug=candidate,
        name=name.strip()[:120],
        normalized_name=normalized,
        aliases=[str(alias).strip()[:120] for alias in (aliases or []) if str(alias).strip()],
        mechanism=mechanism,
        note=(note or "").strip()[:2000] or None,
    )
    session.add(entry)
    await session.flush()
    logger.info("admin_insurance_catalog_entry_created", catalog_id=str(entry.id), slug=entry.slug)
    return entry


async def record_unmatched(session: AsyncSession, tenant_id: UUID, raw_text: str) -> None:
    """Track a clinic's legacy convênio text that matched nothing (idempotent).

    A no-op when this exact (tenant, normalized text) pair is already tracked
    - resolved or not (the unique constraint is the source of truth; this
    function just avoids raising on the expected conflict).
    """
    text_value = str(raw_text).strip()
    key = normalize(text_value)
    if not key:
        return
    existing = await session.scalar(
        select(InsuranceCatalogUnmatched.id).where(
            InsuranceCatalogUnmatched.tenant_id == tenant_id,
            InsuranceCatalogUnmatched.normalized_text == key[:255],
        )
    )
    if existing is not None:
        return
    session.add(
        InsuranceCatalogUnmatched(
            tenant_id=tenant_id, raw_text=text_value[:255], normalized_text=key[:255]
        )
    )
    await session.flush()


async def reconcile_unmatched_insurances(
    session: AsyncSession, catalog_entry: InsuranceCatalog
) -> int:
    """Grant every tenant whose unmatched text now matches `catalog_entry`.

    Runs inside the SAME transaction as the admin endpoint that creates (or
    extends the aliases of) a catalog entry. For each unresolved
    `insurance_catalog_unmatched` row whose normalized text is now one of
    `catalog_match_keys(catalog_entry)`: creates a `tenant_insurance_plans` row
    for that tenant (idempotent - skipped if one already exists,
    `charge_deposit=True` default) and stamps `resolved_at` +
    `resolved_insurance_catalog_id`. Returns the count resolved. No commit.
    """
    keys = catalog_match_keys(catalog_entry)
    if not keys:
        return 0
    pending = list(
        await session.scalars(
            select(InsuranceCatalogUnmatched).where(
                InsuranceCatalogUnmatched.resolved_at.is_(None),
                InsuranceCatalogUnmatched.normalized_text.in_(keys),
            )
        )
    )
    resolved = 0
    for row in pending:
        existing_plan = await session.scalar(
            select(TenantInsurancePlan.id).where(
                TenantInsurancePlan.tenant_id == row.tenant_id,
                TenantInsurancePlan.catalog_id == catalog_entry.id,
            )
        )
        if existing_plan is None:
            session.add(TenantInsurancePlan(tenant_id=row.tenant_id, catalog_id=catalog_entry.id))
        row.resolved_at = datetime.now(UTC)
        row.resolved_insurance_catalog_id = catalog_entry.id
        resolved += 1
    if resolved:
        await session.flush()
        logger.info(
            "insurance_catalog_reconciled",
            catalog_id=str(catalog_entry.id),
            resolved_count=resolved,
        )
    return resolved


# ---------------------------------------------------------------------------
# Agenda read model (TASK-014 / spec TASK E): which plan did each booking use?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppointmentPlan:
    """One convênio row as the agenda shows it: an identity, a name, a flag.

    Never carries `custom_payment_note` or catalog metadata - the read model is
    the wrong place to widen what leaves the clinic's own screen.
    """

    id: UUID
    name: str
    charge_deposit: bool


@dataclass(frozen=True)
class AppointmentPlanLookup:
    """Resolved plan rows for one page of appointments, keyed by row id."""

    tenant: dict[UUID, AppointmentPlan] = field(default_factory=dict)
    professional: dict[UUID, AppointmentPlan] = field(default_factory=dict)

    def resolve(
        self, tenant_plan_id: UUID | None, professional_plan_id: UUID | None
    ) -> AppointmentPlan | None:
        """The plan one appointment points at, or None.

        Same precedence as `deposit_lifecycle._insurance_charges_deposit`: a
        professional row (only ever set under `independent` mode) decides ALONE.
        If it does not resolve (another clinic's id, a doctor row that carries no
        name of its own) the answer is None - it never falls back to the clinic
        row, or the agenda and the deposit guard would name different plans.
        """
        if professional_plan_id is not None:
            return self.professional.get(professional_plan_id)
        if tenant_plan_id is not None:
            return self.tenant.get(tenant_plan_id)
        return None


def _plan_from_row(
    plan_id: UUID, charge_deposit: bool | None, custom_name: str | None, catalog_name: str | None
) -> AppointmentPlan | None:
    name = (catalog_name or custom_name or "").strip()
    if not name:
        return None
    # `is not False`: same reading as the deposit guard - only an explicit false waives.
    return AppointmentPlan(id=plan_id, name=name, charge_deposit=charge_deposit is not False)


async def load_appointment_plans(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    tenant_plan_ids: Iterable[UUID | None],
    professional_plan_ids: Iterable[UUID | None],
) -> AppointmentPlanLookup:
    """Resolve every plan id of a page of appointments in at most TWO queries.

    One per table, `IN (...)`, both filtered by `tenant_id`: a plan id that
    belongs to another clinic (a forged or corrupted appointment row) is simply
    absent from the result. No ids for a table means no query for that table.
    """
    clinic_ids = {i for i in tenant_plan_ids if i is not None}
    doctor_ids = {i for i in professional_plan_ids if i is not None}

    clinic: dict[UUID, AppointmentPlan] = {}
    if clinic_ids:
        rows = await session.execute(
            select(
                TenantInsurancePlan.id,
                TenantInsurancePlan.charge_deposit,
                TenantInsurancePlan.custom_name,
                InsuranceCatalog.name,
            )
            .select_from(TenantInsurancePlan)
            .outerjoin(InsuranceCatalog, InsuranceCatalog.id == TenantInsurancePlan.catalog_id)
            .where(
                TenantInsurancePlan.tenant_id == tenant_id,
                TenantInsurancePlan.id.in_(clinic_ids),
            )
        )
        for plan_id, charge, custom_name, catalog_name in rows.all():
            resolved = _plan_from_row(plan_id, charge, custom_name, catalog_name)
            if resolved is not None:
                clinic[plan_id] = resolved

    doctor: dict[UUID, AppointmentPlan] = {}
    if doctor_ids:
        rows = await session.execute(
            select(
                ProfessionalInsurancePlan.id,
                ProfessionalInsurancePlan.charge_deposit,
                ProfessionalInsurancePlan.custom_name,
                InsuranceCatalog.name,
            )
            .select_from(ProfessionalInsurancePlan)
            .outerjoin(
                InsuranceCatalog, InsuranceCatalog.id == ProfessionalInsurancePlan.catalog_id
            )
            .where(
                ProfessionalInsurancePlan.tenant_id == tenant_id,
                ProfessionalInsurancePlan.id.in_(doctor_ids),
            )
        )
        for plan_id, charge, custom_name, catalog_name in rows.all():
            resolved = _plan_from_row(plan_id, charge, custom_name, catalog_name)
            if resolved is not None:
                doctor[plan_id] = resolved

    return AppointmentPlanLookup(tenant=clinic, professional=doctor)
