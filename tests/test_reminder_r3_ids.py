"""The cancel-path button ids (TASK-032 R3, spec §4.3)."""

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


@pytest.mark.parametrize("action", ["remresched", "remnew", "remgiveup", "remgiveupyes", "remkeep"])
def test_the_cancel_path_ids_decode(action):
    reminder_id = str(uuid4())
    assert decode_action_id(f"{action}|{reminder_id}") == (action, reminder_id)


def test_a_longer_id_never_decodes_as_its_shorter_sibling():
    reminder_id = str(uuid4())
    assert decode_action_id(f"remgiveupyes|{reminder_id}") == ("remgiveupyes", reminder_id)
    assert decode_action_id(f"remgiveup|{reminder_id}") == ("remgiveup", reminder_id)


def test_the_give_up_confirmation_card():
    reminder_id = uuid4()
    assert rt.give_up_confirm_buttons(reminder_id) == [
        (f"remgiveupyes|{reminder_id}", "Sim, cancelar"),
        (f"remkeep|{reminder_id}", "Manter consulta"),
    ]


def test_the_reminder_card_replaces_the_three_way_choice_with_edit():
    reminder_id = uuid4()
    assert rt.reminder_buttons(reminder_id) == [
        (f"remconfirm|{reminder_id}", "Confirmar"),
        (f"remcancel|{reminder_id}", "Cancelar"),
        (f"remedit|{reminder_id}", "Alterar Dados"),
    ]


def test_every_new_label_fits_a_whatsapp_button():
    labels = [
        rt.LABEL_EDIT,
        rt.LABEL_GIVE_UP_CONFIRM,
        rt.LABEL_KEEP,
    ]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)


def test_the_row_actions_are_exactly_the_rem_prefixes_the_decoder_knows():
    known = {prefix[:-1] for prefix in webhook._ACTION_BUTTON_PREFIXES if prefix.startswith("rem")}
    assert known == set(rt.REMINDER_ROW_ACTIONS)
    assert rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remedit")
    assert rt.REMINDER_ROW_ACTIONS == (
        rt.REMINDER_ACTIONS + rt.LEGACY_REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS
    )
