"""The permission-probe engine (services/payments/asaas_probes.py), spec section 12.3A.

The safety property under test, before any table: an inconclusive check NEVER accepts a key.
"""

import pytest

from secretaria.services.payments import asaas_probes as probes
from secretaria.services.payments.asaas import ProbeResponse
from secretaria.services.payments.asaas_probes import ProbeKind, Verdict, VerdictKind
from tests.payments_support import FakeAsaasClient

GRANTED = ProbeResponse(200, None, True, None)
DENIED = ProbeResponse(403, "insufficient_permission", False, None)
REQUIRED_PATHS = {spec.path for spec in probes.REQUIRED_PROBES}
QUALIFIED = probes.MINIMUM_EXCESS_GROUPS | {"PIX_TRANSACTION", "SUBSCRIPTION"}


def _path(group: str) -> str:
    every = [*probes.REQUIRED_PROBES, *probes.EXCESS_PROBES]
    return next(spec.path for spec in every if spec.group == group)


def _client(**overrides) -> FakeAsaasClient:
    """A key with exactly the three required groups; `overrides` maps a group to a reply."""
    probe_map = {path: GRANTED for path in REQUIRED_PATHS}
    for group, reply in overrides.items():
        probe_map[_path(group)] = reply
    return FakeAsaasClient(probe_map=probe_map)


class _Sleeps:
    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


async def _run(client, *, qualified=QUALIFIED, refuse_excess=True, **kw) -> Verdict:
    # The matrix below is decision D14 (excess refused); TASK-053 put it behind
    # ASAAS_REFUSE_EXCESS_PERMISSIONS, so these tests ask for it explicitly. The default
    # (flag OFF) is covered at the end of this file.
    return await probes.run_probe_suite(
        "secret-key-value",
        client=client,
        qualified=qualified,
        refuse_excess=refuse_excess,
        sleep=_Sleeps(),
        **kw,
    )


# --------------------------------------------------------------------------
# classification and the pure verdict matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (GRANTED, ProbeKind.GRANTED),
        (ProbeResponse(200, None, False, None), ProbeKind.UNEXPECTED),
        (DENIED, ProbeKind.DENIED),
        (ProbeResponse(403, None, False, None), ProbeKind.FORBIDDEN_OTHER),
        (ProbeResponse(403, "access_denied_ip", False, None), ProbeKind.FORBIDDEN_OTHER),
        (ProbeResponse(401, None, False, None), ProbeKind.KEY_INVALID),
        (ProbeResponse(429, None, False, 2.0), ProbeKind.RATE_LIMITED),
        (ProbeResponse(500, None, False, None), ProbeKind.TRANSIENT),
        (ProbeResponse(503, None, False, None), ProbeKind.TRANSIENT),
        (ProbeResponse(0, "connection_error", False, None), ProbeKind.TRANSIENT),
        (ProbeResponse(400, "invalid_action", False, None), ProbeKind.UNEXPECTED),
        (ProbeResponse(404, None, False, None), ProbeKind.UNEXPECTED),
    ],
)
def test_classify(response, kind):
    assert probes.classify(response) is kind


def test_classify_honours_the_granted_statuses_of_a_candidate_probe():
    not_found = ProbeResponse(404, None, False, None)
    assert probes.classify(not_found, granted_statuses=frozenset({404})) is ProbeKind.GRANTED


G, D = ProbeKind.GRANTED, ProbeKind.DENIED
REQUIRED_OK = {"CUSTOMER": G, "PAYMENT": G, "WEBHOOK": G}


