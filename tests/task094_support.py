"""Shared harness for the TASK-094 tests (production battery 090+091+093, D4-D7).

The real turn pipeline (`tasks._persist_inbound_message` + `tasks._send_bot_reply`) on an
in-memory SQLite database; the channel client, the entitlement read, the mail transport and
the MODEL are fakes - the same pattern as tests/test_handoff_insurance_payment.py. `agent`
holds what the fake `run_agent` answers next (a plain string, exactly what the real one
returns) and records the keyword arguments of every call.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.models.unit import Unit  # noqa: E402
from secretaria.services import (  # noqa: E402
    handoff_notification as hn,
    pii_pseudonymization as pii_store,
)
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.unit_address import UnitAddressParts, format_unit_address  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests._patching import workers_ns  # noqa: E402

WA_ID = "5511999999999"

# The clinic of the battery (2026-11-26): the legacy clinic-level address carried a junk CEP.
LEGACY_ADDRESS = {
    "line": "Rua Constante Ramos, 122",
    "neighborhood": "Copacabana",
    "city": "Rio de Janeiro",
    "state": "RJ",
    "postal_code": "123121",
}
SEDE_LINE = "Rua Constante Ramos, 122, Copacabana, Rio de Janeiro - RJ, 22051-012"


def unit(
    tenant_id,
    name: str,
    *,
    street: str = "Rua Constante Ramos",
    number: str | None = "122",
    cep: str | None = "22051012",
    neighborhood: str = "Copacabana",
    city: str = "Rio de Janeiro",
    state: str = "RJ",
    active: bool = True,
) -> Unit:
    """A unit whose text line is derived from its parts, exactly like the hub writes it."""
    parts = UnitAddressParts(
        cep=cep, street=street, number=number, neighborhood=neighborhood, city=city, state=state
    )
    return Unit(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        address=format_unit_address(parts),
        cep=cep,
        street=street,
        number=number,
        neighborhood=neighborhood,
        city=city,
        state=state,
        is_active=active,
    )


class WireClient:
    """The channel client: records what was sent, in order."""

    sends: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        WireClient.sends.append(("text", body, None))
        return {"messages": [{"id": f"wamid.out.{len(WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        WireClient.sends.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.out.{len(WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        WireClient.sends.append(("list", body, list(rows)))
        return {"messages": [{"id": f"wamid.out.{len(WireClient.sends)}"}]}


def wire(monkeypatch, db) -> dict:
    """Patch the pipeline onto `db`; returns the fake model's control dict."""
    agent: dict = {"reply": "Oi!", "calls": 0, "kwargs": []}

    async def _send(to, template, variables):
        return EmailOutcome.SENT

    async def _emails(_tenant_id):
        return {}

    async def _run_agent(message, context, **kwargs):
        agent["calls"] += 1
        agent["kwargs"].append(kwargs)
        return agent["reply"]

    async def _entitled(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    async def _token(_session, _tenant_id):
        return "decrypted-waba-token"

    async def _none(*_a, **_k):
        return None

    WireClient.sends = []
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(hn, "async_session_factory", db)
    monkeypatch.setattr(hn, "send_transactional_email_result", _send)
    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    monkeypatch.setattr(hn, "_retry_sleep", lambda _n: 0)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    monkeypatch.setattr(workers_ns, "WhatsAppClient", WireClient)
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "run_agent", _run_agent)
    return agent


async def seed(db, *, units: int = 1, address=None, wa_id: str = WA_ID):
    """A clinic with `units` complete units (Sede, Centro, ...) in LLM mode with one patient."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Chrysostomo For Eyes",
            phone_number_id=str(uuid4().int)[:12],
            contact_email="recepcao@example.com",
            is_active=True,
            initial_flows={},
            timezone="America/Sao_Paulo",
            address=address,
        )
        session.add(tenant)
        await session.flush()
        names = ["Sede", "Centro", "Barra"]
        made = [unit(tenant.id, names[index], number=str(122 + index)) for index in range(units)]
        session.add_all(made)
        patient = Patient(
            tenant_id=tenant.id, wa_id=wa_id, name="Maria", lgpd_accepted_at=datetime.now(UTC)
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Como posso ajudar?",
            )
        )
        await session.commit()
        return tenant, conversation, made


_seq = iter(range(1, 1_000_000))


async def turn(tenant, body: str, *, wa_id: str = WA_ID) -> None:
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=wa_id,
        patient_name="Maria",
        wam_id=f"wamid.in.094.{next(_seq)}",
        body=body,
        interactive_reply_id=None,
    )
    assert reply is not None
    await tasks._send_bot_reply(reply, redis=None)
