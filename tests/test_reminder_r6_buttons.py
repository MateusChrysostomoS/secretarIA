"""The reminder card's buttons and ids (TASK-032 R6, spec §5.1)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS  # noqa: E402
from secretaria.schemas import webhook  # noqa: E402
from secretaria.schemas.webhook import decode_action_id  # noqa: E402
from secretaria.services import reminder_text as rt  # noqa: E402


def test_the_reminder_card_is_confirmar_cancelar_alterar_dados():
    reminder_id = uuid4()
    assert rt.reminder_buttons(reminder_id) == [
        (f"remconfirm|{reminder_id}", "Confirmar"),
        (f"remcancel|{reminder_id}", "Cancelar"),
        (f"remedit|{reminder_id}", "Alterar Dados"),
    ]
    assert len(rt.LABEL_EDIT) <= MAX_BUTTON_LABEL_CHARS


def test_remedit_decodes_like_the_other_row_ids():
    reminder_id = str(uuid4())
    assert decode_action_id(f"remedit|{reminder_id}") == ("remedit", reminder_id)


def test_the_actions_are_split_between_current_legacy_and_confirmation_steps():
    assert rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remedit")
    assert set(rt.LEGACY_REMINDER_ACTIONS) == {"remother", "remresched", "remnew", "remgiveup"}
    assert rt.CANCEL_PATH_ACTIONS == ("remgiveupyes", "remkeep")
    assert set(rt.REMINDER_ROW_ACTIONS) == (
        set(rt.REMINDER_ACTIONS) | set(rt.LEGACY_REMINDER_ACTIONS) | set(rt.CANCEL_PATH_ACTIONS)
    )


def test_the_decoder_knows_exactly_the_rem_actions():
    known = {prefix[:-1] for prefix in webhook._ACTION_BUTTON_PREFIXES if prefix.startswith("rem")}
    assert known == set(rt.REMINDER_ROW_ACTIONS)


@pytest.mark.parametrize("action", ["remother", "remresched", "remnew", "remgiveup"])
def test_legacy_ids_from_cards_still_on_screen_still_decode(action):
    reminder_id = str(uuid4())
    assert decode_action_id(f"{action}|{reminder_id}") == (action, reminder_id)


def test_the_template_payloads_keep_their_position():
    reminder_id = uuid4()
    assert rt.button_payloads(rt.reminder_buttons(reminder_id)) == [
        f"remconfirm|{reminder_id}",
        f"remcancel|{reminder_id}",
        f"remedit|{reminder_id}",
    ]
