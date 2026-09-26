"""insurance_catalog: global convênio catalog + per-clinic and per-doctor plans

TASK-006 part 1 (docs/CHECKPOINT_convenio_catalogo.md). Three new tables, one
new nullable column, a seed and a best-effort backfill of the legacy strings:

- `insurance_catalog` (global, shared by every tenant) seeded with the
  operators researched for the catalog - ids are uuid5(namespace, slug), the
  same ids services/insurance_catalog.py computes, so every environment agrees.
- `tenant_insurance_plans` (tenant x catalog, `charge_deposit` NOT NULL
  default true = today's behaviour for every clinic).
- `professional_insurance_plans` with a composite FK onto
  `tenant_insurance_plans (tenant_id, catalog_id)`: the "a doctor accepts only
  plans the clinic accepts" rule is enforced by Postgres itself.
- `appointments.insurance_plan_id` (nullable FK, SET NULL).

Backfill of `tenants.insurances` (free strings): each string is matched against
the catalog on the normalized name (same rule as the app's `normalize`); a
match becomes a `tenant_insurance_plans` row AND is granted to every active
professional of that clinic - that is what "the clinic accepts X" meant before
per-doctor acceptance existed. An unmatched string is NOT deleted: it stays in
`tenants.insurances` and is logged as `insurance_migration_unmatched`
(tenant id + count, never the strings) for the operator to reclassify with the
SQL in the checkpoint doc.

Widen-before-narrow (skill frozen-contract-migration): purely additive. Old
API/worker code maps none of this and keeps reading `tenants.insurances`, which
is untouched - so this runs BEFORE the new code, and the API and the worker can
then be deployed in either order. Dropping `tenants.insurances` is a separate,
later migration, only after the hub frontend (part 2) reads the catalog alone
and BOTH services run code that no longer maps the column.

Revision ID: c9f4e2a7b815
Revises: b8e3c5a1d702
Create Date: 2026-09-26
"""

import logging
import re
import unicodedata
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f4e2a7b815"
down_revision: str | None = "b8e3c5a1d702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Frozen copies - a migration must not import app code that can change later.
_NAMESPACE = uuid.UUID("6c1f0e3a-8d2b-4f5e-9a7c-2b3d4e5f6a70")
_WHITESPACE = re.compile(r"\s+")

# (slug, name, mechanism, note). Provenance per row (URL + access date):
# docs/CHECKPOINT_convenio_catalogo.md. Same rows as
# services/insurance_catalog.py::CATALOG_SEED at the time of writing.
_SEED: tuple[tuple[str, str, str, str], ...] = (
    (
        "alice",
        "Alice",
        "variavel_por_plano",
        "Modelo coordenado/verticalizado; sem reembolso em contratos pequenos, "
        "reembolso só em linhas específicas para empresas maiores.",
    ),
    (
        "amil",
        "Amil",
        "variavel_por_plano",
        "Reembolso (livre escolha) só nas linhas Ouro/Black; planos de entrada usam só "
        "rede credenciada/própria.",
    ),
    (
        "assim-saude",
        "Assim Saúde",
        "variavel_por_plano",
        "Rede credenciada própria (RJ) é o padrão; reembolso por livre escolha só no "
        "plano Assim Saúde Exclusivo.",
    ),
    (
        "bradesco-saude",
        "Bradesco Saúde",
        "variavel_por_plano",
        "Rede referenciada; reembolso pela tabela THSM (CRS x múltiplo contratado), "
        "varia por linha de produto.",
    ),
    (
        "care-plus",
        "Care Plus",
        "variavel_por_plano",
        "Premium/executivo; reembolso para consultas de livre escolha, e também rede "
        "credenciada sem desembolso.",
    ),
    (
        "cassi",
        "CASSI",
        "variavel_por_plano",
        "Autogestão; rede referenciada e livre escolha com reembolso limitado à Tabela "
        "Geral de Auxílios, conforme o plano.",
    ),
    (
        "golden-cross",
        "Golden Cross",
        "variavel_por_plano",
        "Uso típico é rede credenciada; reembolso fora da rede com percentual/valor por "
        "categoria de plano.",
    ),
    (
        "hapvida",
        "Hapvida",
        "variavel_por_plano",
        "Verticalizado (grupo Hapvida NotreDame Intermédica); rede própria, "
        "coparticipação em parte dos planos, reembolso raro.",
    ),
    (
        "notredame-intermedica",
        "NotreDame Intermédica",
        "variavel_por_plano",
        "Verticalizado (grupo Hapvida NotreDame Intermédica); coparticipação em parte "
        "dos planos, reembolso só na linha Advance.",
    ),
    (
        "omint",
        "Omint",
        "variavel_por_plano",
        "Rede credenciada disponível; reembolso elevado por livre escolha é o "
        "diferencial da linha Premium, varia por linha.",
    ),
    (
        "porto-seguro-saude",
        "Porto Seguro Saúde",
        "variavel_por_plano",
        "Linha Porto Saúde é de livre escolha com reembolso pela tabela própria, "
        "variando pela categoria contratada.",
    ),
    (
        "prevent-senior",
        "Prevent Senior",
        "desconhecido",
        "Modelo verticalizado (rede própria); fontes públicas conflitam sobre reembolso "
        "de consulta, sem confirmação oficial.",
    ),
    (
        "sulamerica",
        "SulAmérica",
        "variavel_por_plano",
        "Rede referenciada com reembolso na maioria das linhas; a linha Direto não "
        "reembolsa consulta eletiva.",
    ),
    (
        "unimed",
        "Unimed",
        "variavel_por_plano",
        "Cooperativa; coparticipação comum e reembolso por livre escolha limitado, "
        "dependendo do produto/cooperativa.",
    ),
)