@pytest.mark.parametrize(
    ("required", "excess", "kind", "code", "missing", "extra"),
    [
        # 7: everything as it should be
        (REQUIRED_OK, {"TRANSFER": D, "BILL": D}, VerdictKind.ACCEPTED, None, (), ()),
        # 1: an invalid key wins over everything, even a transient elsewhere
        ({**REQUIRED_OK, "CUSTOMER": ProbeKind.KEY_INVALID}, {"TRANSFER": ProbeKind.TRANSIENT},
         VerdictKind.REFUSED, "key_invalid", (), ()),
        # 2: a restriction that is not a missing permission (IP allowlist, ...)
        ({**REQUIRED_OK, "PAYMENT": ProbeKind.FORBIDDEN_OTHER}, {},
         VerdictKind.REFUSED, "key_restricted", (), ()),
        # 3: a required group is denied
        ({**REQUIRED_OK, "WEBHOOK": D}, {"TRANSFER": D},
         VerdictKind.REFUSED, "missing_permission", ("WEBHOOK",), ()),
        # 4: an excess group is granted
        (REQUIRED_OK, {"TRANSFER": G, "BILL": D, "ANTICIPATION": G},
         VerdictKind.REFUSED, "key_excess_permissions", (), ("ANTICIPATION", "TRANSFER")),
        # 5: both at once
        ({**REQUIRED_OK, "CUSTOMER": D}, {"TRANSFER": G},
         VerdictKind.REFUSED, "key_permissions_mixed", ("CUSTOMER",), ("TRANSFER",)),
        # 6: no definitive refusal but a probe could not be read -> inconclusive, never accepted
        (REQUIRED_OK, {"TRANSFER": D, "BILL": ProbeKind.TRANSIENT},
         VerdictKind.INCONCLUSIVE, "verification_unavailable", (), ()),
        ({**REQUIRED_OK, "PAYMENT": ProbeKind.RATE_LIMITED}, {},
         VerdictKind.INCONCLUSIVE, "verification_unavailable", (), ()),
        (REQUIRED_OK, {"TRANSFER": ProbeKind.UNEXPECTED},
         VerdictKind.INCONCLUSIVE, "verification_unavailable", (), ()),
        # a definitive refusal outranks an inconclusive probe
        (REQUIRED_OK, {"TRANSFER": G, "BILL": ProbeKind.TRANSIENT},
         VerdictKind.REFUSED, "key_excess_permissions", (), ("TRANSFER",)),
        ({**REQUIRED_OK, "WEBHOOK": D}, {"BILL": ProbeKind.RATE_LIMITED},
         VerdictKind.REFUSED, "missing_permission", ("WEBHOOK",), ()),
    ],
)
def test_the_verdict_matrix(required, excess, kind, code, missing, extra):
    verdict = probes.decide(required, excess)
    assert verdict.kind is kind
    assert verdict.code == code
    assert verdict.missing_groups == tuple(sorted(missing))
    assert verdict.excess_groups == tuple(sorted(extra))
    if kind is VerdictKind.ACCEPTED:
        assert verdict.capabilities == {"customers": True, "payments": True, "webhooks": True}


def test_an_accepted_verdict_needs_every_required_group_granted():
    """Even with zero refusals, a required probe that is merely `UNEXPECTED` is inconclusive."""
    verdict = probes.decide({**REQUIRED_OK, "CUSTOMER": ProbeKind.UNEXPECTED}, {"TRANSFER": D})
    assert verdict.kind is VerdictKind.INCONCLUSIVE


# --------------------------------------------------------------------------
# the suite
# --------------------------------------------------------------------------


async def test_a_key_with_exactly_the_three_permissions_is_accepted():
    client = _client()
    verdict = await _run(client)
    assert verdict.kind is VerdictKind.ACCEPTED
    probed = {call[2] for call in client.calls if call[0] == "probe"}
    assert REQUIRED_PATHS <= probed
    assert {_path(g) for g in QUALIFIED} <= probed


async def test_only_qualified_excess_probes_are_ever_executed():
    client = _client()
    await _run(client)
    probed = {call[2] for call in client.calls if call[0] == "probe"}
    assert _path("BILL") in probed  # qualified
    assert _path("CHARGEBACK") not in probed  # not proved in the sandbox: never called


async def test_a_granted_excess_group_refuses_and_names_the_group():
    verdict = await _run(_client(TRANSFER=GRANTED))
    assert verdict.kind is VerdictKind.REFUSED and verdict.code == "key_excess_permissions"
    assert verdict.excess_groups == ("TRANSFER",)


async def test_a_missing_required_permission_refuses_with_its_name():
    verdict = await _run(_client(WEBHOOK=DENIED))
    assert (verdict.code, verdict.missing_groups) == ("missing_permission", ("WEBHOOK",))


async def test_missing_and_excess_together_are_one_mixed_refusal():
    verdict = await _run(_client(CUSTOMER=DENIED, TRANSFER=GRANTED))
    assert verdict.code == "key_permissions_mixed"
    assert (verdict.missing_groups, verdict.excess_groups) == (("CUSTOMER",), ("TRANSFER",))


async def test_an_invalid_key_and_an_ip_restricted_key_are_refused():
    invalid = await _run(_client(CUSTOMER=ProbeResponse(401, None, False, None)))
    restricted = await _run(_client(CUSTOMER=ProbeResponse(403, None, False, None)))
    assert (invalid.code, restricted.code) == ("key_invalid", "key_restricted")


