"""The convênio catalog: seed, matching, per-clinic and per-doctor selection.

Tables and the product decisions behind them: models/insurance.py. Contract the
hub consumes: docs/CHECKPOINT_convenio_catalogo.md.

Everything that decides "which catalog entry is this text?" goes through
`match_plan` here - the legacy-string migration, the hub's legacy sync, the
booking flow's typed answer and the appointment's `insurance_plan_id` - so the
four can never disagree about what "unimed " means.

PII: a patient's chosen plan identifies their coverage. Nothing in this module
logs a plan name next to a patient/conversation; counts and ids only.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import truncate_list_row_title
from secretaria.models.insurance import (
    InsuranceCatalog,
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
    """A doctor was asked to accept plans the clinic has not enabled."""

    def __init__(self, catalog_ids: list[str]) -> None:
        super().__init__(", ".join(catalog_ids))
        self.catalog_ids = catalog_ids


class UnknownCatalogIds(ValueError):
    """A clinic was asked to enable ids that are not in the (active) catalog."""

    def __init__(self, catalog_ids: list[str]) -> None:
        super().__init__(", ".join(catalog_ids))
        self.catalog_ids = catalog_ids


@dataclass(frozen=True)
class TenantInsurance:
    """What the pure flow router needs about one clinic's convênios.

    `plans` keeps catalog order (by name) as plain dicts, `{"id": str, "name":
    str}`, so it travels on the tenant snapshot like `appointment_types` does.
    `accepted_by` maps professional id (str) -> the catalog ids (str) that
    doctor accepts; a doctor with no rows maps to nothing, which the flow
    renders as "not marked" - never as hidden.
    """

    plans: list[dict] = field(default_factory=list)
    accepted_by: dict[str, frozenset[str]] = field(default_factory=dict)


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


async def list_tenant_plans(
    session: AsyncSession, tenant_id: UUID
) -> list[tuple[TenantInsurancePlan, InsuranceCatalog]]:
    """The clinic's enabled plans with their catalog rows, by name."""
    rows = await session.execute(
        select(TenantInsurancePlan, InsuranceCatalog)
        .join(InsuranceCatalog, InsuranceCatalog.id == TenantInsurancePlan.catalog_id)
        .where(TenantInsurancePlan.tenant_id == tenant_id)
        .order_by(InsuranceCatalog.name)
    )
    return [(plan, entry) for plan, entry in rows.all()]


async def professional_plan_ids(session: AsyncSession, professional_id: UUID) -> list[str]:
    rows = await session.scalars(
        select(ProfessionalInsurancePlan.catalog_id).where(
            ProfessionalInsurancePlan.professional_id == professional_id
        )
    )
    return sorted(str(catalog_id) for catalog_id in rows)


async def load_tenant_insurance(session: AsyncSession, tenant_id: UUID) -> TenantInsurance:
    """ONE read per turn of everything the flow needs (see TenantInsurance).

    Transition rule (until the hub frontend reads only the catalog): a legacy
    `Tenant.insurances` string that matches NO catalog entry at all is still
    offered to the patient, after the catalog plans, as `{"id": None, ...}`.
    The migration and `sync_legacy_insurances` deliberately keep such strings
    instead of deleting them - dropping them from the patient's list would
    silently undo what the clinic configured (and, for a clinic whose every
    plan is outside the catalog, skip the convênio question altogether). No
    id means it can never mark a doctor or reach `insurance_plan_id`.
    """
    plans = [
        {"id": str(entry.id), "name": entry.name}
        for _plan, entry in await list_tenant_plans(session, tenant_id)
    ]
    plans.extend(
        {"id": None, "name": name} for name in await _legacy_only_names(session, tenant_id)
    )
    accepted: dict[str, set[str]] = {}
    rows = await session.execute(
        select(
            ProfessionalInsurancePlan.professional_id, ProfessionalInsurancePlan.catalog_id
        ).where(ProfessionalInsurancePlan.tenant_id == tenant_id)
    )
    for professional_id, catalog_id in rows.all():
        accepted.setdefault(str(professional_id), set()).add(str(catalog_id))
    return TenantInsurance(
        plans=plans,
        accepted_by={key: frozenset(value) for key, value in accepted.items()},
    )


