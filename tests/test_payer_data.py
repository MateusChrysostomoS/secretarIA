"""The payer-data policy (decision D7, generalised): the pure half and the transient store.

Nothing here touches a database: the provider-code mapping, the reading of ONE patient turn, the
copy, the redaction rule and the Redis store are all functions of their arguments. The flow around
them (gate, router, worker ladder, redaction) is `tests/test_payer_data_flow.py` (`before`) and
`tests/test_payer_data_after.py` (`after`).
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import structlog

from secretaria.core import crypto
from secretaria.services.payments import payer_data as pdata
from tests.payments_support import FakeRedis

CPF = "52998224725"  # a well-known valid test number, never a real person's
CNPJ = "11222333000181"  # ditto
EMAIL = "maria.silva@example.com"
VALUE = {"document": CPF, "email": EMAIL}
KINDS = ["document", "email"]
NOW = datetime(2030, 3, 4, 12, 0, tzinfo=UTC)
TENANT, CONVERSATION = uuid4(), uuid4()


def test_the_provider_says_which_field_it_needs():
    assert pdata.kind_for_code("payer_document_required") == "document"
    assert pdata.kind_for_code("payer_email_required") == "email"
    for other in ("customer_failed", "charge_failed", "invalid_amount", "", None):
        assert pdata.kind_for_code(other) is None


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("kind", KINDS)
def test_the_step_round_trips_and_fits_the_column(timing, kind):
    step = pdata.step_for(timing, kind, 2)
    assert len(step) <= 48  # Conversation.flow_step is VARCHAR(48)
    assert pdata.parse_step(step) == pdata.StepInfo(timing, kind, 2)


@pytest.mark.parametrize(
    "step",
    [
        None,
        "",
        "awaiting_confirmation",
        "payer_data:",
        "payer_data:before:doc",
        "payer_data:later:doc:0",
        "payer_data:before:cnh:0",
        "payer_data:before:doc:x",
        "payer_data:before:doc:1:2",
        "payer_doc:0",
    ],
)
def test_a_step_that_is_not_ours_or_is_malformed_reads_as_none(step):
    assert pdata.parse_step(step) is None


def _read(kind, body, *, attempts=0, silent_for=timedelta(minutes=1)):
    return pdata.read_turn(
        kind, body, attempts_so_far=attempts, last_activity_at=NOW - silent_for, now=NOW
    )


@pytest.mark.parametrize(
    ("kind", "typed"),
    [("document", "529.982.247-25"), ("document", CNPJ), ("email", "Maria.Silva@Example.com")],
)
def test_a_valid_value_is_captured_and_never_shown_in_a_repr(kind, typed):
    reading = _read(kind, typed)
    assert reading.kind == "captured"
    assert reading.value is not None and reading.value not in repr(reading)


def test_the_field_asked_is_the_field_read():
    assert _read("email", CPF).kind == "invalid"  # a document is not an e-mail
    assert _read("document", EMAIL).kind == "invalid"  # nor the other way round


@pytest.mark.parametrize("kind", KINDS)
def test_three_invalid_answers_in_a_row_end_the_wait(kind):
    first = _read(kind, "abc")
    second = _read(kind, "123", attempts=first.attempts)
    third = _read(kind, "maria@", attempts=second.attempts)
    assert [r.kind for r in (first, second, third)] == ["invalid", "invalid", "exhausted"]
    assert [r.attempts for r in (first, second, third)] == [1, 2, 3]


@pytest.mark.parametrize("kind", KINDS)
def test_a_valid_value_on_the_last_attempt_still_counts(kind):
    assert _read(kind, VALUE[kind], attempts=2).kind == "captured"


@pytest.mark.parametrize("kind", KINDS)
def test_the_opt_out_wins_over_everything_and_costs_no_attempt(kind):
    reading = _read(kind, pdata.LABEL_OPTOUT, attempts=1)
    assert (reading.kind, reading.value) == ("optout", None)


@pytest.mark.parametrize("kind", KINDS)
def test_silence_past_the_window_expires_the_wait_even_for_a_valid_value(kind):
    late = timedelta(seconds=pdata.DATA_TTL_SECONDS)
    assert _read(kind, VALUE[kind], silent_for=late).kind == "expired"
    assert _read(kind, VALUE[kind], silent_for=late - timedelta(seconds=1)).kind == "captured"


@pytest.mark.parametrize(
    "text",
    [
        pdata.LABEL_OPTOUT,
        "não quero informar",
        "NAO QUERO",
        "Prefiro não",
        "não tenho cpf",
        "não tenho e-mail",
        "sem email",
        "sem cpf",
        "não",
        "cancelar",
        "desisti",
    ],
)
def test_the_ways_of_saying_no_are_recognised(text):
    assert pdata.looks_like_optout(text) is True


@pytest.mark.parametrize(
    "text",
    [
        CPF,
        "meu cpf é 529.982.247-25",
        "123",
        "quanto custa?",
        "nao sei o numero",
        "meu email é maria@example.com",
        # the words of an opt-out inside an ADDRESS are not an opt-out
        "cancelar@example.com",
        "nao.tenho@example.com",
        "desisti@example.com",
    ],
)
def test_a_value_or_a_question_is_not_an_opt_out(text):
    assert pdata.looks_like_optout(text) is False


def test_the_address_that_says_cancel_is_still_captured():
    assert _read("email", "cancelar@example.com").kind == "captured"


def test_the_copy_names_the_provider_says_what_is_kept_and_offers_the_way_out():
    doc = pdata.ask_text("document", "asaas")
    assert "Asaas" in doc and "CPF ou CNPJ" in doc and "envie só os números" in doc
    mail = pdata.ask_text("email", "mercadopago")
    assert "Mercado Pago" in mail and "e-mail" in mail and "CPF" not in mail
    for text in (doc, mail):
        assert "não fica guardado no nosso cadastro" in text
        assert pdata.LABEL_OPTOUT in text
    assert len(pdata.LABEL_OPTOUT) <= 20  # a WhatsApp reply-button title
    assert "Asaas" not in pdata.ask_text("document", None)


@pytest.mark.parametrize("timing", ["before", "after"])
def test_the_refusal_copy_says_whether_the_consultation_stands(timing):
    stands = "continua agendada"
    for text in (
        pdata.optout_text(timing, "document"),
        pdata.optout_text(timing, "email"),
        pdata.exhausted_text(timing),
    ):
        assert (stands in text) is (timing == "after")
        assert ("não foi reservado" in text) is (timing == "before")
    assert (stands in pdata.expired_text(timing)) is (timing == "after")


@pytest.mark.parametrize(
    ("state", "step", "body", "redact"),
    [
        ("AWAITING_PAYER_DATA", "payer_data:before:doc:1", "qualquer coisa", True),
        ("AWAITING_PAYER_DATA", "payer_data:after:mail:0", "qualquer coisa", True),
        ("AWAITING_PAYER_DATA", "payer_data:before:doc:0", CPF, True),
        ("AWAITING_PAYER_DATA", "payer_data:before:mail:0", EMAIL, True),
        ("AWAITING_DEPOSIT", None, "529.982.247-25", True),  # the same document sent twice
        ("AWAITING_DEPOSIT", None, "meu e-mail: maria@example.com", True),  # ditto, an e-mail
        ("SERVICE_CATALOG", "awaiting_confirmation", CPF, True),  # the charge did not start
        ("SERVICE_CATALOG", "awaiting_retry_choice", EMAIL, True),  # the slot was taken meanwhile
        ("AWAITING_DEPOSIT", None, "já paguei", False),
        ("SERVICE_CATALOG", "awaiting_confirmation", "Confirmar", False),
        ("IDLE", None, CPF, False),  # outside the wait nothing is rewritten
        ("IDLE", None, EMAIL, False),
        ("SERVICE_CATALOG", "awaiting_slot", CPF, False),
    ],
)
def test_which_inbound_messages_are_stored_redacted(state, step, body, redact):
    assert pdata.should_redact_inbound(state, step, body) is redact


def test_a_collected_value_is_never_in_a_repr():
    data = pdata.PayerData(document=CPF, email=EMAIL)
    assert CPF not in repr(data) and EMAIL not in repr(data)
    assert data.has("document") and data.has("email") and data.any()
    assert not pdata.PayerData().any()
    assert EMAIL not in str(pdata.PayerDataNeeded("email", "mercadopago"))


# ---- the store --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_value_is_kept_encrypted_with_a_ttl_and_scoped_to_one_conversation(kind):
    redis = FakeRedis()
    value = VALUE[kind]
    kept = await pdata.remember(
        redis, tenant_id=TENANT, conversation_id=CONVERSATION, kind=kind, value=value
    )
    assert kept is True
    (key,) = redis.store
    assert redis.ttls[key] == pdata.DATA_TTL_SECONDS
    assert value not in redis.store[key] and value not in key  # ciphertext, not in the key either
    assert crypto.decrypt(redis.store[key]) == value
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    assert await pdata.recall(redis, kind=kind, **args) == value
    other = "email" if kind == "document" else "document"  # the other field is a different key
    assert await pdata.recall(redis, kind=other, **args) is None
    # another conversation of the same clinic, and the same conversation id under another clinic
    elsewhere = {"redis": redis, "kind": kind}
    assert await pdata.recall(tenant_id=TENANT, conversation_id=uuid4(), **elsewhere) is None
    assert await pdata.recall(tenant_id=uuid4(), conversation_id=CONVERSATION, **elsewhere) is None


async def test_recall_all_reads_both_fields_and_discard_forgets_everything():
    redis = FakeRedis()
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    for kind in KINDS:
        await pdata.remember(redis, kind=kind, value=VALUE[kind], **args)
    await pdata.remember_pending(redis, appointment_id=uuid4(), **args)
    data = await pdata.recall_all(redis, **args)
    assert (data.document, data.email) == (CPF, EMAIL)

    await pdata.discard(redis, **args)
    await pdata.discard(redis, **args)  # harmless twice
    assert redis.store == {} and redis.ttls == {}
    assert not (await pdata.recall_all(redis, **args)).any()


async def test_the_after_pointer_is_an_id_with_a_ttl_and_scoped_to_one_conversation():
    redis = FakeRedis()
    appointment_id = uuid4()
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    assert await pdata.remember_pending(redis, appointment_id=appointment_id, **args)
    (key,) = redis.store
    assert redis.ttls[key] == pdata.DATA_TTL_SECONDS
    assert await pdata.recall_pending(redis, **args) == appointment_id
    other_clinic = {"tenant_id": uuid4(), "conversation_id": CONVERSATION}
    assert await pdata.recall_pending(redis, **other_clinic) is None


@pytest.mark.parametrize("kind", KINDS)
async def test_a_store_that_cannot_keep_it_says_so_and_never_logs_it(kind):
    value = VALUE[kind]

    class Down(FakeRedis):
        async def set(self, *args, **kwargs):
            raise ConnectionError(f"redis down while storing {value}")

    with structlog.testing.capture_logs() as logs:
        kept = await pdata.remember(
            Down(), tenant_id=TENANT, conversation_id=CONVERSATION, kind=kind, value=value
        )
    assert kept is False
    assert [entry["event"] for entry in logs] == ["payer_data_store_failed"]
    assert value not in json.dumps(logs, default=str)  # the exception TEXT is never logged


async def test_no_redis_means_nothing_is_kept_and_nothing_is_read():
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    assert not await pdata.remember(None, kind="email", value=EMAIL, **args)
    assert await pdata.recall(None, kind="email", **args) is None
    assert not (await pdata.recall_all(None, **args)).any()
    assert not await pdata.remember_pending(None, appointment_id=uuid4(), **args)
    await pdata.discard(None, **args)


async def test_a_value_that_cannot_be_decrypted_reads_as_absent():
    redis = FakeRedis()
    await redis.set(f"payer_data:email:{TENANT}:{CONVERSATION}", "not-a-fernet-token")
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    assert await pdata.recall(redis, kind="email", **args) is None


async def test_recall_accepts_the_bytes_a_real_redis_returns():
    redis = FakeRedis()
    args = {"tenant_id": TENANT, "conversation_id": CONVERSATION}
    await pdata.remember(redis, kind="email", value=EMAIL, **args)
    (key,) = redis.store
    redis.store[key] = redis.store[key].encode("ascii")
    assert await pdata.recall(redis, kind="email", **args) == EMAIL
