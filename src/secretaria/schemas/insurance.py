"""Request/response schemas for the doctor-hub convênio catalog (api/hub/insurance.py)
and the admin catalog endpoint (api/admin/insurance.py).

Strict on input (`extra="forbid"`): a typo'd key silently ignored would save
something the clinic did not mean. The one exception is the three schemas an
older Portal still sends the retired `charge_deposit` to (TASK-085 R U1b): they
ignore unknown keys - every field they declare is required, so a typo there is
still a 422 (missing field) - and the wrappers around them stay strict.
Full contract: docs/CHECKPOINT_convenio_catalogo.md.
"""

from pydantic import BaseModel, ConfigDict, Field

from secretaria.models.insurance import (
    INSURANCE_MECHANISMS,
    INSURANCE_MODES,
    INSURANCE_MODES_SELECTABLE,
)

# The payment rule's hard ceiling BEFORE cleaning (anti-abuse: a megabyte body is refused by
# the schema). The real limit - 1000 characters of the CLEANED text - is
# services/insurance_payment_note.MAX_PAYMENT_NOTE_CHARS, checked by the route.
PAYMENT_NOTE_RAW_MAX = 5000


class InsuranceCatalogRead(BaseModel):
    """One global catalog entry. `mechanism`/`note` are metadata, never a rule."""

    id: str
    slug: str
    name: str
    mechanism: str = Field(description=f"One of: {', '.join(INSURANCE_MECHANISMS)}")
    note: str | None = None


class TenantInsurancePlanRead(BaseModel):
    """One of the clinic's convênio lines - catalog-backed or its own "Outro".

    `id` (a `tenant_insurance_plans` row id) is the identifier every write
    endpoint below keys on - `catalog_id` alone cannot, since a custom line has
    none. `is_custom` tells the hub the line is the clinic's (or doctor's) own
    "Outro". `custom_payment_note` (TASK-090 Adendo 1, contract C6) is the clinic's
    optional payment rule for this convênio - on EVERY line, catalog or "Outro" -
    read only by the AI when a patient asks; null = no rule.
    """

    id: str
    catalog_id: str | None = None
    name: str
    is_custom: bool
    custom_payment_note: str | None = None
    # Position the clinic chose in the patient's list (0 = first); None = not placed,
    # follows the placed ones alphabetically. The list itself comes back in this order.
    sort_order: int | None = None


class TenantInsurancePlanIn(BaseModel):
    # `ignore`, not `forbid`: an older Portal still sends the retired per-convênio sinal flag
    # (TASK-085 R U1b: a convênio never exempts the sinal); `catalog_id` is required, so a
    # typo'd key is still refused.
    model_config = ConfigDict(extra="ignore")

    catalog_id: str


class TenantInsurancePlansUpdate(BaseModel):
    """PUT /tenants/me/insurance-plans - replaces the CATALOG-BACKED subset only.

    Never touches the clinic's custom "Outro" rows - see
    services/insurance_catalog.py::set_tenant_plans.
    """

    model_config = ConfigDict(extra="forbid")

    plans: list[TenantInsurancePlanIn] = Field(default_factory=list, max_length=50)


class TenantInsurancePlanCustomCreate(BaseModel):
    """POST /tenants/me/insurance-plans/custom - the clinic's "Outro" convênio.

    Unknown keys are ignored (an older Portal's retired sinal flag - see TenantInsurancePlanIn).
    `custom_payment_note` is optional (TASK-090 Adendo 1, C6); see `PaymentNoteUpdate`.
    """

    model_config = ConfigDict(extra="ignore")

    custom_name: str = Field(min_length=1, max_length=120)
    custom_payment_note: str | None = Field(default=None, max_length=PAYMENT_NOTE_RAW_MAX)


class PaymentNoteUpdate(BaseModel):
    """PUT .../insurance-plans/{plan_id}/payment-note (TASK-090 Adendo 1, contract C6).

    `custom_payment_note`: the rule text; null or blank deletes it. The 1000-character limit
    is checked by the route AFTER cleaning (services/insurance_payment_note.py), so it counts
    what is stored; `PAYMENT_NOTE_RAW_MAX` only refuses an absurd payload before that.
    """

    model_config = ConfigDict(extra="forbid")

    custom_payment_note: str | None = Field(max_length=PAYMENT_NOTE_RAW_MAX)


