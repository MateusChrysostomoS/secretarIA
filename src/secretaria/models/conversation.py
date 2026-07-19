"""Conversation model - one ongoing thread between a patient and a clinic."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class HandoverState(enum.StrEnum):
    """Who currently owns the conversation.

    BOT_ACTIVE   - the AI may answer automatically.
    HUMAN_ACTIVE - a human secretary is handling it; the bot stays silent.
    """

    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"


class FlowState(enum.StrEnum):
    """Which deterministic (zero-LLM) flow the conversation is currently in.

    IDLE            - no active flow; the next inbound opens the menu when
                      the tenant has `initial_flows.enabled`.
    MENU            - the menu buttons were sent; awaiting a tap.
    SERVICE_CATALOG - patient is inside the multi-step service booking flow.
    MANAGE_BOOKING  - patient is inside the cancel/reschedule flow (pick an
                      existing appointment, then cancel or move it).
    BUSINESS_HOURS  - patient asked for hours (one-shot, returns to IDLE).
    LLM             - full LLM mode (patient chose "Outro" or deviated); the
                      conversation stays here until a /menu reset.
    """

    IDLE = "IDLE"
    MENU = "MENU"
    SERVICE_CATALOG = "SERVICE_CATALOG"
    MANAGE_BOOKING = "MANAGE_BOOKING"
    BUSINESS_HOURS = "BUSINESS_HOURS"
    LLM = "LLM"


def _handover_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class Conversation(Base):
    """A patient <-> clinic conversation and its handover state."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "patient_id", name="uq_conversations_tenant_patient"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    handover_state: Mapped[HandoverState] = mapped_column(
        SAEnum(
            HandoverState,
            native_enum=False,
            length=32,
            values_callable=_handover_values,
        ),
        default=HandoverState.BOT_ACTIVE,
    )
    last_human_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_bot_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- Deterministic flow engine state (Track 2) ---
    # Stored as VARCHAR-backed enum to match handover_state (non-native).
    flow_state: Mapped[FlowState] = mapped_column(
        SAEnum(
            FlowState,
            native_enum=False,
            length=32,
            values_callable=_handover_values,
        ),
        server_default=FlowState.IDLE.value,
        default=FlowState.IDLE,
    )
    # Sub-step within the active flow (e.g. "show_services", "awaiting_day").
    flow_step: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Appointment type name the patient picked inside SERVICE_CATALOG.
    flow_selected_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # ISO date string the patient indicated (resolved from free text).
    flow_selected_day: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ISO datetime of the slot the patient chose.
    flow_selected_slot: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Which professional the patient picked inside the multi-doctor
    # SERVICE_CATALOG branch (services/flow_router.py). SET NULL so removing a
    # professional never breaks an ongoing conversation row.
    flow_selected_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("professionals.id", ondelete="SET NULL"), nullable=True
    )
    # Convênio label picked (or typed) at the insurance step; copied onto the
    # Appointment at booking time. Free text, not a FK — tenants.insurances is
    # a JSON list of plan names, there is no Insurance table.
    flow_selected_insurance: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Set while a returning patient is mid-"quer continuar?" prompt: holds the
    # FlowState value to resume to AND marks that the next inbound is the Sim/Não
    # answer. NULL whenever no reactivation prompt is pending.
    reactivation_origin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
