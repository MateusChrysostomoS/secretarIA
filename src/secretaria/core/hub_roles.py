"""Role policy for hub callers (TASK-020) — pure, framework-agnostic.

The role reaches secretarIA in brain-api's hub-token introspection body (secretarIA never
decodes the token). brain-api is the identity authority; this module only decides what a
role MAY do inside the hub. Same meaning of "gestor" as brain-api's own portal:

  * `manager`                                          -> clinic manager
  * `doctor` with `is_owner` or `is_manager`           -> clinic manager (the solo-clinic
    owner is exactly `role=doctor, is_owner=True, is_manager=True`)
  * `doctor` without either flag                       -> clinician, NOT a manager
  * `secretary`                                        -> neither, whatever its flags say
  * anything else (`admin`, legacy `tenant_owner`, a differently-cased or invented string,
    an absent role)                                    -> neither. No normalisation.
"""

ROLE_DOCTOR = "doctor"
ROLE_MANAGER = "manager"
ROLE_SECRETARY = "secretary"


def is_clinic_manager(role: str | None, is_owner: bool, is_manager: bool) -> bool:
    """True for `manager`, or for a `doctor` flagged owner/manager. Never for `secretary`."""
    if role == ROLE_MANAGER:
        return True
    return role == ROLE_DOCTOR and (is_owner or is_manager)


def is_clinic_clinician(role: str | None) -> bool:
    """True for `manager` and `doctor`; never for `secretary` or an unknown/absent role."""
    return role in (ROLE_MANAGER, ROLE_DOCTOR)