async def test_a_rate_limit_then_success_is_retried_and_accepted():
    sleeps = _Sleeps()
    client = _client(CUSTOMER=[ProbeResponse(429, None, False, None), GRANTED])
    verdict = await probes.run_probe_suite(
        "k", client=client, qualified=QUALIFIED, sleep=sleeps
    )
    assert verdict.kind is VerdictKind.ACCEPTED
    assert sleeps.waits == [0.5]


async def test_retry_after_is_honoured_but_capped_at_five_seconds():
    sleeps = _Sleeps()
    client = _client(
        CUSTOMER=[ProbeResponse(429, None, False, 2.0), GRANTED],
        PAYMENT=[ProbeResponse(429, None, False, 99.0), GRANTED],
    )
    await probes.run_probe_suite("k", client=client, qualified=QUALIFIED, sleep=sleeps)
    assert sorted(sleeps.waits) == [2.0, 5.0]


async def test_a_provider_that_keeps_failing_is_inconclusive_after_two_retries_never_accepted():
    sleeps = _Sleeps()
    down = ProbeResponse(503, None, False, None)
    client = _client(WEBHOOK=down)
    verdict = await probes.run_probe_suite("k", client=client, qualified=QUALIFIED, sleep=sleeps)

    assert verdict.kind is VerdictKind.INCONCLUSIVE and verdict.code == "verification_unavailable"
    webhook_calls = [c for c in client.calls if c[2] == _path("WEBHOOK")]
    assert len(webhook_calls) == 3  # first try + 2 retries
    assert sleeps.waits == [0.5, 1.5]


async def test_a_definitive_refusal_beats_a_transient_failure_elsewhere():
    client = _client(TRANSFER=GRANTED, BILL=ProbeResponse(503, None, False, None))
    verdict = await _run(client)
    assert verdict.code == "key_excess_permissions" and verdict.excess_groups == ("TRANSFER",)


async def test_an_unreadable_200_is_inconclusive_not_granted():
    verdict = await _run(_client(CUSTOMER=ProbeResponse(200, None, False, None)))
    assert verdict.kind is VerdictKind.INCONCLUSIVE


async def test_without_the_four_minimum_probes_qualified_it_refuses_to_decide_and_calls_nothing():
    client = _client()
    verdict = await _run(client, qualified=frozenset({"TRANSFER"}))
    assert verdict.kind is VerdictKind.INCONCLUSIVE and verdict.code == "probes_not_qualified"
    assert client.calls == []  # not one network call: nothing was learnt, nothing is accepted


async def test_at_most_four_probes_run_at_once():
    client = _client()
    client.probe_delay = 0.01
    await _run(client)
    assert 1 < client.probe_max_in_flight <= 4


async def test_the_whole_check_has_a_time_budget_and_a_timeout_is_inconclusive():
    client = _client()
    client.probe_delay = 5.0
    verdict = await _run(client, budget_seconds=0.05)
    assert verdict.kind is VerdictKind.INCONCLUSIVE and verdict.code == "verification_unavailable"


async def test_the_verdict_never_carries_the_key():
    verdict = await _run(_client(TRANSFER=GRANTED))
    assert "secret-key-value" not in repr(verdict)


# --------------------------------------------------------------------------
# the static tables
# --------------------------------------------------------------------------


def test_the_four_minimum_groups_have_a_probe_and_every_group_has_a_label():
    excess = {spec.group for spec in probes.EXCESS_PROBES}
    assert probes.MINIMUM_EXCESS_GROUPS == {
        "TRANSFER",
        "BILL",
        "ANTICIPATION",
        "FINANCIAL_TRANSACTION",
    }
    assert probes.MINIMUM_EXCESS_GROUPS <= excess
    for spec in [*probes.REQUIRED_PROBES, *probes.EXCESS_PROBES]:
        assert probes.GROUP_LABELS[spec.group]
    assert {s.group for s in probes.REQUIRED_PROBES} == {"CUSTOMER", "PAYMENT", "WEBHOOK"}


def test_every_probe_is_a_get_except_the_unqualified_refund_candidate():
    posts = [s for s in probes.EXCESS_PROBES if s.method != "GET"]
    assert [s.group for s in posts] == ["PAYMENT_REFUND"]
    assert posts[0].granted_statuses == frozenset({404})
    assert "PAYMENT_REFUND" not in probes.QUALIFIED_EXCESS_GROUPS  # never on by default


@pytest.mark.xfail(
    reason="RELEASE GATE: F1 does not close until the sandbox proof (Task 3) qualifies the four "
    "minimum probes and Task 22 records them in QUALIFIED_EXCESS_GROUPS",
    strict=False,
)
def test_release_gate_the_minimum_probes_are_qualified():
    assert probes.MINIMUM_EXCESS_GROUPS <= probes.QUALIFIED_EXCESS_GROUPS


