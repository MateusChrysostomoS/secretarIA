"""Shared test helpers for the payment provider core (TASK-016).

Not a test module (no `test_` prefix). Fakes here are SUPERSETS of what any one test needs:
a call the code makes that a fake does not model fails loudly instead of being ignored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base
from secretaria.services.payments.provider import NormalizedPaymentEvent, PaymentStatus


async def make_sessionmaker():
    """A fresh in-memory SQLite database with every table; returns (engine, sessionmaker)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class FakeAsaasClient:
    """Records every call; every failure is injectable per method name via `errors`.

    `refund_payment` exists ONLY to fail the test that reaches it: no code path may return
    money (decision D15), so any call is a bug, not a scenario.
    """

    def __init__(
        self,
        *,
        customer_id: str = "cus_fake",
        payment_id: str = "pay_fake",
        qr_payload: str | None = "00020126copiaecola6304ABCD",
        payment: dict | None = None,
        webhooks: list[dict] | None = None,
        errors: dict[str, Exception] | None = None,
        probe_map: dict | None = None,
        probe_delay: float = 0.0,
        account_status: dict | None = None,
    ) -> None:
        # GET /myAccount/status body; the default is a fully approved account.
        self.account_status = (
            account_status
            if account_status is not None
            else {
                "general": "APPROVED",
                "documentation": "APPROVED",
                "commercialInfo": "APPROVED",
                "bankAccountInfo": "APPROVED",
            }
        )
        self.customer_id = customer_id
        self.payment_id = payment_id
        self.qr_payload = qr_payload
        self.payment = payment if payment is not None else {}
        self.webhooks = webhooks if webhooks is not None else []
        self.errors = errors or {}
        self.calls: list[tuple] = []
        # path -> ProbeResponse | [ProbeResponse, ...] (consumed in order, the last repeats).
        # A path not listed answers 403 insufficient_permission, like a key without that group.
        self.probe_map = probe_map or {}
        self.probe_delay = probe_delay
        self.probe_in_flight = 0
        self.probe_max_in_flight = 0

    def _maybe_raise(self, name: str) -> None:
        if name in self.errors:
            raise self.errors[name]

    async def create_customer(self, name, mobile_phone, cpf_cnpj=None):
        self.calls.append(("create_customer", name, mobile_phone, cpf_cnpj))
        self._maybe_raise("create_customer")
        return self.customer_id

    async def create_pix_payment(
        self, customer_id, value_cents, external_reference, description, due_date
    ):
        self.calls.append(
            ("create_pix_payment", customer_id, value_cents, external_reference, due_date)
        )
        self._maybe_raise("create_pix_payment")
        return {"id": self.payment_id, "status": "PENDING"}

    async def get_pix_qr(self, payment_id):
        self.calls.append(("get_pix_qr", payment_id))
        self._maybe_raise("get_pix_qr")
        payload = {"encodedImage": "b64", "expirationDate": "2026-09-30 23:59:59"}
        if self.qr_payload is not None:
            payload["payload"] = self.qr_payload
        return payload

    async def get_payment(self, payment_id):
        self.calls.append(("get_payment", payment_id))
        self._maybe_raise("get_payment")
        return dict(self.payment)

    async def delete_payment(self, payment_id):
        self.calls.append(("delete_payment", payment_id))
        self._maybe_raise("delete_payment")
        return {"deleted": True, "id": payment_id}

    async def delete_customer(self, customer_id):
        self.calls.append(("delete_customer", customer_id))
        self._maybe_raise("delete_customer")
        return {"deleted": True}

    async def get_account_status(self):
        self.calls.append(("get_account_status",))
        self._maybe_raise("get_account_status")
        return dict(self.account_status)

    async def list_webhooks(self, limit=100):
        self.calls.append(("list_webhooks",))
        self._maybe_raise("list_webhooks")
        return {"data": list(self.webhooks)}

    async def create_webhook(self, body):
        self.calls.append(("create_webhook", dict(body)))
        self._maybe_raise("create_webhook")
        created = {
            "id": "wh_fake",
            "url": body.get("url"),
            "enabled": body.get("enabled", True),
            "interrupted": False,
            "events": list(body.get("events", [])),
        }
        self.webhooks.append(created)
        return created

    async def update_webhook(self, webhook_id, body):
        self.calls.append(("update_webhook", webhook_id, dict(body)))
        self._maybe_raise("update_webhook")
        for hook in self.webhooks:
            if hook.get("id") == webhook_id:
                hook.update(body)
        return {"id": webhook_id}

    async def delete_webhook(self, webhook_id):
        self.calls.append(("delete_webhook", webhook_id))
        self._maybe_raise("delete_webhook")
        self.webhooks[:] = [h for h in self.webhooks if h.get("id") != webhook_id]
        return {"deleted": True}

    async def probe(self, path, *, method="GET", expect_list=False, timeout=5.0):
        from secretaria.services.payments.asaas import ProbeResponse

        self.calls.append(("probe", method, path))
        self.probe_in_flight += 1
        self.probe_max_in_flight = max(self.probe_max_in_flight, self.probe_in_flight)
        try:
            if self.probe_delay:
                await asyncio.sleep(self.probe_delay)
        finally:
            self.probe_in_flight -= 1
        reply = self.probe_map.get(path, ProbeResponse(403, "insufficient_permission", False, None))
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return reply

    async def refund_payment(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("refund_payment was called: the platform never refunds (D15)")


class FakeRedis:
    """The few Redis calls the payments code makes (SET NX EX, INCR, EXPIRE, GET, DEL)."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
            self.ttls.pop(key, None)
        return removed


class FakeArqPool(FakeRedis):
    """An arq pool: Redis calls plus a recorded `enqueue_job`."""

    def __init__(self, *, fail_enqueue: bool = False) -> None:
        super().__init__()
        self.jobs: list[tuple[str, tuple, dict]] = []
        self.fail_enqueue = fail_enqueue

    async def enqueue_job(self, name, *args, **kwargs):
        if self.fail_enqueue:
            raise ConnectionError("redis down")
        self.jobs.append((name, args, kwargs))


def recording_enqueue() -> tuple[list[tuple[str, str]], Callable[[str, str], Awaitable[None]]]:
    """An `enqueue(name, argument)` callable (what the jobs take) and the list it appends to."""
    sent: list[tuple[str, str]] = []

    async def enqueue(name: str, argument: str) -> None:
        sent.append((name, argument))
        await asyncio.sleep(0)

    return sent, enqueue


class FakeProvider:
    """A `PaymentProvider` with scripted answers, registered with
    `monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)`."""

    def __init__(self, *, name: str = "asaas", state=None, get_error=None, cancel_error=None):
        self.name = name
        self.state = state
        self.get_error = get_error
        self.cancel_error = cancel_error
        self.created_charge = None
        self.create_error = None
        self.calls: list[tuple] = []
        self.tenants_seen: list = []  # whose credentials each call used

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        if self.create_error:
            raise self.create_error
        if isinstance(self.created_charge, list):  # one scripted answer per call
            return self.created_charge.pop(0)
        return self.created_charge

    def verify_webhook(self, account, headers, raw_body, query=None):
        return True

    def parse_webhook(self, raw_body, headers=None):
        return []

    async def get_charge(self, account, provider_charge_id):
        self.calls.append(("get_charge", provider_charge_id))
        self.tenants_seen.append(account.tenant_id)
        if self.get_error:
            raise self.get_error
        if isinstance(self.state, dict):  # per-charge scripted answers
            return self.state[provider_charge_id]
        return self.state

    async def cancel_charge(self, account, provider_charge_id):
        self.calls.append(("cancel_charge", provider_charge_id))
        self.tenants_seen.append(account.tenant_id)
        if self.cancel_error:
            raise self.cancel_error

    def called(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)


def make_event(
    status: PaymentStatus = PaymentStatus.PAID,
    *,
    event_id: str = "evt_1",
    charge_id: str = "pay_1",
    amount: int | None = 6000,
    reference: str | None = "dep-ref-1",
    event_type: str = "PAYMENT_RECEIVED",
    provider: str = "asaas",
) -> NormalizedPaymentEvent:
    """A normalised webhook event with the defaults every payment test shares."""
    return NormalizedPaymentEvent(
        provider=provider,
        event_id=event_id,
        provider_charge_id=charge_id,
        status=status,
        paid_amount_cents=amount,
        external_reference=reference,
        event_type=event_type,
    )
