"""The sandbox proof script must be incapable of echoing a key, a body or a document."""

import importlib.util
import io
import json
import pathlib
from contextlib import redirect_stdout

import httpx
import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "asaas_sandbox_proof.py"
_SANDBOX_KEY = "$aact_hmlg_000000000000000000"


def _load():
    spec = importlib.util.spec_from_file_location("asaas_sandbox_proof", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_refuses_a_production_key():
    proof = _load()
    with pytest.raises(SystemExit):
        proof.require_sandbox_key("$aact_prod_000000000000000000")


def test_accepts_a_sandbox_key_shape():
    proof = _load()
    assert proof.require_sandbox_key(_SANDBOX_KEY) == _SANDBOX_KEY


def test_no_key_in_the_environment_is_a_skip_never_a_failure():
    proof = _load()
    assert proof.resolve_keys({}) == (None, None, "no_sandbox_key")
    assert proof.resolve_keys({"ASAAS_SANDBOX_API_KEY": ""}) == (None, None, "no_sandbox_key")


def test_a_production_key_in_the_environment_is_a_skip_and_is_never_used():
    proof = _load()
    env = {"ASAAS_SANDBOX_API_KEY": "$aact_prod_000000000000000000"}
    assert proof.resolve_keys(env) == (None, None, "key_not_sandbox")


def test_only_the_default_key_runs_without_the_qualification_checks():
    proof = _load()
    env = {"ASAAS_SANDBOX_API_KEY": _SANDBOX_KEY}
    assert proof.resolve_keys(env) == (_SANDBOX_KEY, None, None)


def test_a_scoped_key_that_is_not_sandbox_is_dropped_not_used():
    proof = _load()
    env = {
        "ASAAS_SANDBOX_API_KEY": _SANDBOX_KEY,
        "ASAAS_SANDBOX_API_KEY_SCOPED": "$aact_prod_000000000000000000",
    }
    assert proof.resolve_keys(env) == (_SANDBOX_KEY, None, None)


def test_both_keys_enable_the_full_qualification_run():
    proof = _load()
    scoped = "$aact_hmlg_111111111111111111"
    env = {"ASAAS_SANDBOX_API_KEY": _SANDBOX_KEY, "ASAAS_SANDBOX_API_KEY_SCOPED": scoped}
    assert proof.resolve_keys(env) == (_SANDBOX_KEY, scoped, None)


def test_main_without_keys_prints_one_skip_line_and_exits_zero(monkeypatch):
    proof = _load()
    monkeypatch.delenv("ASAAS_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("ASAAS_SANDBOX_API_KEY_SCOPED", raising=False)
    out = io.StringIO()
    with redirect_stdout(out):
        code = proof.main()
    assert code == 0
    assert json.loads(out.getvalue().strip()) == {
        "check": "skipped_all", "reason": "no_sandbox_key"
    }


def test_emit_prints_only_status_and_symbolic_code_never_the_body():
    proof = _load()
    secret_body = {
        "errors": [
            {
                "code": "insufficient_permission",
                "description": "cpf 52998224725 marina@example.test",
            }
        ],
        "note": "SECRET-BODY-TEXT",
    }
    response = httpx.Response(403, json=secret_body)
    out = io.StringIO()
    with redirect_stdout(out):
        proof.emit("S12_probe", response, group="TRANSFER", role="excess")
    printed = out.getvalue()
    line = json.loads(printed.strip())
    assert line == {
        "check": "S12_probe",
        "status": 403,
        "code": "insufficient_permission",
        "group": "TRANSFER",
        "role": "excess",
    }
    assert "SECRET-BODY-TEXT" not in printed
    assert "52998224725" not in printed
    assert "marina@example.test" not in printed


def test_emit_rejects_free_text_extras():
    proof = _load()
    with pytest.raises(ValueError):
        proof.emit("S99", httpx.Response(200, json={}), free_text="a sentence with spaces")
