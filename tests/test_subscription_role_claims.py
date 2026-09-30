"""TASK-020: `SubscriptionClaim` carries the hub caller's role, parsed defensively.

Token verification itself is covered in test_subscription.py; here only the new optional
role claims: present, absent (a brain-api that predates TASK-020), malformed, and cached.
"""

from uuid import uuid4

import pytest

from secretaria.core import subscription
from secretaria.core.hub_roles import is_clinic_clinician, is_clinic_manager
from secretaria.core.subscription import SubscriptionClaim
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api


@pytest.fixture(autouse=True)
def brain_api(monkeypatch: pytest.MonkeyPatch):
    asked = install_fake_brain_api(monkeypatch)
    yield asked
    reset_fake_brain_api()


async def test_claim_carries_role_and_flags() -> None:
    tenant_id = uuid4()
    token = make_hub_token(tenant_id=tenant_id, role="manager", is_owner=False, is_manager=True)

    claim = await subscription.verify_subscription_token(token)

    assert claim == SubscriptionClaim(
        tenant_id=tenant_id, active=True, role="manager", is_owner=False, is_manager=True
    )


async def test_body_without_role_keys_is_the_old_claim_shape() -> None:
    """A brain-api that predates TASK-020: same claim as before, role None."""
    tenant_id = uuid4()
    token = make_hub_token(tenant_id=tenant_id, role=None)

    claim = await subscription.verify_subscription_token(token)

    assert claim == SubscriptionClaim(tenant_id=tenant_id, active=True)
    assert claim.role is None


@pytest.mark.parametrize("bad_role", [123, ["manager"], {"role": "manager"}, True, "", "m" * 33])
async def test_malformed_role_reads_as_absent(bad_role: object) -> None:
    token = make_hub_token(
        tenant_id=uuid4(), role="manager", is_manager=True, body_overrides={"role": bad_role}
    )

    claim = await subscription.verify_subscription_token(token)

    assert claim is not None
    assert claim.role is None


@pytest.mark.parametrize("bad_flag", ["true", 1, "yes", None, [True]])
async def test_non_boolean_flags_read_as_false(bad_flag: object) -> None:
    token = make_hub_token(
        tenant_id=uuid4(),
        role="doctor",
        body_overrides={"is_owner": bad_flag, "is_manager": bad_flag},
    )

    claim = await subscription.verify_subscription_token(token)

    assert claim is not None
    assert claim.is_owner is False
    assert claim.is_manager is False


async def test_inactive_answer_is_still_none_even_with_a_role() -> None:
    token = make_hub_token(tenant_id=uuid4(), role="manager", is_manager=True, active=False)

    assert await subscription.verify_subscription_token(token) is None


async def test_role_survives_the_positive_cache(brain_api: list[str]) -> None:
    tenant_id = uuid4()
    token = make_hub_token(tenant_id=tenant_id, role="doctor", is_owner=True)

    first = await subscription.verify_subscription_token(token)
    second = await subscription.verify_subscription_token(token)

    assert first == second
    assert second is not None and second.role == "doctor" and second.is_owner is True
    assert len(brain_api) == 1


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager", "manager_ok", "clinician_ok"),
    [
        ("manager", False, False, True, True),
        ("manager", True, True, True, True),
        ("doctor", False, False, False, True),
        ("doctor", True, False, True, True),
        ("doctor", False, True, True, True),
        ("doctor", True, True, True, True),
        ("secretary", False, False, False, False),
        ("secretary", True, True, False, False),
        ("admin", True, True, False, False),
        ("tenant_owner", True, True, False, False),
        ("Manager", True, True, False, False),
        ("superadmin", True, True, False, False),
        (None, True, True, False, False),
        ("", True, True, False, False),
    ],
)
def test_role_policy_truth_table(role, is_owner, is_manager, manager_ok, clinician_ok) -> None:
    assert is_clinic_manager(role, is_owner, is_manager) is manager_ok
    assert is_clinic_clinician(role) is clinician_ok
