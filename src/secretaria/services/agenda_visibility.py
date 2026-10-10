"""Who may see which appointments in the hub agenda (TASK-044 R7, spec 2026-10-09 §5.A).

brain-api is the identity authority: its hub-token introspection says whether the person
behind the hub session sees the whole clinic ("clinic": owner/manager, secretary) or only
their own appointments ("own": any other doctor), plus the secretarIA professional they
are. This module turns that answer into ONE value the hub routes consult
(api/hub/deps.py::get_agenda_viewer); it never re-derives a role.

Rollout - brain-api may still be on the version without `agenda_scope`: a session whose
token carries a professional is treated as "own" (fail closed: a doctor never sees more
than his own because a deploy is late); a session without one keeps today's clinic-wide
view (a receptionist or a manager without a professional row has no own agenda to be
narrowed to, and locking them out would stop the clinic's reception).
"""

from dataclasses import dataclass
from uuid import UUID

from secretaria.core.subscription import AGENDA_SCOPE_CLINIC, AGENDA_SCOPE_OWN


@dataclass(frozen=True)
class AgendaViewer:
    """The hub session's agenda visibility.

    `professional_id` is a Professional row of THIS clinic (the dependency drops any
    other), or None when the person is not a professional here.
    """

    scope: str
    professional_id: UUID | None = None

    @property
    def restricted(self) -> bool:
        """True = only the appointments of `professional_id` exist for this viewer."""
        return self.scope != AGENDA_SCOPE_CLINIC

    @property
    def can_filter_own(self) -> bool:
        """A clinic-wide viewer who is also a professional: the "Todos / Só os meus" switch."""
        return not self.restricted and self.professional_id is not None

    def sees(self, professional_id: UUID | None) -> bool:
        """Whether an appointment owned by `professional_id` exists for this viewer."""
        if not self.restricted:
            return True
        return self.professional_id is not None and professional_id == self.professional_id


CLINIC_WIDE = AgendaViewer(scope=AGENDA_SCOPE_CLINIC)


def viewer_from_claim(agenda_scope: str | None, professional_id: UUID | None) -> AgendaViewer:
    """brain-api's answer -> the viewer. `agenda_scope` None = brain-api did not say."""
    if agenda_scope == AGENDA_SCOPE_CLINIC:
        return AgendaViewer(scope=AGENDA_SCOPE_CLINIC, professional_id=professional_id)
    if agenda_scope == AGENDA_SCOPE_OWN or professional_id is not None:
        return AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=professional_id)
    return CLINIC_WIDE