async def test_default_module_state_is_fail_closed_until_qualification_is_recorded():
    if probes.MINIMUM_EXCESS_GROUPS <= probes.QUALIFIED_EXCESS_GROUPS:
        pytest.skip("qualification already recorded (Task 22)")
    client = _client()
    verdict = await probes.run_probe_suite("k", client=client, refuse_excess=True, sleep=_Sleeps())
    assert verdict.code == "probes_not_qualified" and client.calls == []


def _settings_with_flag(monkeypatch, value):
    from secretaria.config import get_settings

    if value is None:
        monkeypatch.delenv("ASAAS_PROBES_QUALIFIED", raising=False)
    else:
        monkeypatch.setenv("ASAAS_PROBES_QUALIFIED", value)
    get_settings.cache_clear()
    return get_settings


async def test_the_qualification_flag_defaults_to_off_and_the_default_suite_refuses_to_decide(
    monkeypatch,
):
    get_settings = _settings_with_flag(monkeypatch, None)
    monkeypatch.setattr(probes, "QUALIFIED_EXCESS_GROUPS", frozenset())
    try:
        assert get_settings().ASAAS_PROBES_QUALIFIED is False
        assert probes.effective_qualified_groups() == frozenset()
        client = _client()
        verdict = await probes.run_probe_suite(
            "k", client=client, refuse_excess=True, sleep=_Sleeps()
        )
        assert verdict.code == "probes_not_qualified" and client.calls == []
    finally:
        get_settings.cache_clear()


async def test_the_flag_on_qualifies_the_four_minimum_groups_without_touching_the_module_constant(
    monkeypatch,
):
    get_settings = _settings_with_flag(monkeypatch, "true")
    monkeypatch.setattr(probes, "QUALIFIED_EXCESS_GROUPS", frozenset())
    try:
        assert probes.effective_qualified_groups() == probes.MINIMUM_EXCESS_GROUPS
        assert probes.QUALIFIED_EXCESS_GROUPS == frozenset()  # the flag never edits the record
        verdict = await probes.run_probe_suite(
            "k", client=_client(), refuse_excess=True, sleep=_Sleeps()
        )
        assert verdict.kind is VerdictKind.ACCEPTED

        granted = await probes.run_probe_suite(
            "k", client=_client(TRANSFER=GRANTED), refuse_excess=True, sleep=_Sleeps()
        )
        assert granted.code == "key_excess_permissions" and granted.excess_groups == ("TRANSFER",)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# TASK-053 (spec 2026-10-11 section 7): the excess refusal behind its own flag, OFF by default
# --------------------------------------------------------------------------


def _refusal_flag(monkeypatch, value):
    from secretaria.config import get_settings

    if value is None:
        monkeypatch.delenv("ASAAS_REFUSE_EXCESS_PERMISSIONS", raising=False)
    else:
        monkeypatch.setenv("ASAAS_REFUSE_EXCESS_PERMISSIONS", value)
    monkeypatch.delenv("ASAAS_PROBES_QUALIFIED", raising=False)
    monkeypatch.setattr(probes, "QUALIFIED_EXCESS_GROUPS", frozenset())
    get_settings.cache_clear()
    return get_settings


async def test_the_excess_refusal_flag_defaults_to_off(monkeypatch):
    get_settings = _refusal_flag(monkeypatch, None)
    try:
        assert get_settings().ASAAS_REFUSE_EXCESS_PERMISSIONS is False
    finally:
        get_settings.cache_clear()


async def test_flag_off_a_key_with_extra_permissions_is_accepted_and_the_extras_are_reported(
    monkeypatch,
):
    get_settings = _refusal_flag(monkeypatch, None)
    try:
        verdict = await probes.run_probe_suite(
            "k", client=_client(TRANSFER=GRANTED, BILL=GRANTED), sleep=_Sleeps()
        )
        assert verdict.kind is VerdictKind.ACCEPTED and verdict.code is None
        assert verdict.excess_groups == ("BILL", "TRANSFER")
        assert verdict.capabilities == {"customers": True, "payments": True, "webhooks": True}
    finally:
        get_settings.cache_clear()


