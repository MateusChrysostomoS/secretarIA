"""Provider registry: name -> `PaymentProvider` singleton.

Built-in providers are imported lazily on first use so importing this module (from the
webhook handler, the jobs, the lifecycle) never drags a provider's HTTP client in early or
creates an import cycle. Tests replace an entry with
`monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)`.

Adding a provider (Mercado Pago, F3) = one line in `_BUILTIN` + its wrapper module; nothing
else in the system changes (that is the point of the abstraction).
"""

import importlib

from secretaria.services.payments.provider import PaymentProvider

_PROVIDERS: dict[str, PaymentProvider] = {}

# name -> "module:Class" of a wrapper with a no-argument constructor.
_BUILTIN: dict[str, str] = {
    "asaas": "secretaria.services.payments.asaas_provider:AsaasProvider",
}


class UnknownProviderError(KeyError):
    """No provider is registered under that name."""


def register_provider(provider: PaymentProvider) -> None:
    _PROVIDERS[provider.name] = provider


def find_provider(name: str) -> PaymentProvider | None:
    """The provider called `name`, or None. Safe for untrusted input (a URL segment)."""
    if name in _PROVIDERS:
        return _PROVIDERS[name]
    target = _BUILTIN.get(name)
    if target is None:
        return None
    module_name, _, class_name = target.partition(":")
    provider = getattr(importlib.import_module(module_name), class_name)()
    _PROVIDERS[name] = provider
    return provider


def get_provider(name: str) -> PaymentProvider:
    provider = find_provider(name)
    if provider is None:
        raise UnknownProviderError(name)
    return provider
