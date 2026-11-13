"""TASK-082 S1 - GET /internal/brain-message/messages/{message_id}/options.

The route a Portal card's later pages and search come from (CONTRACT.md §c4). Keyed
by the CARD's message id; the list is rebuilt from the scope stored on that row, so
the caller never chooses what is listed. Ownership is one query (message + tenant +
channel + external_id), and every miss is the same 404.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import (
    flow_router as fr,  # noqa: E402
    list_options as lo,  # noqa: E402
)
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    RECORDED_MESSAGE_ID,
    BrainMessageSender,
)
from secretaria.workers.shared.bubbles import _send_bubble  # noqa: E402

KEY = {"X-Internal-Api-Key": "test-internal-key"}
EXTERNAL_ID = "bm-options-1"
NAMES = (
    [f"Serviço {i:02d}" for i in range(20)]
    + ["Avaliação inicial", "Cardiologia pediátrica"]
    + [f"Exame {i:02d}" for i in range(3)]
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    from httpx import ASGITransport, AsyncClient

    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _clinic(db, *, names=NAMES, external_id=EXTERNAL_ID):
    """A clinic with a long tenant-level catalogue and one Portal patient's conversation."""
    async with db() as session, session.begin():
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            initial_flows={"enabled": True},
            appointment_types=[
                {"name": n, "duration_min": 30, "is_active": True, "sort_order": i}
                for i, n in enumerate(names)
            ],
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(
            tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE, external_id=external_id
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        return tenant, conversation


async def _second_patient(db, tenant, external_id):
    async with db() as session, session.begin():
        patient = Patient(
            tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE, external_id=external_id
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        return conversation


async def _send(db, conversation, bubble) -> str:
    response = await _send_bubble(
        BrainMessageSender(conversation_id=conversation.id, session_factory=db), "x", bubble
    )
    return response[RECORDED_MESSAGE_ID]


async def _send_service_card(db, tenant, conversation) -> str:
    """The real router card, sent through the real Portal sender; returns its message id."""
    snapshot = SimpleNamespace(appointment_types=tenant.appointment_types)
    result = await fr._enter_menu_choice(0, snapshot, None)
    [bubble] = result.bubbles
    return await _send(db, conversation, bubble)


async def _ask(client, message_id, tenant_id, external_id=EXTERNAL_ID, **body):
    """POST, with everything in the JSON body: the search text never rides in a URL."""
    return await client.post(
        f"/internal/brain-message/messages/{message_id}/options",
        json={"tenant_id": str(tenant_id), "external_id": external_id, **body},
        headers=KEY,
    )


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


async def test_the_card_holds_page_one_and_the_route_serves_the_rest(client, db) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    async with db() as session:
        card = (await session.get(Message, UUID(message_id))).interactive
    assert card["source"]["total"] == len(NAMES)

    first = await _ask(client, message_id, tenant.id)
    assert first.status_code == 200, first.text
    body = first.json()
    # Page 1 of the route IS the card's page 1 (pinned rows excluded).
    assert [i["id"] for i in body["items"]] == [
        o["id"] for o in card["options"] if o["id"] not in card["source"]["pinned_ids"]
    ]
    assert body["next_cursor"] == card["source"]["next_cursor"]
    assert body["total"] == len(NAMES)

    middle = (await _ask(client, message_id, tenant.id, cursor=body["next_cursor"])).json()
    assert [i["id"] for i in middle["items"]] == [f"svc|{n}" for n in NAMES[10:20]]
    last = (await _ask(client, message_id, tenant.id, cursor=middle["next_cursor"])).json()
    assert [i["id"] for i in last["items"]] == [f"svc|{n}" for n in NAMES[20:]]
    assert last["next_cursor"] is None
    assert all(i["id"] != "svchelp|0" for i in body["items"] + middle["items"] + last["items"])


async def test_an_empty_catalogue_and_a_cursor_past_the_end(client, db) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    past = await _ask(client, message_id, tenant.id, cursor=lo.encode_cursor(990))
    assert past.status_code == 200
    assert past.json() == {"items": [], "next_cursor": None, "total": len(NAMES)}

    # The clinic empties its catalogue after the card went out: an empty page, not an error.
    async with db() as session, session.begin():
        (await session.get(Tenant, tenant.id)).appointment_types = []
    empty = await _ask(client, message_id, tenant.id)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None, "total": 0}


@pytest.mark.parametrize("bad", ["garbage!!", "eyJ2IjoyLCJvIjoxMH0", "x" * 201, ""])
async def test_a_tampered_cursor_is_422(client, db, bad) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    response = await _ask(client, message_id, tenant.id, cursor=bad)
    assert response.status_code == 422
    # `detail.code` - the shape brain-api maps (results/s2-brain-api-proxy.md).
    assert response.json()["detail"] == {"code": "invalid_cursor"}


@pytest.mark.parametrize("size", [0, 11, -1])
async def test_page_size_out_of_range_is_422(client, db, size) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    assert (await _ask(client, message_id, tenant.id, page_size=size)).status_code == 422


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("avaliacao", ["svc|Avaliação inicial"]),
        ("CARDIO", ["svc|Cardiologia pediátrica"]),
        ("🏥 exame 0", ["svc|Exame 00", "svc|Exame 01", "svc|Exame 02"]),
        ("nada disso", []),
    ],
)
async def test_search_covers_the_whole_catalogue(client, db, q, expected) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    response = await _ask(client, message_id, tenant.id, q=q)
    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == expected
    assert response.json()["total"] == len(expected)


