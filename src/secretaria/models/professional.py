"""Professional model - one bookable staff member within a tenant.

Multi-professional CRUD/tooling (creating more than one, or an EXTRA one
beyond the auto-created default) is entitlement-gated by the
`multi_professional` plugin (see plugins/multi_professional.py) - but a
tenant with a SINGLE professional is not addon-gated: the onboarding
migration backfills exactly one `Professional` row per pre-existing tenant
(named after `clinic_name`), so every tenant has at least one from here on.

A professional optionally carries its own Google Calendar id (falls back to
the tenant's own `google_calendar_id`;
services/calendar.py:CalendarService.from_tenant_config) and, separately, its
own encrypted Google refresh token (models/professional_credentials.py).
`business_hours`/`appointment_types` mirror the tenant's own columns and fall
back to them when NULL (services/tenant_config.py:
`professional_business_hours` / `professional_appointment_types`) - this is
the "legacy single-professional config still applies" path.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Professional(Base):
    """A bookable professional (doctor, therapist, ...) belonging to a tenant."""

    __tablename__ = "professionals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    # None -> falls back to the tenant's own calendar (google_calendar_id).
    google_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)

    # --- Per-professional config (onboarding/multi-professional contract v1) ---
    # Free-text specialty label shown to patients, e.g. "Cardiologia".
    specialty: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Short bio surfaced by the doctor hub / patient-facing pitch.
    about: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-text context/instructions injected into the system prompt for
    # THIS professional (ai/prompts.py::_format_professional_context) —
    # background the LLM personalizes tone/content with, never a script to
    # recite. Not a persona/safety override: the hardcoded safety/tone rules
    # in ai/prompts.py::_format_safety_rules apply regardless of this field.
    context_doctor_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Same shape as tenants.business_hours. NULL = fall back to the tenant's
    # own column (services/tenant_config.professional_business_hours) - the
    # legacy single-professional behaviour.
    business_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Same shape as tenants.appointment_types. NULL = fall back to the
    # tenant's own column (services/tenant_config.professional_appointment_types).
    appointment_types: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
