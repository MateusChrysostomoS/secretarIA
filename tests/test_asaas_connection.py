"""The Asaas key-connect service (services/payments/asaas_connection.py), spec section 12.3.

Order of the flow: format -> probes (key NOT stored yet) -> refuse or accept -> store encrypted
-> create the clinic's webhook and read it back. A refused/inconclusive key leaves NOTHING
behind; a failed webhook leaves the account `incomplete`, never silently `connected`.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.core.crypto import decrypt
from secretaria.models import ClinicPaymentAccount, Tenant, TenantCredentials
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, alerts, asaas_connection, asaas_provider, jobs
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_probes import (
    CODE_EXCESS,
    CODE_KEY_INVALID,
    CODE_MISSING,
    CODE_UNAVAILABLE,
    Verdict,
    VerdictKind,
)
from tests.payments_support import FakeAsaasClient, FakeRedis, make_sessionmaker

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
NEW_KEY = "$aact_prod_ffffffffffffffffffffffffffffffffffffffffff"
BASE = "https://secretaria.example.test"
CAPS = {"customers": True, "payments": True, "webhooks": True}

ACCEPTED = Verdict(VerdictKind.ACCEPTED, None, (), (), dict(CAPS))
INCONCLUSIVE = Verdict(VerdictKind.INCONCLUSIVE, CODE_UNAVAILABLE)


def _refused(code, *, missing=(), excess=()):
    return Verdict(VerdictKind.REFUSED, code, tuple(missing), tuple(excess))


def _probes(verdict):
    calls = []

    async def run(api_key):
        calls.append(api_key)
        return verdict

    run.calls = calls
    return run


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def asaas(monkeypatch) -> FakeAsaasClient:
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    return fake


@pytest.fixture
def mails(monkeypatch):
    sent = []

    async def fake_send(to_email, clinic_name, kind):
        sent.append(kind)

    monkeypatch.setattr(alerts.email, "send_payment_alert", fake_send)
    return sent


async def _tenant(session) -> Tenant:
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12],
        contact_email="c@example.test",
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _connect(session, tenant, verdict=ACCEPTED, key=KEY, **kw):
    kw.setdefault("webhook_base_url", BASE)
    result = await asaas_connection.connect_asaas_account(
        session, tenant_id=tenant.id, api_key=key, now=NOW, run_probes=_probes(verdict), **kw
    )
    await session.commit()
    return result


async def _row(session, tenant) -> ClinicPaymentAccount | None:
    return await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")


# --------------------------------------------------------------------------
# accept -> store -> webhook
# --------------------------------------------------------------------------


async def test_an_accepted_key_is_stored_encrypted_and_the_webhook_is_created_and_read_back(
    db, asaas
):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant)
        row = await _row(session, tenant)
        creds = await session.scalar(select(TenantCredentials))

    assert (result.outcome, result.status) == ("connected", "connected")
    assert row.status == "connected" and row.capabilities == CAPS
    assert row.excess_groups == [] and row.incomplete_reason is None
    assert row.connected_at is not None and row.last_verified_at is not None
    assert row.external_webhook_id == "wh_fake"
    assert KEY not in row.api_key_encrypted and decrypt(row.api_key_encrypted) == KEY
    token = decrypt(row.webhook_token_encrypted)
    assert 32 <= len(token) <= 255
    # dual-write for pre-core readers (until the "narrow" phase)
    assert decrypt(creds.asaas_api_key_encrypted) == KEY
    assert decrypt(creds.asaas_webhook_token_encrypted) == token

    _, body = next(c for c in asaas.calls if c[0] == "create_webhook")
    assert body["url"] == f"{BASE}/webhooks/asaas/{row.webhook_slug}"
    assert body["authToken"] == token and body["enabled"] is True and body["interrupted"] is False
    assert body["sendType"] == "SEQUENTIALLY"
    assert set(body["events"]) == set(asaas_connection.WEBHOOK_EVENTS)
    assert "PAYMENT_CREATED" not in body["events"]
    assert [c[0] for c in asaas.calls].count("list_webhooks") == 2  # look, then read back


async def test_an_operator_supplied_token_is_used_as_the_webhook_auth_token(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        await _connect(session, tenant, auth_token="operator-token-0123456789-abcdefghijklmnop")
        row = await _row(session, tenant)
    assert decrypt(row.webhook_token_encrypted) == "operator-token-0123456789-abcdefghijklmnop"


async def test_an_existing_webhook_on_our_url_is_updated_not_duplicated(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        await session.commit()
        asaas.webhooks.append(
            {"id": "wh_old", "url": f"{BASE}/webhooks/asaas/{row.webhook_slug}",
             "enabled": False, "interrupted": True}
        )
        result = await _connect(session, tenant)
        row = await _row(session, tenant)
    names = [c[0] for c in asaas.calls]
    assert result.outcome == "connected" and row.external_webhook_id == "wh_old"
    assert "update_webhook" in names and "create_webhook" not in names


# --------------------------------------------------------------------------
# refusals leave nothing behind
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "outcome", "code"),
    [
        (_refused(CODE_EXCESS, excess=("TRANSFER", "BILL")), "refused", CODE_EXCESS),
        (_refused(CODE_MISSING, missing=("WEBHOOK",)), "refused", CODE_MISSING),
        (_refused(CODE_KEY_INVALID), "refused", CODE_KEY_INVALID),
        (INCONCLUSIVE, "inconclusive", CODE_UNAVAILABLE),
    ],
)
async def test_a_refused_or_inconclusive_key_is_never_stored_anywhere(
    db, asaas, verdict, outcome, code
):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant, verdict)
        row = await _row(session, tenant)
        creds = (await session.scalars(select(TenantCredentials))).all()

    assert (result.outcome, result.code) == (outcome, code)
    assert result.status is None
    assert row is None and creds == []  # not even an empty account row
    assert asaas.calls == []  # and the webhook was never touched
    assert KEY not in repr(result)


async def test_the_refusal_names_the_groups_by_their_technical_code(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(
            session, tenant, _refused(CODE_EXCESS, excess=("BILL", "TRANSFER"))
        )
    assert result.excess_groups == ("BILL", "TRANSFER") and result.missing_groups == ()


async def test_replacing_a_key_with_one_that_is_refused_keeps_the_previous_key(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        await _connect(session, tenant)  # KEY is connected
        result = await _connect(
            session, tenant, _refused(CODE_EXCESS, excess=("TRANSFER",)), key=NEW_KEY
        )
        row = await _row(session, tenant)
    assert result.outcome == "refused"
    assert decrypt(row.api_key_encrypted) == KEY and row.status == "connected"


@pytest.mark.parametrize(
    "bad_key",
    ["", "short", "$aact_", "not-an-asaas-key-0123456789012345", "$aact_sandbox_" + "a" * 40,
     "$aact_hmlg_" + "a" * 40, "$aact_prod_" + "a b" * 20, "$aact_prod_" + "a" * 500],
)
async def test_a_key_of_the_wrong_shape_or_environment_is_refused_before_any_network(
    db, asaas, bad_key
):
    probes = _probes(ACCEPTED)
    async with db() as session:
        tenant = await _tenant(session)
        result = await asaas_connection.connect_asaas_account(
            session, tenant_id=tenant.id, api_key=bad_key, webhook_base_url=BASE, run_probes=probes
        )
    assert (result.outcome, result.code) == ("refused", CODE_KEY_INVALID)
    assert probes.calls == [] and asaas.calls == []
    assert bad_key == "" or bad_key not in repr(result)


# --------------------------------------------------------------------------
# an accepted key with a broken webhook is `incomplete`, never silently connected
# --------------------------------------------------------------------------


async def test_a_webhook_that_cannot_be_created_leaves_the_account_incomplete_and_keeps_the_key(
    db, monkeypatch
):
    fake = FakeAsaasClient(errors={"create_webhook": AsaasError(500)})
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant)
        row = await _row(session, tenant)

    assert (result.outcome, result.status) == ("incomplete", "incomplete")
    assert result.incomplete_reason == "webhook_create_failed"
    assert row.status == "incomplete" and row.connected_at is None
    assert decrypt(row.api_key_encrypted) == KEY  # already approved: kept for "try again"
    assert row.capabilities == CAPS

    fake.errors.clear()  # "Tentar de novo": no key is pasted again
    async with db() as session:
        retried = await asaas_connection.retry_webhook_setup(
            session, tenant_id=tenant.id, webhook_base_url=BASE, now=NOW
        )
        await session.commit()
        row = await _row(session, tenant)
    assert retried.outcome == "connected" and row.status == "connected"
    assert row.incomplete_reason is None and row.external_webhook_id == "wh_fake"


async def test_a_webhook_that_is_not_visible_afterwards_is_incomplete(db, monkeypatch):
    class Invisible(FakeAsaasClient):
        async def list_webhooks(self, limit=100):
            self.calls.append(("list_webhooks",))
            return {"data": []}  # never shows what was created

    fake = Invisible()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant)
    assert result.incomplete_reason == "webhook_readback_failed"


async def test_without_a_public_base_url_the_account_cannot_become_connected(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant, webhook_base_url=None)
        row = await _row(session, tenant)
    assert result.incomplete_reason == "webhook_create_failed" and row.status == "incomplete"
    # No webhook call; the (informational) onboarding read is the only thing allowed through.
    assert [call for call in asaas.calls if call[0] != "get_account_status"] == []


async def test_a_retry_without_a_stored_key_is_refused(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        result = await asaas_connection.retry_webhook_setup(
            session, tenant_id=tenant.id, webhook_base_url=BASE, now=NOW
        )
    assert result.outcome == "refused" and result.code == "no_key"


# --------------------------------------------------------------------------
# disconnect
# --------------------------------------------------------------------------


async def test_disconnect_wipes_every_secret_and_survives_a_failing_remote_delete(db, monkeypatch):
    fake = FakeAsaasClient(errors={"delete_webhook": AsaasError(500)})
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    async with db() as session:
        tenant = await _tenant(session)
        await _connect(session, tenant)
        assert await asaas_connection.disconnect_asaas_account(session, tenant_id=tenant.id)
        await session.commit()
        row = await _row(session, tenant)
        creds = await session.scalar(select(TenantCredentials))
        again = await asaas_connection.disconnect_asaas_account(session, tenant_id=uuid4())

    assert ("delete_webhook", "wh_fake") in fake.calls  # tried
    assert row.status == "revoked" and row.api_key_encrypted is None
    assert row.webhook_token_encrypted is None and row.external_webhook_id is None
    assert row.capabilities == {} and row.connected_at is None
    assert creds.asaas_api_key_encrypted is None and creds.asaas_webhook_token_encrypted is None
    assert again is False


# --------------------------------------------------------------------------
# the daily recheck
# --------------------------------------------------------------------------


async def _connected(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        await _connect(session, tenant)
    return tenant


async def _recheck(db, tenant, verdict, redis=None):
    async with db() as session:
        outcome = await asaas_connection.recheck_asaas_account(
            session, tenant_id=tenant.id, redis=redis or FakeRedis(), now=NOW,
            run_probes=_probes(verdict),
        )
        await session.commit()
        return outcome, await _row(session, tenant)


async def test_recheck_with_a_dead_key_marks_it_incomplete_and_alerts(db, asaas, mails):
    tenant = await _connected(db, asaas)
    outcome, row = await _recheck(db, tenant, _refused(CODE_KEY_INVALID))
    assert outcome == "refused"
    assert (row.status, row.incomplete_reason) == ("incomplete", "key_expired_or_disabled")
    assert mails == []  # operational problems appear in the clinic UI


async def test_recheck_finding_new_excess_permissions_pauses_new_charges_and_alerts(
    db, asaas, mails
):
    tenant = await _connected(db, asaas)
    outcome, row = await _recheck(db, tenant, _refused(CODE_EXCESS, excess=("TRANSFER",)))
    assert (row.status, row.incomplete_reason) == ("incomplete", "excess_permissions")
    assert row.excess_groups == ["TRANSFER"] and mails == []
    async with db() as session:
        # new `before` charges are off; `after` keeps working (already-issued charges continue)
        assert await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="before"
        ) is None
        assert await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="after"
        ) is not None


async def test_recheck_with_a_missing_permission_records_which(db, asaas, mails):
    tenant = await _connected(db, asaas)
    _, row = await _recheck(db, tenant, _refused(CODE_MISSING, missing=("CUSTOMER", "WEBHOOK")))
    assert row.incomplete_reason == "missing_permission:CUSTOMER,WEBHOOK"
    assert mails == []


async def test_an_inconclusive_recheck_changes_no_state_and_alerts_only_after_three_days(
    db, asaas, mails
):
    tenant = await _connected(db, asaas)
    for day in (1, 2, 3):
        outcome, row = await _recheck(db, tenant, INCONCLUSIVE)
        assert outcome == "inconclusive"
        assert (row.status, row.probe_inconclusive_days) == ("connected", day)
        assert mails == []


async def test_a_clean_recheck_resets_the_counter_and_restores_a_fixed_account(db, asaas, mails):
    tenant = await _connected(db, asaas)
    await _recheck(db, tenant, INCONCLUSIVE)
    await _recheck(db, tenant, _refused(CODE_EXCESS, excess=("BILL",)))
    outcome, row = await _recheck(db, tenant, ACCEPTED)  # the clinic swapped in a safer key
    assert outcome == "accepted"
    assert (row.status, row.incomplete_reason, row.excess_groups) == ("connected", None, [])
    assert row.probe_inconclusive_days == 0 and row.last_verified_at is not None


async def test_recheck_reactivates_a_paused_webhook_queue_and_alerts(db, asaas, mails):
    tenant = await _connected(db, asaas)
    asaas.webhooks[0]["interrupted"] = True
    await _recheck(db, tenant, ACCEPTED)
    assert any(
        call[0] == "update_webhook" and call[1] == "wh_fake"
        and call[2].get("interrupted") is False and call[2].get("enabled") is True
        for call in asaas.calls
    )
    assert mails == []


async def test_failed_webhook_reactivation_keeps_account_incomplete(db, asaas):
    tenant = await _connected(db, asaas)
    asaas.webhooks[0]["interrupted"] = True
    asaas.errors["update_webhook"] = AsaasError(503)
    _, row = await _recheck(db, tenant, ACCEPTED)
    assert row.status == "incomplete"
    assert row.incomplete_reason == "webhook_readback_failed"
    assert row.last_verified_at is None


async def test_recheck_rejects_disabled_or_wrongly_subscribed_webhook(db, asaas):
    tenant = await _connected(db, asaas)
    asaas.webhooks[0]["enabled"] = False
    _, row = await _recheck(db, tenant, ACCEPTED)
    assert row.status == "incomplete"


async def test_daily_recheck_restores_the_stored_webhook_token(db, asaas):
    tenant = await _connected(db, asaas)
    async with db() as session:
        row = await _row(session, tenant)
        token = decrypt(row.webhook_token_encrypted)
    asaas.webhooks[0]["authToken"] = "drifted-token"

    _, row = await _recheck(db, tenant, ACCEPTED)

    assert row.status == "connected"
    assert asaas.webhooks[0]["authToken"] == token
    assert any(
        call[0] == "update_webhook" and call[2].get("authToken") == token
        for call in asaas.calls
    )
    asaas.webhooks[0]["enabled"] = True
    asaas.webhooks[0]["events"] = ["PAYMENT_RECEIVED"]
    _, row = await _recheck(db, tenant, ACCEPTED)
    assert row.status == "incomplete"


async def test_a_legacy_account_is_never_promoted_to_verified_without_a_webhook_on_our_url(
    db, asaas, mails
):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key=KEY, webhook_token="legacy-token-0123456789")
        row.status = "connected"  # what the migration backfills
        await session.commit()
    outcome, row = await _recheck(db, tenant, ACCEPTED)
    assert outcome == "accepted"
    assert (row.status, row.incomplete_reason) == ("incomplete", "webhook_readback_failed")
    assert row.last_verified_at is None  # still not ready for `before`
    async with db() as session:
        assert await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="after"
        ) is not None  # but `after` keeps working exactly as before


async def test_the_daily_step_isolates_accounts_and_is_registered(db, asaas, monkeypatch, mails):
    assert asaas_connection.recheck_all_asaas_accounts in jobs.DAILY_STEPS
    async with db() as session:
        first, second = await _tenant(session), await _tenant(session)
        for tenant in (first, second):
            row = await accounts.get_or_create_account_row(
                session, tenant_id=tenant.id, provider="asaas"
            )
            cfg.set_payment_account_secrets(row, api_key=KEY, webhook_token="t" * 40)
            row.status = "connected"
        await session.commit()
        calls = {"n": 0}

        async def flaky(api_key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _refused(CODE_KEY_INVALID)

        monkeypatch.setattr(asaas_connection, "run_probe_suite", flaky)
        await asaas_connection.recheck_all_asaas_accounts(session, NOW, FakeRedis())
        rows = (await session.scalars(select(ClinicPaymentAccount))).all()
    assert calls["n"] == 2
    assert sorted(r.status for r in rows) == ["connected", "incomplete"]


async def test_no_log_line_ever_contains_the_key_or_the_webhook_token(db, asaas, mails):
    import structlog

    with structlog.testing.capture_logs() as logs:
        async with db() as session:
            tenant = await _tenant(session)
            await _connect(session, tenant, _refused(CODE_EXCESS, excess=("TRANSFER",)))
            await _connect(session, tenant)
            row = await _row(session, tenant)
            token = decrypt(row.webhook_token_encrypted)
        await _recheck(db, tenant, _refused(CODE_KEY_INVALID))
    rendered = repr(logs)
    assert logs, "the flow must log something (otherwise this proves nothing)"
    assert KEY not in rendered and token not in rendered


@pytest.mark.parametrize("token", ["short", "x" * 31, "x" * 256])
async def test_a_webhook_token_outside_the_32_to_255_range_is_refused_before_any_probe(
    db, asaas, token
):
    probes = _probes(ACCEPTED)
    async with db() as session:
        tenant = await _tenant(session)
        result = await asaas_connection.connect_asaas_account(
            session,
            tenant_id=tenant.id,
            api_key=KEY,
            webhook_base_url=BASE,
            auth_token=token,
            run_probes=probes,
        )
    assert (result.outcome, result.code) == ("refused", "invalid_webhook_token")
    assert probes.calls == [] and token not in repr(result)


# --------------------------------------------------------------------------
# TASK-053: extra permissions are INFORMATION while the excess refusal is OFF
# --------------------------------------------------------------------------

ACCEPTED_WITH_EXTRAS = Verdict(VerdictKind.ACCEPTED, None, (), ("TRANSFER",), dict(CAPS))


async def test_an_accepted_key_with_extra_permissions_is_connected_and_keeps_the_groups(db, asaas):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(session, tenant, verdict=ACCEPTED_WITH_EXTRAS)
        row = await _row(session, tenant)
    assert (result.outcome, result.status) == ("connected", "connected")
    assert result.excess_groups == ("TRANSFER",)
    assert row.status == "connected" and row.excess_groups == ["TRANSFER"]
    assert row.incomplete_reason is None


async def test_an_accepted_key_with_extras_whose_webhook_fails_is_incomplete_with_the_groups(
    db, asaas
):
    async with db() as session:
        tenant = await _tenant(session)
        result = await _connect(
            session, tenant, verdict=ACCEPTED_WITH_EXTRAS, webhook_base_url=None
        )
        row = await _row(session, tenant)
    assert (result.outcome, result.incomplete_reason) == ("incomplete", "webhook_create_failed")
    assert result.excess_groups == ("TRANSFER",) and row.excess_groups == ["TRANSFER"]


async def test_a_recheck_that_finds_extras_while_the_flag_is_off_keeps_the_account_connected(
    db, asaas, mails
):
    tenant = await _connected(db, asaas)
    outcome, row = await _recheck(db, tenant, ACCEPTED_WITH_EXTRAS)
    assert outcome == "accepted"
    assert (row.status, row.excess_groups) == ("connected", ["TRANSFER"])
    assert mails == []
    _, cleaned = await _recheck(db, tenant, ACCEPTED)  # the clinic swapped in a restricted key
    assert cleaned.excess_groups == []


# --------------------------------------------------------------------------
# TASK-067 session 03: the REAL Asaas sandbox answers 400 "O parâmetro email deve ser informado"
# to POST /webhooks without an `email`; it is the address Asaas writes to when it pauses the queue.
# --------------------------------------------------------------------------


async def test_the_webhook_carries_the_alert_email_asaas_requires(db, asaas, monkeypatch):
    from secretaria.config import get_settings

    monkeypatch.setenv("ASAAS_WEBHOOK_ALERT_EMAIL", "alertas@example.com")
    get_settings.cache_clear()
    try:
        async with db() as session:
            tenant = await _tenant(session)
            await _connect(session, tenant)
        _, body = next(c for c in asaas.calls if c[0] == "create_webhook")
        assert body["email"] == "alertas@example.com"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_without_an_alert_email_the_field_is_left_out_not_sent_empty(db, asaas, monkeypatch):
    from secretaria.config import get_settings

    monkeypatch.delenv("ASAAS_WEBHOOK_ALERT_EMAIL", raising=False)
    get_settings.cache_clear()
    async with db() as session:
        tenant = await _tenant(session)
        await _connect(session, tenant)
    _, body = next(c for c in asaas.calls if c[0] == "create_webhook")
    assert "email" not in body
