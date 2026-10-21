"""Fetch a public web page for context ingestion without being turned against our network.

The manager types a URL and the server fetches it, so the URL is attacker-controlled input
(SSRF). What this module guarantees, hop by hop (redirects are followed by hand):

  * scheme is http/https, no credentials in the URL, port is 80/443, host has a dot;
  * the host is RESOLVED HERE and every address must be public (no loopback, private,
    link-local, cloud-metadata, CGNAT, multicast, reserved, NAT64/6to4/Teredo ranges);
  * the connection goes to the address we validated (no second DNS lookup that could answer
    differently - DNS rebinding), with the original name as Host header and TLS SNI;
  * at most 3 redirects, each one re-validated from scratch;
  * 10 s timeout, 2 MiB cap measured AFTER decompression, only html/plain-text answers;
  * the body is decompressed HERE, with an output limit: only gzip is asked for and accepted
    (httpx decodes zstd and stacked encodings with no limit - a 16 KB body inflated to 1 GiB).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

# httpx logs every request - the pinned IP, the path and the QUERY - at INFO. Nothing of the URL may
# reach a log (SPEC G3 / acceptance 4), and this client is the only thing that touches it.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_URL_CHARS = 2048
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
ALLOWED_PORTS = frozenset({80, 443})
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
USER_AGENT = "SecretarIA-ContextBot/1.0"

_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(ValueError):
    """The URL (or a redirect target) must not be fetched. `code` is stable for the UI/tests."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FetchError(Exception):
    """The page could not be fetched (network, status, size, type). `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchedPage:
    final_url: str
    content_type: str
    text: str


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version):
        return False
    return ip.is_global and not ip.is_multicast


def _ip_literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def validate_url(raw: str) -> _Target:
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise UnsafeUrlError("invalid_url")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("scheme")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("no_host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UnsafeUrlError("port") from None
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("port")
    literal = _ip_literal(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeUrlError("private_address")
        host = literal
    elif "." not in host:
        raise UnsafeUrlError("no_dot")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return _Target(parts.scheme, host, port, path)


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeUrlError("unresolvable") from None
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def _netloc(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _host_header(target: _Target) -> str:
    default = 443 if target.scheme == "https" else 80
    host = f"[{target.host}]" if ":" in target.host else target.host
    return host if target.port == default else f"{host}:{target.port}"


def _content_codings(response: httpx.Response) -> list[str]:
    raw = response.headers.get("content-encoding", "")
    codings = [c.strip().lower() for c in raw.split(",") if c.strip()]
    return [c for c in codings if c != "identity"]


async def _read_capped(response: httpx.Response) -> bytes:
    """The body, never more than MAX_BYTES of it IN MEMORY, decompressed or not.

    The wire bytes are read raw and gzip is inflated here with a hard output limit; httpx's own
    decoders have none. Any other (or stacked) content-coding is refused unread.
    """
    codings = _content_codings(response)
    if codings not in ([], ["gzip"]):
        raise FetchError("content_encoding")
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES and not codings:
        raise FetchError("too_large")
    inflater = zlib.decompressobj(31) if codings else None  # 31 = gzip wrapper only
    chunks: list[bytes] = []
    total = 0

    def keep(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > MAX_BYTES:
            raise FetchError("too_large")
        chunks.append(data)

    try:
        async for chunk in response.aiter_raw():
            if inflater is None:
                keep(chunk)
                continue
            data = chunk
            while data:
                out = inflater.decompress(data, MAX_BYTES - total + 1)
                keep(out)
                data = inflater.unconsumed_tail
                if not out:
                    break
        if inflater is not None:
            while out := inflater.decompress(b"", MAX_BYTES - total + 1):
                keep(out)
    except zlib.error:
        raise FetchError("bad_body") from None
    return b"".join(chunks)


def _decode(body: bytes, content_type_header: str) -> str:
    charset = "utf-8"
    for param in content_type_header.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip("\"' ")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch_public_page(
    url: str,
    *,
    resolver: Resolver = resolve_host,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedPage:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target = validate_url(current)
        literal = _ip_literal(target.host)
        addresses = [literal] if literal is not None else await resolver(target.host, target.port)
        if not addresses:
            raise UnsafeUrlError("unresolvable")
        if not all(is_public_ip(a) for a in addresses):
            raise UnsafeUrlError("private_address")
        pinned = f"{target.scheme}://{_netloc(addresses[0], target.port)}{target.path}"
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        headers = {
            "Host": _host_header(target),
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
            "Accept-Encoding": "gzip",
        }
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET", pinned, headers=headers, extensions=extensions
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("bad_redirect")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"http_{response.status_code}")
                    header = response.headers.get("content-type", "")
                    main_type = header.split(";")[0].strip().lower()
                    if main_type not in ALLOWED_CONTENT_TYPES:
                        raise FetchError("content_type")
                    body = await _read_capped(response)
                    return FetchedPage(current, main_type, _decode(body, header))
        except httpx.TimeoutException:
            raise FetchError("timeout") from None
        except httpx.HTTPError:
            raise FetchError("network") from None
    raise FetchError("too_many_redirects")
