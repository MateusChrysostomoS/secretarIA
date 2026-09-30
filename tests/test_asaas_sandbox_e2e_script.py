"""The sandbox end-to-end script must refuse anything that is not a throwaway sandbox setup."""

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "asaas_sandbox_e2e.py"


def _load():
    spec = importlib.util.spec_from_file_location("asaas_sandbox_e2e", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@db.production.example.com:5432/secretaria",
        "postgresql+asyncpg://u:p@10.0.0.5/secretaria",
        "",
    ],
)
def test_refuses_a_database_that_is_not_local(url):
    e2e = _load()
    with pytest.raises(SystemExit):
        e2e.require_local_database(url)


def test_accepts_a_local_throwaway_database():
    e2e = _load()
    url = "postgresql+asyncpg://postgres@127.0.0.1:5544/t016_e2e"
    assert e2e.require_local_database(url) == url
    assert e2e.require_local_database(url.replace("127.0.0.1", "localhost")).endswith("t016_e2e")


def test_refuses_the_production_asaas_host():
    e2e = _load()
    with pytest.raises(SystemExit):
        e2e.require_sandbox_base_url("https://api.asaas.com/v3")
    assert e2e.require_sandbox_base_url("https://api-sandbox.asaas.com/v3")


def test_refuses_a_non_sandbox_key():
    e2e = _load()
    with pytest.raises(SystemExit):
        e2e.require_sandbox_key("$aact_prod_0000000000000000000")
    assert e2e.require_sandbox_key("$aact_hmlg_0000000000000000000")


def test_missing_or_non_sandbox_keys_are_a_skip_not_a_failure():
    e2e = _load()
    assert e2e.resolve_keys({}) == (None, None, "no_sandbox_key")
    prod = {"ASAAS_SANDBOX_API_KEY": "$aact_prod_0000000000000000000"}
    assert e2e.resolve_keys(prod) == (None, None, "key_not_sandbox")


def test_the_default_key_plays_both_roles_when_no_scoped_key_exists():
    e2e = _load()
    key = "$aact_hmlg_0000000000000000000"
    assert e2e.resolve_keys({"ASAAS_SANDBOX_API_KEY": key}) == (key, key, None)
    scoped = "$aact_hmlg_1111111111111111111"
    env = {"ASAAS_SANDBOX_API_KEY": key, "ASAAS_SANDBOX_API_KEY_SCOPED": scoped}
    assert e2e.resolve_keys(env) == (key, scoped, None)


def test_main_without_keys_skips_before_touching_the_database_or_the_network(monkeypatch, capsys):
    import asyncio

    e2e = _load()
    monkeypatch.delenv("ASAAS_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("ASAAS_SANDBOX_API_KEY_SCOPED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert asyncio.run(e2e.main()) == 0
    assert capsys.readouterr().out.strip() == "skipped_all | reason=no_sandbox_key"
