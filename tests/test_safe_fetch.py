"""SSRF guard for the context ingestion URL fetcher (TASK-026). No real network is used."""

from __future__ import annotations

import httpx
import pytest

from secretaria.services import safe_fetch
from secretaria.services.safe_fetch import (
    FetchError,
    UnsafeUrlError,
    fetch_public_page,
    is_public_ip,
    validate_url,
)

PUBLIC = "93.184.216.34"
HTML = "<html><body><h1>Clínica</h1></body></html>"


def _resolver(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def _by_host(mapping: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        return mapping[host]

    return resolve


class _Recorder:
    """A MockTransport handler that records every request it was asked to serve."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if response.is_stream_consumed:
            # `content=` responses are read (and decoded) when constructed; a real server's body
            # is a stream the client reads raw. Serve it that way: the fetcher sees what it would.
            response = httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(response.content),
            )
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _ok(body: str = HTML, content_type: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": content_type})


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


# ------------------------------------------------------------------ is_public_ip


@pytest.mark.parametrize("address", ["8.8.8.8", PUBLIC, "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_ip(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.1.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:a00:1::1",
        "not-an-ip",
    ],
)
def test_internal_and_special_addresses_are_blocked(address: str) -> None:
    assert not is_public_ip(address)


# ------------------------------------------------------------------- validate_url


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "invalid_url"),
        ("   ", "invalid_url"),
        ("http://example.com/" + "a" * 3000, "invalid_url"),
        ("ftp://example.com/", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://example.com/", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("http://user:pass@example.com/", "credentials"),
        ("http://admin@example.com/", "credentials"),
        ("http:///path", "no_host"),
        ("http://example.com:8080/", "port"),
        ("http://example.com:22/", "port"),
        ("http://example.com:notaport/", "port"),
        ("http://localhost/", "no_dot"),
        ("http://intranet/", "no_dot"),
        ("http://2130706433/", "no_dot"),
        ("http://127.0.0.1/", "private_address"),
        ("http://10.0.0.5/admin", "private_address"),
        ("http://169.254.169.254/latest/meta-data/", "private_address"),
        ("http://[::1]/", "private_address"),
        ("http://[::ffff:127.0.0.1]/", "private_address"),
        ("http://0.0.0.0/", "private_address"),
    ],
)
def test_hostile_urls_are_refused_before_any_lookup(url: str, code: str) -> None:
    with pytest.raises(UnsafeUrlError) as caught:
        validate_url(url)
    assert caught.value.code == code


def test_a_normal_url_is_accepted_and_normalised() -> None:
    target = validate_url("  HTTPS://Clinica.Example.com./contato?x=1  ")
    assert (target.scheme, target.host, target.port) == ("https", "clinica.example.com", 443)
    assert target.path == "/contato?x=1"


# ------------------------------------------------------------------ fetch: happy path


async def test_the_validated_address_is_pinned_with_host_and_sni() -> None:
    server = _Recorder(_ok())
    page = await fetch_public_page(
        "https://clinica.example.com/contato",
        resolver=_resolver(PUBLIC),
        transport=server.transport,
    )
    assert page.text == HTML and page.content_type == "text/html"
    (request,) = server.requests
    assert request.url.host == PUBLIC and request.url.scheme == "https"
    assert request.headers["host"] == "clinica.example.com"
    assert request.extensions["sni_hostname"] == "clinica.example.com"


async def test_a_public_ip_literal_is_fetched_without_a_lookup() -> None:
    async def never(host: str, port: int) -> list[str]:
        raise AssertionError("no DNS lookup for an IP literal")

    server = _Recorder(_ok())
    await fetch_public_page(f"http://{PUBLIC}/", resolver=never, transport=server.transport)
    assert server.requests[0].url.host == PUBLIC


async def test_ipv6_literal_is_bracketed_in_the_host_header() -> None:
    server = _Recorder(_ok())
    await fetch_public_page(
        "http://[2606:4700:4700::1111]/x", resolver=_resolver(), transport=server.transport
    )
    assert server.requests[0].headers["host"] == "[2606:4700:4700::1111]"


async def test_the_declared_charset_is_honoured() -> None:
    server = _Recorder(
        httpx.Response(
            200,
            content="Clínica São José".encode("latin-1"),
            headers={"content-type": "text/plain; charset=iso-8859-1"},
        )
    )
    page = await fetch_public_page(
        "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
    )
    assert page.text == "Clínica São José"


# ---------------------------------------------------------- fetch: never the inside


async def test_a_host_that_resolves_to_a_private_address_is_never_contacted() -> None:
    server = _Recorder(_ok())
    with pytest.raises(UnsafeUrlError) as caught:
        await fetch_public_page(
            "http://evil.example.com/", resolver=_resolver("10.0.0.7"), transport=server.transport
        )
    assert caught.value.code == "private_address" and server.requests == []


async def test_one_private_answer_among_public_ones_blocks_the_host() -> None:
    server = _Recorder(_ok())
    with pytest.raises(UnsafeUrlError):
        await fetch_public_page(
            "http://rebind.example.com/",
            resolver=_resolver(PUBLIC, "127.0.0.1"),
            transport=server.transport,
        )
    assert server.requests == []


async def test_a_host_that_does_not_resolve_is_refused() -> None:
    with pytest.raises(UnsafeUrlError) as caught:
        await fetch_public_page("http://nada.example.com/", resolver=_resolver())
    assert caught.value.code == "unresolvable"


async def test_a_redirect_to_the_cloud_metadata_address_is_blocked() -> None:
    server = _Recorder(_redirect("http://169.254.169.254/latest/meta-data/"))
    with pytest.raises(UnsafeUrlError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "private_address" and len(server.requests) == 1


async def test_a_redirect_to_a_host_that_resolves_private_is_blocked() -> None:
    server = _Recorder(_redirect("http://internal.example.com/admin"))
    resolver = _by_host({"clinica.example.com": [PUBLIC], "internal.example.com": ["192.168.0.9"]})
    with pytest.raises(UnsafeUrlError):
        await fetch_public_page(
            "http://clinica.example.com/", resolver=resolver, transport=server.transport
        )
    assert len(server.requests) == 1


async def test_a_redirect_to_another_scheme_is_blocked() -> None:
    server = _Recorder(_redirect("file:///etc/passwd"))
    with pytest.raises(UnsafeUrlError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "scheme"


async def test_a_relative_redirect_is_followed_and_revalidated() -> None:
    server = _Recorder(_redirect("/novo-endereco"), _ok("<p>oi</p>"))
    page = await fetch_public_page(
        "http://clinica.example.com/antigo", resolver=_resolver(PUBLIC), transport=server.transport
    )
    assert page.final_url == "http://clinica.example.com/novo-endereco"
    assert server.requests[1].url.path == "/novo-endereco"


async def test_too_many_redirects_stop() -> None:
    server = _Recorder(*[_redirect("/loop") for _ in range(10)])
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "too_many_redirects"
    assert len(server.requests) == safe_fetch.MAX_REDIRECTS + 1


async def test_a_redirect_without_location_is_an_error() -> None:
    server = _Recorder(httpx.Response(302))
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "bad_redirect"


# ------------------------------------------------------- fetch: size, type, failures


async def test_a_pdf_or_binary_answer_is_refused_by_content_type() -> None:
    server = _Recorder(
        httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})
    )
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "content_type"


async def test_a_missing_page_reports_its_status() -> None:
    server = _Recorder(httpx.Response(404, content=b"nope", headers={"content-type": "text/html"}))
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "http_404"


async def test_an_oversized_body_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "MAX_BYTES", 100)
    server = _Recorder(_ok("x" * 500))
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "too_large"


async def test_an_oversized_declared_length_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(safe_fetch, "MAX_BYTES", 100)
    server = _Recorder(
        httpx.Response(
            200, content=b"ok", headers={"content-type": "text/html", "content-length": "999999"}
        )
    )
    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
        )
    assert caught.value.code == "too_large"


@pytest.mark.parametrize(
    ("error", "code"),
    [(httpx.ConnectTimeout("t"), "timeout"), (httpx.ConnectError("boom"), "network")],
)
async def test_network_failures_become_stable_codes(error, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(FetchError) as caught:
        await fetch_public_page(
            "http://clinica.example.com/",
            resolver=_resolver(PUBLIC),
            transport=httpx.MockTransport(handler),
        )
    assert caught.value.code == code


# ------------------------------------------------- security review: decoding is bounded


async def _fetch_from(server: _Recorder):
    return await fetch_public_page(
        "http://clinica.example.com/", resolver=_resolver(PUBLIC), transport=server.transport
    )


@pytest.mark.parametrize("encoding", ["zstd", "br", "gzip, gzip", "deflate", "gzip, zstd"])
async def test_an_encoding_it_will_not_decode_is_refused_before_any_decoding(encoding: str) -> None:
    # httpx decodes zstd and stacked encodings with NO output limit: a 16 KB body peaked at 1 GiB.
    server = _Recorder(
        httpx.Response(
            200,
            stream=httpx.ByteStream(b"x"),
            headers={"content-type": "text/html", "content-encoding": encoding},
        )
    )
    with pytest.raises(FetchError) as caught:
        await _fetch_from(server)
    assert caught.value.code == "content_encoding"


async def test_only_gzip_is_asked_for() -> None:
    server = _Recorder(_ok())
    await _fetch_from(server)
    assert server.requests[0].headers["accept-encoding"] == "gzip"


async def test_a_gzip_page_is_read() -> None:
    import gzip

    body = gzip.compress(HTML.encode())
    server = _Recorder(
        httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"content-type": "text/html", "content-encoding": "gzip"},
        )
    )
    page = await _fetch_from(server)
    assert "Clínica" in page.text


async def test_a_gzip_bomb_is_cut_off_without_inflating_it() -> None:
    import gzip
    import tracemalloc

    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), 9)  # a few dozen KB on the wire
    server = _Recorder(
        httpx.Response(
            200,
            stream=httpx.ByteStream(bomb),
            headers={"content-type": "text/html", "content-encoding": "gzip"},
        )
    )
    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as caught:
            await _fetch_from(server)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert caught.value.code == "too_large"
    assert peak < 24 * 1024 * 1024  # the 2 MiB cap, not the 64 MiB the body would become


def test_the_deprecated_ipv4_compatible_range_is_not_public() -> None:
    assert not is_public_ip("::7f00:1")  # ::/96 - 127.0.0.1 in disguise
    assert not is_public_ip("::a00:1")


async def test_the_http_client_never_logs_the_url(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)  # production's root level; httpx logs every request at INFO
    server = _Recorder(_ok())
    await fetch_public_page(
        "http://clinica.example.com/segredo?token=abc123",
        resolver=_resolver(PUBLIC),
        transport=server.transport,
    )
    assert "abc123" not in caplog.text and "segredo" not in caplog.text
