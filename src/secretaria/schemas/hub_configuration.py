"""Aggregate request/response for the transactional hub configuration save.

PUT /tenants/me/configuration replaces the "two PUTs and hope" pattern the
Configuração screen used to run: tenant config first, professional config
second, each committing on its own. A failure on the second left the first
already live, with the UI unable to say which half had landed.

This envelope carries both patches so the server can validate them together and
commit them together. Either patch may be omitted — a tenant with no
professional selected sends only `tenant`, and a screen that only touched one
professional's hours may send only the professional pair.

Deliberate design notes:

* `extra="forbid"` applies to THIS envelope only. The nested TenantConfigUpdate
  and ProfessionalConfigUpdate keep whatever leniency they already had, because
  tightening them here would silently change the contract of the two legacy
  endpoints that share those classes (PUT /config still accepts and ignores
  retired fields such as `greeting_buttons`). Forbidding extras on the wrapper
  catches the realistic mistake — a caller inventing a top-level key, or
  spelling `professionalId` in camelCase — without breaking anyone.

* `professional_id` and `professional` travel as a pair, enforced below. An id
  with no patch would be a no-op that still looked like a write; a patch with
  no id would have nowhere to land.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from secretaria.schemas.config import TenantConfigRead, TenantConfigUpdate
from secretaria.schemas.professional import ProfessionalConfigUpdate, ProfessionalListItem


class HubConfigurationUpdate(BaseModel):
    """PUT /tenants/me/configuration body."""

    model_config = ConfigDict(extra="forbid")

    # Tenant-level patch, same semantics as PUT /tenants/me/config (partial:
    # absent fields are left untouched, `is_active: true` still passes the
    # activation gate).
    tenant: TenantConfigUpdate | None = None

    # Which professional the professional patch applies to. Always resolved
    # against the tenant from the hub token — a professional belonging to
    # another clinic reads as "not found", never as a permission error.
    professional_id: str | None = Field(default=None, max_length=64)

    # Per-professional patch, same semantics as
    # PUT /tenants/me/professionals/{id}/config.
    professional: ProfessionalConfigUpdate | None = None

    @model_validator(mode="after")
    def _professional_pair_is_complete(self) -> HubConfigurationUpdate:
        if (self.professional_id is None) != (self.professional is None):
            raise ValueError(
                "professional_id and professional must be provided together, or both omitted"
            )
        return self

    @model_validator(mode="after")
    def _at_least_one_patch(self) -> HubConfigurationUpdate:
        # An empty body would commit nothing while returning 200, which reads
        # to the caller as a successful save. Refuse it instead.
        if self.tenant is None and self.professional is None:
            raise ValueError("at least one of `tenant` or `professional` must be provided")
        return self


class HubConfigurationRead(BaseModel):
    """PUT /tenants/me/configuration response.

    Both halves are built by the same readers the GET endpoints use
    (services/hub_configuration.py), so a client can hydrate straight from this
    response and a subsequent GET will agree with it field for field.
    `professional` is null when the request carried no professional patch.
    """

    tenant: TenantConfigRead
    professional: ProfessionalListItem | None = None