async def test_a_long_search_is_clamped_and_never_logged(client, db) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    secret = "pacienteprocurouisto" * 10  # 200 chars, a patient's own words
    with capture_logs() as logs:
        response = await _ask(client, message_id, tenant.id, q=secret)
        missing = await _ask(client, uuid4(), tenant.id, q=secret)
    assert response.status_code == 200 and response.json()["total"] == 0
    assert missing.status_code == 404
    assert not any("pacienteprocurou" in repr(entry) for entry in logs)


async def test_there_is_no_url_form_of_the_search(client, db) -> None:
    """Every server and proxy on the way logs URLs; a search in one is a search in a log.

    So the route is POST-only and the text travels in the body. A GET with `?q=` is
    refused outright, and a `q` in the query string of the POST is never read.
    """
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    url = f"/internal/brain-message/messages/{message_id}/options"
    params = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "q": "avaliacao"}

    assert (await client.get(url, params=params, headers=KEY)).status_code == 405
    ignored = await client.post(
        url,
        params={"q": "avaliacao"},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
        headers=KEY,
    )
    assert ignored.status_code == 200 and ignored.json()["total"] == len(NAMES)


# --------------------------------------------------------------------------
# Ownership: one 404 for every miss
# --------------------------------------------------------------------------


async def test_every_miss_is_the_same_404(client, db) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    other_tenant, other_conversation = await _clinic(db, external_id="someone")
    other_card = await _send_service_card(db, other_tenant, other_conversation)
    neighbour = await _second_patient(db, tenant, "neighbour")
    neighbour_card = await _send_service_card(db, tenant, neighbour)
    async with db() as session, session.begin():
        inbound = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.PATIENT,
            body="oi",
        )
        old_card = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.BOT,
            body="lista antiga",
            interactive={"kind": "list", "body": "b", "options": [{"id": "svc|A", "title": "A"}]},
        )
        session.add_all([inbound, old_card])
        await session.flush()
        inbound_id, old_card_id = inbound.id, old_card.id

    misses = [
        (other_card, tenant.id, EXTERNAL_ID),  # another clinic's card
        (message_id, other_tenant.id, EXTERNAL_ID),  # this card, asked under another tenant
        (neighbour_card, tenant.id, EXTERNAL_ID),  # another patient's card, same clinic
        (message_id, tenant.id, "neighbour"),  # this card, asked as another patient
        (inbound_id, tenant.id, EXTERNAL_ID),  # not a card at all
        (old_card_id, tenant.id, EXTERNAL_ID),  # a card from before TASK-082 (no source)
        (uuid4(), tenant.id, EXTERNAL_ID),  # unknown
        ("not-a-uuid", tenant.id, EXTERNAL_ID),  # malformed
    ]
    for mid, tid, ext in misses:
        response = await _ask(client, mid, tid, ext)
        assert response.status_code == 404, (mid, response.text)
        assert response.json()["detail"] == {"code": "options_not_found"}
    # Control: the owner still reads it.
    assert (await _ask(client, message_id, tenant.id)).status_code == 200


async def test_the_route_requires_the_internal_key(client, db) -> None:
    tenant, conversation = await _clinic(db)
    message_id = await _send_service_card(db, tenant, conversation)
    response = await client.post(
        f"/internal/brain-message/messages/{message_id}/options",
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
    )
    assert response.status_code in (401, 403)


async def test_doctor_scope_never_widens_to_another_doctor(client, db) -> None:
    """A doctor's service card pages only that doctor's services, whatever is asked."""
    tenant, conversation = await _clinic(db, names=["Legado"])
    async with db() as session, session.begin():
        ana = Professional(
            tenant_id=tenant.id,
            name="Ana",
            is_active=True,
            appointment_types=[
                {"name": f"Ana {i:02d}", "duration_min": 30, "is_active": True, "sort_order": i}
                for i in range(12)
            ],
        )
        bia = Professional(
            tenant_id=tenant.id,
            name="Bia",
            is_active=True,
            appointment_types=[{"name": "Bia só", "duration_min": 30, "is_active": True}],
        )
        session.add_all([ana, bia])
        await session.flush()
        ana_snapshot = SimpleNamespace(id=ana.id, appointment_types=ana.appointment_types)
    services = fr.professional_appointment_types(ana_snapshot, tenant)
    bubble = fr._service_list_bubble(tenant, services, scope=fr.service_scope(ana_snapshot.id))
    message_id = await _send(db, conversation, bubble)

    every: list[str] = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = (await _ask(client, message_id, tenant.id, **params)).json()
        every += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert every == [f"svc|Ana {i:02d}" for i in range(12)]
    search = (await _ask(client, message_id, tenant.id, q="bia")).json()
    assert search["items"] == [] and search["total"] == 0
