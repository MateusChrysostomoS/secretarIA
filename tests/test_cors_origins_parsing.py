"""CORS_ALLOW_ORIGINS parsing — the format the mesh actually ships.

The failure these tests exist for (2026-09-12): brain-api learned to read a
JSON array for this variable on 2026-08-21; this service did not. An operator
copied brain-api's JSON value into `secretaria_api`'s EasyPanel panel, where
the comma split tore it into `["https://…host` and `https://…host"]`. Neither
matches a browser's `Origin` header, so the hub answered 400 "Disallowed CORS
origin" to EVERY origin and the doctor portal's /configuracao went blank and
read-only. The value looked perfectly correct in the panel.

See docs/CHECKPOINT_cors_json_array_hub.md.
"""

import pytest

from secretaria.config import Settings

FRONTEND = "https://secretaria-secretaria-frontend.cpux9k.easypanel.host"
BRAIN_MESSAGE = "https://precheckv2-brain-message-frontend.cpux9k.easypanel.host"


def _origins(raw: str) -> list[str]:
    return Settings(CORS_ALLOW_ORIGINS=raw).cors_origins


def test_production_json_value_yields_the_real_origins():
    """The exact string that was live in EasyPanel when the portal broke."""
    raw = f'["{FRONTEND}","{BRAIN_MESSAGE}"]'
    assert _origins(raw) == [FRONTEND, BRAIN_MESSAGE]


def test_json_array_never_leaks_brackets_or_quotes_into_an_origin():
    """The actual regression: an origin carrying `["` or `"]` matches nothing."""
    for origin in _origins(f'["{FRONTEND}","{BRAIN_MESSAGE}"]'):
        assert "[" not in origin and "]" not in origin
        assert '"' not in origin and "'" not in origin


def test_legacy_comma_separated_form_still_works():
    """The old format stays valid — this is additive, nothing to migrate."""
    assert _origins(f"{FRONTEND},{BRAIN_MESSAGE}") == [FRONTEND, BRAIN_MESSAGE]


def test_json_array_with_spacing_and_trailing_slashes():
    raw = f'[ "{FRONTEND}/" , "{BRAIN_MESSAGE}" ]'
    assert _origins(raw) == [FRONTEND, BRAIN_MESSAGE]


def test_malformed_json_fails_closed_without_raising():
    """A typo must reject everyone, never crash the process building Settings."""
    assert _origins(f'["{FRONTEND}",') == []


def test_json_scalar_is_not_treated_as_a_list():
    assert _origins('["]') == []


@pytest.mark.parametrize("raw", ["", "   ", ",,,"])
def test_blank_values_yield_no_origins(raw: str):
    assert _origins(raw) == []


def test_wildcard_survives_verbatim_in_both_formats():
    assert _origins("*") == ["*"]
    assert _origins('["*"]') == ["*"]


def test_json_and_comma_forms_agree():
    """Same origins, either spelling — the property the mesh relies on."""
    assert _origins(f'["{FRONTEND}","{BRAIN_MESSAGE}"]') == _origins(f"{FRONTEND},{BRAIN_MESSAGE}")