async def resolve_tenant_plan_id(
    session: AsyncSession, tenant_id: UUID, insurance: str | None
) -> UUID | None:
    """Catalog id of the clinic plan `insurance` names, or None.

    Applied once, when an appointment row is created: "Particular", a typed
    "Outro convênio" and anything the clinic does not accept resolve to None,
    and the deposit guard then leaves the tenant's policy alone.
    """
    if not insurance or not insurance.strip():
        return None
    plans = [
        {"id": entry.id, "name": entry.name}
        for _plan, entry in await list_tenant_plans(session, tenant_id)
    ]
    matched = match_plan(plans, insurance)
    return matched["id"] if matched is not None else None


async def _legacy_only_names(session: AsyncSession, tenant_id: UUID) -> list[str]:
    """The clinic's legacy strings that match no catalog entry (deduplicated)."""
    legacy = await session.scalar(select(Tenant.insurances).where(Tenant.id == tenant_id))
    if not legacy:
        return []
    known = set(await session.scalars(select(InsuranceCatalog.normalized_name)))
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
    await session.execute(
        delete(ProfessionalInsurancePlan).where(
            ProfessionalInsurancePlan.tenant_id == tenant_id,
            ProfessionalInsurancePlan.catalog_id.in_(ids),
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
    """Replace the clinic's plan set with `plans` = [(catalog_id, charge_deposit)].

    A plan kept keeps its row (and its doctors); only `charge_deposit` is
    updated. A plan removed disappears from every doctor too. A plan added is
    accepted by NO doctor yet - the hub asks each doctor explicitly (decision
    2: a doctor's plans are chosen, not inherited). Raises UnknownCatalogIds
    before writing anything. No commit.
    """
    wanted: dict[UUID, bool] = {}
    for catalog_id, charge_deposit in plans:
        wanted[UUID(str(catalog_id))] = bool(charge_deposit)
    catalog = {entry.id: entry for entry in await list_catalog(session)}
    unknown = [str(catalog_id) for catalog_id in wanted if catalog_id not in catalog]
    if unknown:
        raise UnknownCatalogIds(unknown)

    current = {
        plan.catalog_id: plan for plan, _entry in await list_tenant_plans(session, tenant.id)
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


async def sync_legacy_insurances(
    session: AsyncSession, tenant: Tenant, names: Sequence[str] | None
) -> None:
    """A PUT of the legacy `insurances` strings, applied to the catalog tables.

    The transition path for a hub frontend that still edits the free-text list
    (part 2 of the rollout replaces it). Each string is matched against the
    catalog; matches become clinic plans, the rest are kept verbatim in
    `Tenant.insurances` and logged as a count. Because that old UI has no
    per-doctor control, a plan ADDED here is granted to every active doctor -
    exactly what "the clinic accepts X" meant before per-doctor acceptance
    existed. Plans already enabled keep their `charge_deposit` and doctors.
    No commit.
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
        plan.catalog_id: plan for plan, _entry in await list_tenant_plans(session, tenant.id)
    }
    await _drop_tenant_plans(session, tenant.id, [cid for cid in current if cid not in matched_ids])
    added = [cid for cid in matched_ids if cid not in current]
    for catalog_id in added:
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=catalog_id))
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
                        catalog_id=catalog_id,
                    )
                )
        await session.flush()
    # Exactly what was typed (unmatched strings included): the old UI must
    # read back what it saved.
    tenant.insurances = [str(value) for value in (names or []) if str(value).strip()]


async def set_professional_plans(
    session: AsyncSession, professional: Professional, catalog_ids: Sequence[str]
) -> list[str]:
    """Replace the plans `professional` accepts. Subset of the clinic's or nothing.

    REJECTS (never silently drops) an id the clinic has not enabled: the hub
    only offers the clinic's plans, so such an id means a stale screen or a
    forged request, and the save should fail visibly rather than leave a
    ticked box that quietly did not stick. Raises NotClinicPlans before
    writing anything. No commit.
    """
    wanted: list[UUID] = []
    for raw in catalog_ids:
        catalog_id = UUID(str(raw))
        if catalog_id not in wanted:
            wanted.append(catalog_id)
    clinic = {
        plan.catalog_id for plan, _entry in await list_tenant_plans(session, professional.tenant_id)
    }
    outside = [str(catalog_id) for catalog_id in wanted if catalog_id not in clinic]
    if outside:
        raise NotClinicPlans(outside)
    await session.execute(
        delete(ProfessionalInsurancePlan).where(
            ProfessionalInsurancePlan.professional_id == professional.id
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
