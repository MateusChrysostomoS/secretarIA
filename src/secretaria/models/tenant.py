"""Tenant model - one clinic, with its own WhatsApp Business credentials."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Tenant(Base):
    """A clinic using SecretarIA.

    The system is multi-tenant in the data model. For the MVP a single tenant
    is configured via environment variables.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinic_name: Mapped[str] = mapped_column(String(255))
    # The WhatsApp phone_number_id (NOT the phone number).
    phone_number_id: Mapped[str] = mapped_column(String(64), unique=True)
    waba_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # TODO(security): encrypt the access token at rest (Fernet / KMS / Vault).
    #   Stored in plaintext for the MVP only.
    access_token: Mapped[str] = mapped_column(String(512), default="")
    # Feature flag: only call the Precheck service when enabled for this tenant.
    precheck_enabled: Mapped[bool] = mapped_column(default=False)

    # --- Tenant config (edited via the doctor hub; all non-sensitive) ---
    # Literal first-contact greeting (sent verbatim, not improvised by the LLM).
    greeting_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Greeting sent to a patient who has contacted the clinic before (a known
    # patient starting a fresh conversation). Supports a `{{name}}` placeholder
    # substituted with the patient's stored name (or "" when unknown). NULL =
    # returning patients fall through to the normal first-contact path.
    returning_greeting_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional quick-reply buttons appended to the first-contact greeting, e.g.
    #   ["Agendar consulta", "Como funciona?", "Horários e valores"]
    # Max 3 labels, <=20 chars each (WhatsApp reply-button limits). Empty list =
    # plain-text greeting. The label the patient taps becomes their next message.
    greeting_buttons: Mapped[list] = mapped_column(
        JSON, server_default=text("'[]'"), default=list
    )
    # Tone/persona instructions injected into the system prompt (LLM interprets).
    persona_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), server_default="pt-BR", default="pt-BR")
    timezone: Mapped[str] = mapped_column(
        String(64), server_default="America/Sao_Paulo", default="America/Sao_Paulo"
    )
    # Fallback consult duration used when no appointment type applies.
    appointment_duration_min: Mapped[int] = mapped_column(
        Integer, server_default="30", default=30
    )
    # Per-weekday availability windows, e.g.
    #   {"monday": [{"start": "08:00", "end": "12:00"}, ...], "tuesday": [...], ...}
    business_hours: Mapped[dict] = mapped_column(
        JSON, server_default=text("'{}'"), default=dict
    )
    # Bookable reasons, e.g.
    #   [{"name": "Primeira consulta", "description": "...",
    #     "duration_min": 40, "is_active": true, "sort_order": 0}, ...]
    appointment_types: Mapped[list] = mapped_column(
        JSON, server_default=text("'[]'"), default=list
    )
    google_calendar_id: Mapped[str] = mapped_column(
        String(255), server_default="primary", default="primary"
    )
    # Gates whether the bot answers for this tenant. Cannot be true without a
    # connected Calendar + at least one active appointment type and window.
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # Deterministic (zero-LLM) entry flows configuration. Shape, e.g.:
    #   {"enabled": true, "menu_label": "Como posso te ajudar?",
    #    "buttons": ["Serviços e Custo", "Horários", "Outro"]}
    # Empty dict / "enabled" false = the legacy full-LLM path is used.
    initial_flows: Mapped[dict] = mapped_column(
        JSON, server_default=text("'{}'"), default=dict
    )

    # Optional email address for operational alerts (e.g. calendar access lost).
    contact_email: Mapped[str | None] = mapped_column(String(254), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
