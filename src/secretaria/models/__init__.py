"""ORM models.

Importing this package registers every table on `Base.metadata`, which is
what Alembic autogenerate and the test fixtures rely on.
"""

from secretaria.models.conversation import Conversation, HandoverState
from secretaria.models.message import Message, MessageDirection, MessageSender
from secretaria.models.patient import Patient
from secretaria.models.processed_event import ProcessedEvent
from secretaria.models.tenant import Tenant

__all__ = [
    "Conversation",
    "HandoverState",
    "Message",
    "MessageDirection",
    "MessageSender",
    "Patient",
    "ProcessedEvent",
    "Tenant",
]
