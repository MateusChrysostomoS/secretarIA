"""ORM models.

Importing this package registers every table on `Base.metadata`, which is
what Alembic autogenerate and the test fixtures rely on.
"""

from secretaria.models.analytics_event import AnalyticsEvent
from secretaria.models.appointment import Appointment, AppointmentStatus
from secretaria.models.consent_event import ConsentEvent
from secretaria.models.conversation import Conversation, FlowState, HandoverState
from secretaria.models.message import Message, MessageDirection, MessageSender
from secretaria.models.patient import Patient
from secretaria.models.processed_event import ProcessedEvent
from secretaria.models.professional import Professional
from secretaria.models.tenant import Tenant
from secretaria.models.tenant_credentials import TenantCredentials
from secretaria.models.unit import Unit

__all__ = [
    "AnalyticsEvent",
    "Appointment",
    "AppointmentStatus",
    "ConsentEvent",
    "Conversation",
    "FlowState",
    "HandoverState",
    "Message",
    "MessageDirection",
    "MessageSender",
    "Patient",
    "ProcessedEvent",
    "Professional",
    "Tenant",
    "TenantCredentials",
    "Unit",
]