def _normalize(name: str | None) -> str:
    text = _WHITESPACE.sub(" ", (name or "").strip())
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "insurance_catalog",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False, unique=True),
        sa.Column("mechanism", sa.String(length=32), nullable=False, server_default="desconhecido"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "mechanism IN ('reembolso', 'coparticipacao', 'desconto_direto', "
            "'variavel_por_plano', 'desconhecido')",
            name="ck_insurance_catalog_mechanism",
        ),
    )
    op.create_table(
        "tenant_insurance_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "catalog_id",
            sa.Uuid(),
            sa.ForeignKey("insurance_catalog.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("charge_deposit", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "catalog_id", name="uq_tenant_insurance_plans"),
    )
    op.create_index("ix_tenant_insurance_plans_tenant_id", "tenant_insurance_plans", ["tenant_id"])
    op.create_index(
        "ix_tenant_insurance_plans_catalog_id", "tenant_insurance_plans", ["catalog_id"]
    )
    op.create_table(
        "professional_insurance_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "professional_id",
            sa.Uuid(),
            sa.ForeignKey("professionals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "professional_id", "catalog_id", name="uq_professional_insurance_plans"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "catalog_id"],
            ["tenant_insurance_plans.tenant_id", "tenant_insurance_plans.catalog_id"],
            ondelete="CASCADE",
            name="fk_professional_insurance_plans_tenant_plan",
        ),
    )
    op.create_index(
        "ix_professional_insurance_plans_professional_id",
        "professional_insurance_plans",
        ["professional_id"],
    )
    op.create_index(
        "ix_professional_insurance_plans_tenant_id",
        "professional_insurance_plans",
        ["tenant_id"],
    )
    op.add_column(
        "appointments",
        sa.Column(
            "insurance_plan_id",
            sa.Uuid(),
            sa.ForeignKey(
                "insurance_catalog.id",
                ondelete="SET NULL",
                name="fk_appointments_insurance_plan_id",
            ),
            nullable=True,
        ),
    )

    catalog = sa.table(
        "insurance_catalog",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("normalized_name", sa.String()),
        sa.column("mechanism", sa.String()),
        sa.column("note", sa.Text()),
    )
    op.bulk_insert(
        catalog,
        [
            {
                "id": uuid.uuid5(_NAMESPACE, slug),
                "slug": slug,
                "name": name,
                "normalized_name": _normalize(name),
                "mechanism": mechanism,
                "note": note,
            }
            for slug, name, mechanism, note in _SEED
        ],
    )
    _backfill_legacy_insurances()


def _backfill_legacy_insurances() -> None:
    by_name = {_normalize(name): uuid.uuid5(_NAMESPACE, slug) for slug, name, _m, _n in _SEED}
    bind = op.get_bind()
    tenants = bind.execute(
        sa.text("SELECT id, insurances FROM tenants WHERE insurances IS NOT NULL")
    ).all()
    matched_total = unmatched_total = 0
    for tenant_id, raw in tenants:
        values = raw if isinstance(raw, list) else []
        catalog_ids: list[uuid.UUID] = []
        unmatched = 0
        for value in values:
            key = _normalize(str(value))
            if not key:
                continue
            catalog_id = by_name.get(key)
            if catalog_id is None:
                unmatched += 1
            elif catalog_id not in catalog_ids:
                catalog_ids.append(catalog_id)
        if unmatched:
            unmatched_total += unmatched
            logger.info(
                "insurance_migration_unmatched tenant_id=%s unmatched_count=%d",
                tenant_id,
                unmatched,
            )
        if not catalog_ids:
            continue
        professional_ids = [
            row[0]
            for row in bind.execute(
                sa.text("SELECT id FROM professionals WHERE tenant_id = :t AND is_active"),
                {"t": tenant_id},
            ).all()
        ]
        for catalog_id in catalog_ids:
            matched_total += 1
            bind.execute(
                sa.text(
                    "INSERT INTO tenant_insurance_plans (id, tenant_id, catalog_id)"
                    " VALUES (:id, :t, :c)"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "c": catalog_id},
            )
            for professional_id in professional_ids:
                bind.execute(
                    sa.text(
                        "INSERT INTO professional_insurance_plans"
                        " (id, professional_id, tenant_id, catalog_id)"
                        " VALUES (:id, :p, :t, :c)"
                    ),
                    {"id": uuid.uuid4(), "p": professional_id, "t": tenant_id, "c": catalog_id},
                )
    logger.info(
        "insurance_migration_done tenants=%d matched_plans=%d unmatched_strings=%d",
        len(tenants),
        matched_total,
        unmatched_total,
    )


def downgrade() -> None:
    # Only safe while NO deployed service maps these tables/column (see the
    # module docstring). `tenants.insurances` was never modified, so nothing
    # needs restoring there.
    op.drop_column("appointments", "insurance_plan_id")
    op.drop_table("professional_insurance_plans")
    op.drop_table("tenant_insurance_plans")
    op.drop_table("insurance_catalog")
