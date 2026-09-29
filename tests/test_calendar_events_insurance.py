"""Agenda read model: the convênio of each appointment (TASK-014, spec TASK E).

Ground truth: api/hub/calendar.py::list_events, schemas/calendar.py::CalendarEventRead,
services/insurance_catalog.py::load_appointment_plans. Contract for the consumer:
docs/CHECKPOINT_convenio_catalogo.md section 11.

Fixture shape mirrors tests/test_cancellation_notice.py (fake Calendar, in-memory
SQLite) so no Google or Redis call is ever attempted.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.schemas.calendar import (  # noqa: E402
    CalendarDepositRead,
    CalendarEventRead,
    CalendarInsurancePlanRead,
)

LEGACY_EVENT_KEYS = {"id", "summary", "start", "end", "appointment_id"}


# ---------------------------------------------------------------------------
# Schema — additive; plan never wider than {id, name, charge_deposit},
# deposit never wider than {status, amount_cents}
# ---------------------------------------------------------------------------


def test_a_legacy_event_gains_three_null_fields_and_loses_nothing():
    event = CalendarEventRead(id="g1", summary="Consulta", start="s", end="e")

    dumped = event.model_dump()

    assert set(dumped) == LEGACY_EVENT_KEYS | {"insurance", "insurance_plan", "deposit"}
    assert dumped["insurance"] is None
    assert dumped["insurance_plan"] is None
    assert dumped["deposit"] is None
    assert dumped["appointment_id"] is None


def test_the_plan_wire_carries_exactly_three_keys():
    """custom_payment_note / mechanism / note must never be widened into this
    shape by accident: the agenda shows a name and a flag, nothing else."""
    assert set(CalendarInsurancePlanRead.model_fields) == {"id", "name", "charge_deposit"}


def test_the_deposit_wire_carries_exactly_two_keys():
    """The Pix copy-paste payload, the Asaas payment id and the patient stay
    server-side: the agenda shows the STATE of the deposit and its amount.

    This is the wire of TODAY. Spec F (section 10A.5) deliberately adds a third
    key, `needs_attention`, in TASK-017, which then updates THIS assertion on
    purpose; nothing else may widen it (never the provider charge id, the
    fulfillment_status column itself, the booking snapshot or the payer)."""
    assert set(CalendarDepositRead.model_fields) == {"status", "amount_cents"}