class TenantInsurancePlanOrderUpdate(BaseModel):
    """PUT /tenants/me/insurance-plans/order - the patient list's order.

    `plan_ids` (`tenant_insurance_plans.id`) first, in this order; every plan left
    out follows alphabetically. An empty list restores the alphabetical order.
    """

    model_config = ConfigDict(extra="forbid")

    plan_ids: list[str] = Field(default_factory=list, max_length=100)


class InsuranceModeRead(BaseModel):
    mode: str | None = Field(default=None, description=f"One of: {', '.join(INSURANCE_MODES)}")


class InsuranceModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(description=f"One of: {', '.join(INSURANCE_MODES_SELECTABLE)}")


class ProfessionalInsurancePlansRead(BaseModel):
    """What the per-doctor picker needs - shape depends on `mode` (TASK-008 §4.1).

    `mode is None` or `"shared"`: nothing to configure here - the hub shows no
    section at all, and this endpoint answers 409 (see api/hub/insurance.py).
    `"clinic_with_exceptions"`: `selectable` is the clinic's own plans (by
    `TenantInsurancePlanRead.id`), `accepted_plan_ids` a subset of it, and
    `inherits_clinic` is true exactly when this doctor has never customized
    (accepts ALL of `selectable` by default - owner's decision, SPEC §2).
    `"independent"`: `selectable` is the WHOLE active global catalog plus this
    doctor's own custom rows, `accepted_plan_ids` this doctor's own picks
    (catalog ids and/or their own custom-row ids), `inherits_clinic` always
    false (nothing to inherit).
    """

    professional_id: str
    mode: str | None
    selectable: list[TenantInsurancePlanRead]
    accepted_plan_ids: list[str]
    inherits_clinic: bool = False


class ProfessionalInsurancePlansUpdate(BaseModel):
    """PUT .../professionals/{id}/insurance-plans - payload varies by mode.

    `clinic_with_exceptions` sends `plan_ids` (the clinic's own plan ids);
    `independent` sends `catalog_ids` (global catalog ids). The endpoint 422s
    if the field that does not match the tenant's current mode is sent, or if
    neither is sent.
    """

    model_config = ConfigDict(extra="forbid")

    plan_ids: list[str] | None = Field(default=None, max_length=50)
    catalog_ids: list[str] | None = Field(default=None, max_length=50)


class ProfessionalInsuranceCustomCreate(BaseModel):
    """POST .../professionals/{id}/insurance-plans/custom - independent mode only.

    Unknown keys are ignored (an older Portal's retired sinal flag - see TenantInsurancePlanIn).
    `custom_payment_note` is optional (TASK-090 Adendo 1, C6); see `PaymentNoteUpdate`.
    """

    model_config = ConfigDict(extra="ignore")

    custom_name: str = Field(min_length=1, max_length=120)
    custom_payment_note: str | None = Field(default=None, max_length=PAYMENT_NOTE_RAW_MAX)


# ---------------------------------------------------------------------------
# Admin (SaaS-owner) - extensible catalog
# ---------------------------------------------------------------------------


class InsuranceCatalogAdminCreate(BaseModel):
    """POST /admin/insurance-catalog - add one operator, no code migration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    # Extra spellings a clinic's legacy free text might have used - matched the
    # same way the canonical name is (services/insurance_catalog.py::catalog_match_keys).
    aliases: list[str] = Field(default_factory=list, max_length=20)
    mechanism: str = Field(
        default="desconhecido", description=f"One of: {', '.join(INSURANCE_MECHANISMS)}"
    )
    note: str | None = Field(default=None, max_length=2000)


class InsuranceCatalogAdminRead(BaseModel):
    id: str
    slug: str
    name: str
    aliases: list[str]
    mechanism: str
    note: str | None
    is_active: bool
    # How many previously-unmatched legacy strings this creation just resolved.
    reconciled_count: int = 0


class InsuranceCatalogUnmatchedRead(BaseModel):
    """GET /admin/insurance-catalog/unmatched - operator visibility only.

    Never a rule the product enforces - the moment a matching catalog entry
    exists, `reconcile_unmatched_insurances` resolves it on its own.
    """

    tenant_id: str
    raw_text: str
    first_seen_at: str
