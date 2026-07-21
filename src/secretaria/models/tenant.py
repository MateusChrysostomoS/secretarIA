"""Tenant model - one clinic, with its own WhatsApp Business credentials."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Tenant(Base):
    """A clinic using SecretarIA.

    The system is multi-tenant in the data model. For the MVP a single tenant
    is configured via environment variables.
    """

    __tablename__ = "tenants"
    __table_args__ = (
        # Onboarding (brain-api-mediated) creates a tenant row BEFORE its
        # WhatsApp number is connected, so phone_number_id is nullable and
        # many tenants may simultaneously be NULL (not yet connected).
        # Uniqueness therefore only applies to CONNECTED tenants - a plain
        # UniqueConstraint would reject a second NULL. Both postgresql_where
        # (prod/dev Postgres) and sqlite_where (the in-memory test engine,
        # Base.metadata.create_all) are set so the partial index is honored
        # on whichever dialect actually creates the table.
        Index(
            "uq_tenants_phone_number_id_not_null",
            "phone_number_id",
            unique=True,
            postgresql_where=text("phone_number_id IS NOT NULL"),
            sqlite_where=text("phone_number_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinic_name: Mapped[str] = mapped_column(String(255))
    # The WhatsApp phone_number_id (NOT the phone number). NULL until the
    # tenant completes the embedded-signup/Coexistence connection step - see
    # workers/tasks.py:_resolve_tenant, which never matches a NULL row.
    phone_number_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    waba_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The WhatsApp access token does NOT live here: it is Fernet-encrypted at rest as
    # `tenant_credentials.waba_token_encrypted` (tenant-secrets-encryption skill) and
    # decrypted ONLY via services/tenant_config.get_waba_token.
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
    # Copy the secretary will send after a consult. Sending itself is wired by
    # a later feature - this column is only persisted + surfaced (doctor hub)
    # for now, no runtime use.
    post_consult_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-text reference knowledge the LLM MAY consult to answer post-consult
    # questions (recovery care, return-visit norms, how exam results are
    # delivered). Injected into the system prompt only on qualifying turns -
    # see ai/prompts.py::_format_post_consult_knowledge.
    post_consult_knowledge: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Which EHR provider (plugins/ehr.py's PROVIDERS registry key, e.g.
    # "iclinic") this tenant pushes booked appointments to. NULL = the `ehr`
    # addon's post_booking hook no-ops (no provider selected yet).
    ehr_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The clinic's OWN Pix key (email/phone/random key/CPF-CNPJ) used to
    # receive deposit payments (plugins/pix_whatsapp.py). This is a PUBLIC
    # payment address, not a secret credential — unlike the encrypted
    # tenant_credentials columns, it lives here in plaintext. Still never
    # logged in full (only tenant_id is logged around it), out of caution.
    # NULL = the `pix_whatsapp` addon's post_booking hook silently no-ops.
    pix_key: Mapped[str | None] = mapped_column(String(140), nullable=True)

    # --- Onboarding / multi-professional (cross-service contract v1) ---
    # When the WhatsApp number was connected (phone_number_id/waba_id set).
    # NULL until then. Set by the brain-api-mediated onboarding bridge, not by
    # this service directly.
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When Coexistence mode was confirmed resolved (webhook `history` /
    # `smb_app_state_sync` / `smb_message_echoes` field observed - see
    # api/webhook.py's process_webhook_event). NULL until then.
    mode_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # "none" | "in_progress" | "done" - progress of the WhatsApp `history` sync
    # sent once on Coexistence connect. Plain string (no native enum), same
    # convention as the rest of this service's status-ish columns.
    history_sync_status: Mapped[str] = mapped_column(
        String(20), server_default="none", default="none"
    )
    history_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Clinic's physical address, e.g.
    #   {"line": "...", "complement": "...", "neighborhood": "...",
    #    "city": "...", "state": "...", "postal_code": "..."}
    # NULL = not collected yet.
    address: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Accepted health-insurance plan names, e.g. ["Unimed", "Amil"]. NULL/empty
    # = the clinic does not take insurance, or hasn't configured the list yet.
    insurances: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Whether the bot should ask patients for their insurance during booking.
    collect_insurance: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
