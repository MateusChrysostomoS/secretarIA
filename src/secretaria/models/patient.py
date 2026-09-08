"""Patient model - a person messaging a clinic on one of its channels."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

# The only channel that existed before Brain-Message, and the value every
# pre-existing row was backfilled to.
CHANNEL_WHATSAPP = "whatsapp"


def _external_id_default(context: DefaultExecutionContext) -> str | None:
    """Mirror `wa_id` into `external_id` when the caller did not supply one.

    A column-level default rather than a change to the insert call sites, and
    that is the whole point: the two places that build a Patient
    (workers/tasks.py) still pass only `tenant_id`/`wa_id`/`name`, exactly as
    before, and still get a complete, channel-addressable row. Nothing on the
    WhatsApp path had to learn about channels for this migration to land.

    Not reachable for Brain-Message rows: those pass `external_id` explicitly,
    so this default is never consulted for them.
    """
    return context.get_current_parameters().get("wa_id")


class Patient(Base):
    """A patient, identified by (tenant, channel, external_id).

    Historically that identity was (tenant, wa_id) and nothing else - WhatsApp
    was the only way to reach a clinic. `channel` + `external_id` generalise it
    without disturbing it: for every WhatsApp row, past and future,
    `channel == "whatsapp"` and `external_id == wa_id`, so the new key is a
    relabelling of the old one rather than a different one.
    """

    __tablename__ = "patients"
    __table_args__ = (
        # The historical identity. KEPT alongside the channel-aware constraint
        # below, not replaced by it, and deliberately so: it is redundant for
        # every row this model writes (channel="whatsapp" => external_id ==
        # wa_id, so both constraints describe the same tuple), but it is the
        # only thing guarding the mixed-deploy window. secretarIA's API and its
        # arq worker are two EasyPanel services with independent manual
        # deploys, so after this migration a worker still running the
        # pre-channel model inserts a Patient with NO external_id - and a NULL
        # external_id does not bind the constraint below, because NULLs compare
        # distinct. Without this line that window silently allows two rows for
        # one phone number. Drop it only once both services are proven on the
        # new code AND no NULL external_id remains.
        UniqueConstraint("tenant_id", "wa_id", name="uq_patients_tenant_wa_id"),
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_id",
            name="uq_patients_tenant_channel_external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # WhatsApp id == the patient's phone number in E.164-ish digits.
    #
    # NULLABLE as of the Brain-Message channel: a patient who reaches the
    # clinic through the web console has no WhatsApp number at all. The column
    # is only widened here, never renamed or dropped, because a long tail of
    # readers still dereference it directly (plugins/reminders.py,
    # _ReplyContext.patient_wa_id, services/payments/deposit_lifecycle.py, ...)
    # and they keep working untouched: the WhatsApp path still always sets it,
    # and no channel="brain_message" row exists until the pipeline prompt ships.
    # Every one of those readers is inventoried, with the ones that assume
    # non-null called out, in docs/CHECKPOINT_patient_channel_identity.md.
    wa_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    # WHICH surface this patient reaches the clinic on: "whatsapp" (every row
    # that predates this column) or "brain_message".
    channel: Mapped[str] = mapped_column(
        String(32), server_default=CHANNEL_WHATSAPP, default=CHANNEL_WHATSAPP
    )
    # The patient's identifier ON `channel`: wa_id for WhatsApp, a patient
    # session id for Brain-Message (its exact shape is decided in the
    # switchboard prompt, not here - this column only has to hold a string).
    # Filled from wa_id automatically when absent; see _external_id_default.
    #
    # Nullable in the SCHEMA even though no row written by this model leaves it
    # empty. NOT NULL would be the stronger invariant and is the wrong call
    # today: a cross-column DEFAULT is not expressible in Postgres, so NOT NULL
    # would make every INSERT from the not-yet-redeployed worker fail outright
    # - it maps no external_id - and take first contact down for every clinic.
    # Tighten it in the same round that drops uq_patients_tenant_wa_id.
    external_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=_external_id_default
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Explicit opt-out from proactive reminder sends (plugins/reminders.py).
    # Reminders otherwise ride on the booking relationship itself — a patient
    # who booked an appointment has a legitimate expectation of a reminder
    # about it, so no separate opt-IN flow gates sending them. An explicit
    # opt-out, though, is always honored: this is that one override. A full
    # LGPD consent/preference registry (marketing opt-in, channel prefs, ...)
    # is a separate round — TODO(lgpd-consent-registry).
    reminder_opt_out: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # WHEN this subject accepted the Terms of Use / Privacy Policy on WhatsApp,
    # or NULL if they never have. Written once, by the consent gate in
    # workers/tasks.py, when the "✅ Concordo" button is tapped.
    #
    # This is the OPERATIONAL flag the gate reads on every inbound turn; the
    # immutable audit trail is the matching ConsentEvent row
    # (kind="terms_accepted"). Two records on purpose, with different
    # lifetimes: `/dangerously-remove-context` DELETES the Patient row (and so
    # this column) but does NOT delete consent_events, so wiping a patient's
    # context replays a genuine first contact — including being asked again —
    # while the legal record of what they once accepted survives.
    #
    # A timestamp rather than a boolean because LGPD asks WHEN consent was
    # given, not merely whether. `is not None` is the truth test everywhere.
    # Compare PreCheck, which encodes the same thing as a transient
    # `sessions.state` of LGPD_PENDING -> ACTIVE and therefore cannot answer
    # "when did this person agree?" at all.
    lgpd_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The CLINIC's own Asaas customer id for this patient (Asaas accounts are
    # per-tenant — see tenant_credentials.asaas_api_key_encrypted). Reused
    # across deposits so a repeat patient doesn't get a duplicate Asaas
    # customer on every booking. NOT a secret (a foreign-system foreign key,
    # not a credential), so no `_encrypted` suffix.
    asaas_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
