"""Request/response schemas for the doctor-hub convênio catalog (api/hub/insurance.py).

Strict on input (`extra="forbid"`): these endpoints are new, have exactly one
client (the secretarIA-frontend hub, part 2 of TASK-006), and a typo'd key such
as `chargeDeposit` silently ignored would leave a clinic charging deposits it
meant to switch off. Full contract: docs/CHECKPOINT_convenio_catalogo.md.
"""

from pydantic import BaseModel, ConfigDict, Field

from secretaria.models.insurance import INSURANCE_MECHANISMS


class InsuranceCatalogRead(BaseModel):
    """One global catalog entry. `mechanism`/`note` are metadata, never a rule."""

    id: str
    slug: str
    name: str
    mechanism: str = Field(description=f"One of: {', '.join(INSURANCE_MECHANISMS)}")
    note: str | None = None


class TenantInsurancePlanRead(BaseModel):
    catalog_id: str
    name: str
    charge_deposit: bool


class TenantInsurancePlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_id: str
    # Default = today's behaviour: the tenant's pix_deposit_* policy applies.
    charge_deposit: bool = True


class TenantInsurancePlansUpdate(BaseModel):
    """PUT /tenants/me/insurance-plans - the clinic's WHOLE plan set (replace)."""

    model_config = ConfigDict(extra="forbid")

    plans: list[TenantInsurancePlanIn] = Field(default_factory=list, max_length=50)


class ProfessionalInsurancePlansRead(BaseModel):
    """What the per-doctor picker needs: the options and the ticked ones.

    `selectable` is exactly the clinic's plans - a doctor is never offered a
    plan the clinic did not enable. `accepted_catalog_ids` is a subset of it.
    """

    professional_id: str
    selectable: list[TenantInsurancePlanRead]
    accepted_catalog_ids: list[str]


class ProfessionalInsurancePlansUpdate(BaseModel):
    """PUT .../professionals/{id}/insurance-plans - the doctor's WHOLE set (replace)."""

    model_config = ConfigDict(extra="forbid")

    catalog_ids: list[str] = Field(default_factory=list, max_length=50)
