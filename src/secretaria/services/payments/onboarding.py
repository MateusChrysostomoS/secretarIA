"""The Asaas onboarding status of a clinic account (GET /myAccount/status), TASK-071 Plan 4.

Asaas answers four independent states: `general`, `documentation`, `commercialInfo` and
`bankAccountInfo`, each PENDING | APPROVED | REJECTED | AWAITING_APPROVAL. Anything else - a value
Asaas adds tomorrow, a missing field, a non-string - is `UNKNOWN`, never approved: a green tick
must come from a known "APPROVED". A key that cannot read the status (403) or an answer that
carries none of the four fields is "unreadable" (`None`), which is not an error: the screen then
asks the manager to confirm the registration in the Asaas panel instead of claiming anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

OnboardingState = Literal["PENDING", "APPROVED", "REJECTED", "AWAITING_APPROVAL", "UNKNOWN"]
_KNOWN: frozenset[str] = frozenset({"PENDING", "APPROVED", "REJECTED", "AWAITING_APPROVAL"})
_APPROVED = "APPROVED"


def _state(value: object) -> OnboardingState:
    return value if isinstance(value, str) and value in _KNOWN else "UNKNOWN"  # type: ignore[return-value]


@dataclass(frozen=True)
class AccountOnboarding:
    general: OnboardingState
    documents: OnboardingState
    commercial: OnboardingState
    bank: OnboardingState

    @property
    def all_approved(self) -> bool:
        return all(
            state == _APPROVED
            for state in (self.general, self.documents, self.commercial, self.bank)
        )


def parse_account_status(payload: Mapping | None) -> AccountOnboarding | None:
    """The four states of a /myAccount/status body. `None` for a payload that is not a mapping
    or carries none of the four fields (an unexpected answer is "unreadable", never approved);
    a missing field next to present ones, or a value outside the four known, is `UNKNOWN`."""
    if not isinstance(payload, Mapping):
        return None
    fields = ("general", "documentation", "commercialInfo", "bankAccountInfo")
    if not any(field in payload for field in fields):
        return None
    return AccountOnboarding(
        general=_state(payload.get("general")),
        documents=_state(payload.get("documentation")),
        commercial=_state(payload.get("commercialInfo")),
        bank=_state(payload.get("bankAccountInfo")),
    )


async def read_onboarding(client) -> AccountOnboarding | None:
    """Ask Asaas for the onboarding status; any failure (403 for a key without that permission,
    5xx, network, unexpected body) is `None` and is logged by error type only."""
    try:
        payload = await client.get_account_status()
    except Exception as exc:  # noqa: BLE001 - unreadable is a state, never an error
        logger.info("asaas_onboarding_unreadable", error_type=type(exc).__name__)
        return None
    return parse_account_status(payload)


def store_onboarding(row, onboarding: AccountOnboarding | None, now: datetime) -> None:
    """Write the states on the account row (all None when unreadable) and stamp the check."""
    row.onboarding_general = onboarding.general if onboarding else None
    row.onboarding_docs = onboarding.documents if onboarding else None
    row.onboarding_commercial = onboarding.commercial if onboarding else None
    row.onboarding_bank = onboarding.bank if onboarding else None
    row.onboarding_checked_at = now


def stored_onboarding(row) -> AccountOnboarding | None:
    """The states kept on the row, or `None` when they were never readable."""
    if row.onboarding_general is None and row.onboarding_docs is None:
        return None
    return AccountOnboarding(
        general=_state(row.onboarding_general),
        documents=_state(row.onboarding_docs),
        commercial=_state(row.onboarding_commercial),
        bank=_state(row.onboarding_bank),
    )
