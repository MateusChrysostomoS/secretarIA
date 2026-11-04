"""Write path for the clinic's operational e-mail (`Tenant.contact_email`).

One function shared by the hub route and the internal (brain-api) route, so the two cannot
drift. It does NOT commit (the API layer does) and logs only the tenant id and whether the
address was set or cleared — never the address itself (PII).
"""

from __future__ import annotations

from secretaria.core.logging import get_logger
from secretaria.models import Tenant

logger = get_logger(__name__)


def set_contact_email(tenant: Tenant, contact_email: str | None) -> str | None:
    """Assign an already-normalised address (None clears it); return what is now stored."""
    tenant.contact_email = contact_email
    logger.info(
        "contact_email_updated",
        tenant_id=str(tenant.id),
        set=contact_email is not None,
        cleared=contact_email is None,
    )
    return contact_email
