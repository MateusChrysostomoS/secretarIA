"""Architecture guards of the payment core: rules that must hold for EVERY future change.

These read the SOURCE, so a refund call or a `split` cannot sneak in behind a passing
behavioural test (decision D15; spec sections 5.1, 14 and risk 18).
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "secretaria"
PAYMENT_SOURCES = sorted(
    [*(ROOT / "services" / "payments").glob("*.py"), ROOT / "workers" / "payments_tasks.py"]
    + [ROOT / "api" / "webhook_payments.py", ROOT / "api" / "webhook_asaas.py"]
)

# Identifiers that would return money. `refund_due`, `mark_refund_seen`, `REFUNDED`,
# `pix_refund_window_hours`... are NOT in this list: they calculate or observe, never act.
_FORBIDDEN_NAMES = ("refund_payment", "refund_charge", "refundcharge", "create_refund", "do_refund")
# The ONE place a refund path may appear as text: the qualification probe of the
# permission-group verdict (a POST on a NON-EXISTENT id that only reads the error code).
_REFUND_PATH_ALLOWED_IN = {"asaas_probes.py"}


def _names_and_strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id, node
        elif isinstance(node, ast.Attribute):
            yield node.attr, node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, ast.arg):
            yield node.arg, node
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg, node
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node


@pytest.mark.parametrize("path", PAYMENT_SOURCES, ids=lambda p: p.name)
def test_no_payment_source_calls_or_defines_a_refund(path):
    for text, node in _names_and_strings(path):
        lowered = text.lower()
        for forbidden in _FORBIDDEN_NAMES:
            assert forbidden not in lowered, f"{path.name}:{node.lineno} mentions {forbidden}"
        if "/refund" in lowered and path.name not in _REFUND_PATH_ALLOWED_IN:
            pytest.fail(f"{path.name}:{node.lineno} contains a refund URL path")


def test_the_asaas_client_and_provider_expose_no_refund_method():
    from secretaria.services.payments.asaas import AsaasClient
    from secretaria.services.payments.asaas_provider import AsaasProvider

    for cls in (AsaasClient, AsaasProvider):
        assert not any("refund" in name.lower() for name in dir(cls)), cls.__name__


def test_no_charge_body_is_ever_built_with_a_split():
    """A Cobrancas-only key may (NOT VERIFIED) be able to split a charge to another wallet.
    Our code never sends `split`, so the risk stays theoretical."""
    banned = {"split", "splits", "splitrefunds"}
    for path in PAYMENT_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            literal = isinstance(node, ast.Constant) and isinstance(node.value, str)
            keyword = isinstance(node, ast.keyword) and node.arg
            name = node.value if literal else (node.arg if keyword else "")
            assert name.lower() not in banned, f"{path.name}:{node.lineno} builds a split"
