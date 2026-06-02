"""Tests for the /menu reset-command predicate."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.workers.tasks import is_menu_command  # noqa: E402


@pytest.mark.parametrize(
    "body",
    [
        "/menu",
        "/MENU",
        "  /menu  ",
        "/reset",
        "/recomecar",
        "/recomeçar",
        "/inicio",
        "/início",
    ],
)
def test_recognised_triggers(body: str) -> None:
    assert is_menu_command(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "menu",
        "/menus",
        "/menu agora",
        "olá /menu",
        "marcar consulta",
        "",
        None,
    ],
)
def test_non_triggers(body: str | None) -> None:
    assert is_menu_command(body) is False
