"""ContactEmailUpdate normalisation: strip, lowercase, "" -> None, strict syntax, required key."""

import pytest
from pydantic import ValidationError

from secretaria.schemas.contact_email import ContactEmailRead, ContactEmailUpdate


def test_normalises_whitespace_and_case():
    assert ContactEmailUpdate(contact_email=" Foo@Bar.COM ").contact_email == "foo@bar.com"


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_string_clears(raw):
    assert ContactEmailUpdate(contact_email=raw).contact_email is None


def test_explicit_null_clears():
    assert ContactEmailUpdate(contact_email=None).contact_email is None


def test_the_key_is_required_so_a_typo_never_clears_silently():
    with pytest.raises(ValidationError):
        ContactEmailUpdate.model_validate({})
    with pytest.raises(ValidationError):
        ContactEmailUpdate.model_validate({"contato_email": "a@b.com"})


@pytest.mark.parametrize(
    "raw",
    [
        "plain",
        "a@b",
        "@b.com",
        "a@.com",
        "a@b..com",
        "a b@c.com",
        "a@b.com, c@d.com",
        "a@b@c.com",
        "<a@b.com>",
        "a@b.c",
        "a@-b.com",
        "a\n@b.com",
        123,
    ],
)
def test_invalid_syntax_is_rejected(raw):
    with pytest.raises(ValidationError):
        ContactEmailUpdate.model_validate({"contact_email": raw})


def test_max_254_chars():
    local = "a" * 64
    ok = f"{local}@{'b' * 63}.{'c' * 63}.{'d' * 55}.com"
    assert len(ok) <= 254
    assert ContactEmailUpdate(contact_email=ok).contact_email == ok
    too_long = f"{local}@{'b' * 63}.{'c' * 63}.{'d' * 63}.com"
    assert len(too_long) > 254
    with pytest.raises(ValidationError):
        ContactEmailUpdate(contact_email=too_long)


def test_validation_error_does_not_echo_the_address():
    with pytest.raises(ValidationError) as info:
        ContactEmailUpdate(contact_email="secret-person-not-an-email")
    assert "secret-person" not in str(info.value)


def test_read_shape():
    assert ContactEmailRead(contact_email=None).model_dump() == {"contact_email": None}
