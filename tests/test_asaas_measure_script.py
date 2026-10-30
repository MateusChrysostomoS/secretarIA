"""The Asaas measurement script must be incapable of echoing a key, a document or Asaas text."""

import importlib.util
import io
import json
import pathlib
from contextlib import redirect_stdout

import httpx
import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "asaas_measure.py"


def _load():
    spec = importlib.util.spec_from_file_location("asaas_measure", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


def _printed(measure, *args, **kwargs) -> dict:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        measure.emit(*args, **kwargs)
    return json.loads(buffer.getvalue())


def test_error_text_is_reduced_to_true_false_flags():
    measure = _load()
    secret_text = "O valor minimo da cobranca e R$ 5,00 para o CPF 123.456.789-00"
    line = _printed(
        measure,
        "charge_100",
        _response(400, {"errors": [{"code": "invalid_value", "description": secret_text}]}),
        cents=100,
    )
    assert line["status"] == 400
    assert line["code"] == "invalid_value"
    assert line["kw_value"] is True
    assert line["kw_document"] is True
    assert line["kw_pix_key"] is False
    assert "123" not in json.dumps(line) and "minimo" not in json.dumps(line)


def test_emit_refuses_any_free_text_field():
    measure = _load()
    with pytest.raises(ValueError):
        measure.emit("x", None, note="texto livre do asaas")
    with pytest.raises(ValueError):
        measure.emit("x", None, state_general="Conta aprovada para fulano")


def test_onboarding_states_are_fixed_words_only():
    measure = _load()
    states = measure.onboarding_states(
        {
            "general": "APPROVED",
            "documentation": "AWAITING_APPROVAL",
            "commercialInfo": "something new",
        }
    )
    assert states == {
        "state_general": "APPROVED",
        "state_documents": "AWAITING_APPROVAL",
        "state_commercial": "OTHER",
        "state_bank": "NOT_READABLE",
    }


def test_sandbox_key_targets_the_sandbox_host():
    measure = _load()
    assert "sandbox" in measure.base_url_for("$aact_hmlg_abc")
    assert "sandbox" not in measure.base_url_for("$aact_prod_abc")
