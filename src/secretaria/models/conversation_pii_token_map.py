"""Per-conversation PII token map - the re-identification key for one thread.

Mirrors PreCheck's `pii_token_maps` (app/models/pii_token_map.py) in ROLE, but
is keyed and lived differently, and both differences are deliberate:

* **Keyed by `conversation_id`, not by a one-shot submission id.** PreCheck
  masks one report and re-hydrates it minutes later; this pipeline is a
  conversation that comes back tomorrow. `Pseudonymizer(tokens=...)` was built
  to be re-seeded exactly for this - a patient who writes their phone on Monday
  and again on Friday has to get the SAME token both times, or the model reads
  two strangers where there is one person.

* **The row SURVIVES the turn.** PreCheck deletes its map on successful
  re-hydration, because there the map is a second, otherwise-nonexistent copy of
  the name<->session link. Here it is not: `messages.body` already stores the
  patient's own words, in the clear, for the life of the conversation. Deleting
  this row would therefore protect nothing and break token stability across
  turns. It dies with the conversation instead - hence ON DELETE CASCADE, which
  also means `/dangerously-remove-context` (which deletes the Patient, cascading
  to conversations) takes this with it, with no extra wiring.

No tenant column: the FK to `conversations` carries the tenant transitively, and
a second copy would be one more thing to keep honest.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ConversationPiiTokenMap(Base):
    """`{token: valor real}` for one conversation, grown turn by turn."""

    __tablename__ = "conversation_pii_token_maps"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    # Plain JSON (not the JSONB variant PreCheck uses): every other JSON column
    # in this repo is plain JSON (tenants.business_hours, tenants.insurances),
    # the test suite runs on SQLite, and nothing queries INSIDE this blob - it
    # is read whole and written whole.
    tokens: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