async def test_flag_off_needs_no_qualification_and_probes_only_the_four_minimum_groups(monkeypatch):
    get_settings = _refusal_flag(monkeypatch, None)
    try:
        client = _client()
        verdict = await probes.run_probe_suite("k", client=client, sleep=_Sleeps())
        assert verdict.kind is VerdictKind.ACCEPTED and verdict.excess_groups == ()
        probed = {call[2] for call in client.calls if call[0] == "probe"}
        assert {_path(g) for g in probes.MINIMUM_EXCESS_GROUPS} <= probed
        # Never the refund candidate (a POST) nor the unqualified second wave.
        assert _path("CHARGEBACK") not in probed and _path("PAYMENT_REFUND") not in probed
        assert all(call[1] == "GET" for call in client.calls if call[0] == "probe")
    finally:
        get_settings.cache_clear()


async def test_flag_off_still_refuses_a_missing_permission_an_invalid_key_and_an_ip_restriction(
    monkeypatch,
):
    get_settings = _refusal_flag(monkeypatch, None)
    try:
        missing = await probes.run_probe_suite("k", client=_client(WEBHOOK=DENIED), sleep=_Sleeps())
        assert (missing.kind, missing.code, missing.missing_groups) == (
            VerdictKind.REFUSED,
            "missing_permission",
            ("WEBHOOK",),
        )
        dead = ProbeResponse(401, None, False, None)
        dead_key = FakeAsaasClient(probe_map=dict.fromkeys(REQUIRED_PATHS, dead))
        invalid = await probes.run_probe_suite("k", client=dead_key, sleep=_Sleeps())
        assert invalid.code == "key_invalid"
        ip = ProbeResponse(403, "access_denied_ip", False, None)
        restricted = await probes.run_probe_suite("k", client=_client(PAYMENT=ip), sleep=_Sleeps())
        assert restricted.code == "key_restricted"
        # A missing permission together with extras is still just "missing" (no D14 "mixed").
        both = await probes.run_probe_suite(
            "k", client=_client(WEBHOOK=DENIED, TRANSFER=GRANTED), sleep=_Sleeps()
        )
        assert (both.code, both.missing_groups) == ("missing_permission", ("WEBHOOK",))
    finally:
        get_settings.cache_clear()


async def test_flag_off_an_unclear_excess_probe_never_blocks_but_an_unclear_required_one_does(
    monkeypatch,
):
    get_settings = _refusal_flag(monkeypatch, None)
    failing = ProbeResponse(500, None, False, None)
    try:
        good = await probes.run_probe_suite(
            "k",
            client=_client(TRANSFER=failing, BILL=ProbeResponse(403, None, False, None)),
            sleep=_Sleeps(),
        )
        assert good.kind is VerdictKind.ACCEPTED and good.excess_groups == ()
        unclear = await probes.run_probe_suite(
            "k", client=_client(PAYMENT=failing), sleep=_Sleeps()
        )
        assert unclear.kind is VerdictKind.INCONCLUSIVE
        assert unclear.code == "verification_unavailable"
    finally:
        get_settings.cache_clear()


async def test_flag_off_a_timeout_is_inconclusive_and_nothing_carries_the_key(monkeypatch):
    import structlog

    get_settings = _refusal_flag(monkeypatch, None)
    key = "$aact_hmlg_SECRET-should-never-show-0001"
    try:
        slow = FakeAsaasClient(
            probe_map={path: GRANTED for path in REQUIRED_PATHS}, probe_delay=1.0
        )
        with structlog.testing.capture_logs() as logs:
            verdict = await probes.run_probe_suite(
                key, client=slow, sleep=_Sleeps(), budget_seconds=0.05
            )
        assert (verdict.kind, verdict.code) == (
            VerdictKind.INCONCLUSIVE,
            "verification_unavailable",
        )
        assert "SECRET" not in repr(verdict) and "SECRET" not in repr(logs)
    finally:
        get_settings.cache_clear()


async def test_flag_on_from_the_setting_restores_the_d14_refusal(monkeypatch):
    get_settings = _refusal_flag(monkeypatch, "true")
    monkeypatch.setenv("ASAAS_PROBES_QUALIFIED", "true")
    get_settings.cache_clear()
    try:
        verdict = await probes.run_probe_suite(
            "k", client=_client(TRANSFER=GRANTED), sleep=_Sleeps()
        )
        assert verdict.code == "key_excess_permissions" and verdict.excess_groups == ("TRANSFER",)
    finally:
        get_settings.cache_clear()


async def test_flag_on_without_qualification_still_refuses_to_decide(monkeypatch):
    get_settings = _refusal_flag(monkeypatch, "true")
    try:
        client = _client()
        verdict = await probes.run_probe_suite("k", client=client, sleep=_Sleeps())
        assert verdict.code == "probes_not_qualified" and client.calls == []
    finally:
        get_settings.cache_clear()
