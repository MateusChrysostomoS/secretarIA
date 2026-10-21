"""Turn a fetched page or an uploaded document into plain text (TASK-026).

Inputs are attacker-influenced (a URL's HTML, a file the manager uploads), so every format has a
hard cap and fails with a STABLE code instead of an exception message:

  * html  - stdlib `html.parser`; drops script/style/svg/template, keeps the title, the meta
            description and any JSON-LD block (clinic sites often publish address/hours there);
  * pdf   - `pypdf`, page-capped; encrypted files and scans without a text layer are refused;
  * docx  - `zipfile` + `ElementTree` on word/document.xml with a decompressed-size cap;
  * txt   - utf-8, falling back to latin-1; NUL bytes mean "not text".

No OCR: a scanned PDF is reported as `no_text` so the manager can paste the text instead.
"""

from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from typing import Literal
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 60_000
MAX_PDF_PAGES = 60
# pypdf interprets page content in pure Python and superlinearly (3 MB of operators took 38 s):
# past this much DECODED content across the pages the file is refused before it is interpreted.
MAX_PDF_CONTENT_BYTES = 1024 * 1024
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
MAX_JSON_LD_CHARS = 5_000
MIN_USEFUL_CHARS = 20

Kind = Literal["pdf", "docx", "txt"]

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
_BLOCK = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "address",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "dt",
        "dd",
        "blockquote",
    }
)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractionError(Exception):
    """The document cannot be turned into text. `code` is stable for the UI and tests."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > MAX_TEXT_CHARS:
        cut = text[:MAX_TEXT_CHARS]
        last_break = cut.rfind("\n")
        text = cut[:last_break] if last_break > MAX_TEXT_CHARS // 2 else cut
    return text


# ----------------------------------------------------------------------------- html


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.description = ""
        self.json_ld: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_json_ld = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("name") or "").lower() == "description":
            self.description = (attributes.get("content") or "").strip()
        if tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
            self._in_json_ld = True
            self.json_ld.append("")
            return
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self._skip_depth += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            return
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self.json_ld[-1] += data
        elif self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - html.parser can choke on garbage; keep what we have
        pass
    head = []
    if parser.title.strip():
        head.append("Título da página: " + parser.title.strip())
    if parser.description:
        head.append("Descrição da página: " + parser.description)
    structured = [block.strip()[:MAX_JSON_LD_CHARS] for block in parser.json_ld if block.strip()]
    # A closing and an opening block tag both emit a break; for web pages one break is enough.
    body = re.sub(r"\n[ \t ]*(?:\n[ \t ]*)+", "\n", "".join(parser.parts))
    sections = ["\n".join(head), body]
    if structured:
        sections.append("Dados estruturados da página:\n" + "\n".join(structured))
    return clean_text("\n\n".join(s for s in sections if s))


# ------------------------------------------------------------------------------ pdf


def _content_bytes(page) -> int:
    """Decoded size of a page's content stream(s), without interpreting a single operator."""
    contents = page.get("/Contents")
    contents = contents.get_object() if contents is not None else None
    if contents is None:
        return 0
    streams = contents if isinstance(contents, list) else [contents]
    return sum(len(stream.get_object().get_data()) for stream in streams)


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise ExtractionError("pdf_unavailable") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("encrypted")
        pages = reader.pages[:MAX_PDF_PAGES]
        if sum(_content_bytes(page) for page in pages) > MAX_PDF_CONTENT_BYTES:
            raise ExtractionError("too_large")
        text = "\n\n".join((page.extract_text() or "") for page in pages)
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError):
        raise ExtractionError("unreadable") from None
    text = clean_text(text)
    if len(text) < MIN_USEFUL_CHARS:
        raise ExtractionError("no_text")
    return text


# ----------------------------------------------------------------------------- docx


def docx_to_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError:
                raise ExtractionError("unreadable") from None
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ExtractionError("too_large")
            # `file_size` is whatever the uploader wrote in the header, and read(n) inflates up to
            # n bytes BEFORE it truncates: small blocks keep the peak independent of that claim.
            blocks: list[bytes] = []
            size = 0
            with archive.open(info) as member:
                while block := member.read(64 * 1024):
                    size += len(block)
                    if size > MAX_DOCX_XML_BYTES:
                        raise ExtractionError("too_large")
                    blocks.append(block)
            xml = b"".join(blocks)
    except zipfile.BadZipFile:
        raise ExtractionError("unreadable") from None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise ExtractionError("unreadable") from None
    paragraphs: list[str] = []
    for paragraph in root.iter(_W + "p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == _W + "t" and node.text:
                pieces.append(node.text)
            elif node.tag == _W + "tab":
                pieces.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                pieces.append("\n")
        paragraphs.append("".join(pieces))
    text = clean_text("\n".join(paragraphs))
    if len(text) < MIN_USEFUL_CHARS:
        raise ExtractionError("no_text")
    return text


# ------------------------------------------------------------------------------ txt


def txt_to_text(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        raise ExtractionError("unsupported_type")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    text = clean_text(text)
    if len(text) < MIN_USEFUL_CHARS:
        raise ExtractionError("no_text")
    return text


# --------------------------------------------------------------------------- upload


def detect_kind(head: bytes, filename: str | None) -> Kind:
    """The kind of an upload, decided by CONTENT and cross-checked with the extension."""
    name = filename or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        if extension == "docx":
            return "docx"
        raise ExtractionError("unsupported_type")  # a zip, xlsx, pptx, ... is not accepted
    if extension in {"txt", "md", "text"} and b"\x00" not in head:
        return "txt"
    raise ExtractionError("unsupported_type")


def extract_upload_text(data: bytes, filename: str | None) -> str:
    if not data:
        raise ExtractionError("empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtractionError("too_large")
    kind = detect_kind(data[:16], filename)
    if kind == "pdf":
        return pdf_to_text(data)
    if kind == "docx":
        return docx_to_text(data)
    return txt_to_text(data)
