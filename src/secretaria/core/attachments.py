"""Attachments on the Brain-Message channel - secretarIA's copy of the contract.

The limits, the accepted kinds and the name rules are brain-api's
(`brain-api/src/brain_api/core/attachments.py`, contract in
`brain-api/docs/CHECKPOINT_brain_message_anexos.md` §4-§5), REPEATED here, not
imported: the two services share no package, and extracting one while this is
the only consumer on this side is the premature abstraction the
`brain-shared-python-library` skill exists to prevent (see
docs/CHECKPOINT_brain_message_anexos_secretaria.md). Repeated means repeated:
brain-api promises a patient that a file it accepted will go through, so this
side must accept everything that side accepts - same integer, same magic bytes,
same name rules. `tests/test_brain_message_attachments.py` pins the numbers.

secretarIA re-validates anyway (defence in depth - "the other edge already
checked" is never a reason to skip the check here), and it is the FIRST edge for
the staff console, which uploads straight to this service.

Pure on purpose: no Starlette, no storage, no database. The HTTP reading lives in
`api/attachment_upload.py`, the bytes in `services/media_storage.py`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

#: 20 MiB, in BYTES (20971520) - brain-api's `MAX_ATTACHMENT_BYTES`, byte for byte.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
#: Room for the text fields, part headers and boundaries - brain-api's value.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
#: Bytes needed to tell every accepted kind apart (WEBP's marker ends at offset 12).
SNIFF_BYTES = 16
#: Longest display name kept, extension included - brain-api's value.
MAX_FILENAME_CHARS = 120


@dataclass(frozen=True)
class AttachmentKind:
    """One accepted kind of file: the type it travels as, and the names it may carry."""

    content_type: str
    extension: str
    extensions: frozenset[str]
    #: "image" | "document": a name may slip WITHIN a family, never across one.
    family: str


JPEG = AttachmentKind("image/jpeg", "jpg", frozenset({"jpg", "jpeg", "jpe", "jfif"}), "image")
PNG = AttachmentKind("image/png", "png", frozenset({"png"}), "image")
WEBP = AttachmentKind("image/webp", "webp", frozenset({"webp"}), "image")
GIF = AttachmentKind("image/gif", "gif", frozenset({"gif"}), "image")
PDF = AttachmentKind("application/pdf", "pdf", frozenset({"pdf"}), "document")

#: The ONLY types a stored attachment can carry, in either direction. SVG is absent on
#: purpose: an image that can carry script.
ALLOWED_KINDS: dict[str, AttachmentKind] = {
    kind.content_type: kind for kind in (JPEG, PNG, WEBP, GIF, PDF)
}
_KIND_BY_EXTENSION = {ext: kind for kind in ALLOWED_KINDS.values() for ext in kind.extensions}

# --- Refusals ---------------------------------------------------------------------------
# The first six are brain-api's codes with brain-api's statuses. The rest exist only on
# this side, because only this side knows the conversation (consent), the storage (quota,
# availability) and the staff console (channel).

ATTACHMENT_EMPTY = "attachment_empty"
ATTACHMENT_TOO_LARGE = "attachment_too_large"
ATTACHMENT_TYPE_UNSUPPORTED = "attachment_type_unsupported"
ATTACHMENT_TYPE_MISMATCH = "attachment_type_mismatch"
ATTACHMENT_MALFORMED = "attachment_malformed"
ATTACHMENT_NOT_FOUND = "attachment_not_found"
ATTACHMENT_CONSENT_REQUIRED = "attachment_consent_required"
ATTACHMENT_QUOTA_EXCEEDED = "attachment_quota_exceeded"
ATTACHMENT_UNSUPPORTED_FOR_CHANNEL = "attachment_unsupported_for_channel"
ATTACHMENT_STORAGE_UNAVAILABLE = "attachment_storage_unavailable"

_MAX_MB = MAX_ATTACHMENT_BYTES // (1024 * 1024)

#: code -> (HTTP status, the sentence shown as-is). Every 4xx is PERMANENT for the request
#: as sent (`brain-mesh-permanent-vs-transient-refusal`): resending it unchanged never passes
#: - consent and quota only pass once the STATE changes (terms accepted, a day gone by). The
#: one 5xx, storage unavailable, is the transient one: the same request may pass later.
REFUSALS: dict[str, tuple[int, str]] = {
    ATTACHMENT_EMPTY: (422, "O arquivo está vazio. Escolha outro arquivo."),
    ATTACHMENT_TOO_LARGE: (
        413,
        f"O arquivo passa do limite de {_MAX_MB} MB. Envie um arquivo menor.",
    ),
    ATTACHMENT_TYPE_UNSUPPORTED: (
        415,
        "Tipo de arquivo não aceito. Envie uma imagem (JPG, PNG, WEBP ou GIF) ou um PDF.",
    ),
    ATTACHMENT_TYPE_MISMATCH: (
        422,
        "O conteúdo do arquivo não corresponde à extensão do nome. "
        "Confira o arquivo e envie de novo.",
    ),
    ATTACHMENT_MALFORMED: (422, 'Envio inválido: mande um único arquivo, no campo "file".'),
    ATTACHMENT_NOT_FOUND: (404, "Arquivo não encontrado."),
    ATTACHMENT_CONSENT_REQUIRED: (
        409,
        "Para enviar arquivos, aceite primeiro os Termos de Uso e a Política de Privacidade "
        "nesta conversa.",
    ),
    ATTACHMENT_QUOTA_EXCEEDED: (
        429,
        "O limite diário de envio de arquivos foi atingido. Tente novamente amanhã.",
    ),
    ATTACHMENT_UNSUPPORTED_FOR_CHANNEL: (
        422,
        "Envio de arquivos só está disponível para pacientes do Portal, não do WhatsApp.",
    ),
    ATTACHMENT_STORAGE_UNAVAILABLE: (
        503,
        "O armazenamento de arquivos está indisponível no momento. Tente de novo em instantes.",
    ),
}


class AttachmentRefused(Exception):
    """A refusal the caller can act on. Carries its CODE only - never a name, never a byte."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code, self.message = REFUSALS[code]

    @property
    def detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.code == ATTACHMENT_TOO_LARGE:
            body["max_bytes"] = MAX_ATTACHMENT_BYTES
        return body


@dataclass(frozen=True)
class CheckedAttachment:
    """An upload that passed `check_attachment`: its REAL kind, a safe name, its real size."""

    kind: AttachmentKind
    filename: str
    size_bytes: int


def sniff_kind(head: bytes) -> AttachmentKind | None:
    """The kind the CONTENT says it is, from its magic bytes - or None.

    Never the extension, never the sender's Content-Type. PDF only at offset 0, so a
    polyglot that merely CONTAINS a PDF header further in is refused.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head.startswith(b"%PDF-"):
        return PDF
    return None


_RESERVED = re.compile(r'[<>:"|?*]')
# Controls, format marks (bidi overrides, zero-width, tags, soft hyphen), private use,
# surrogates, unassigned: removed by CATEGORY, never by a hand-written list of invisible
# characters (brain-api's review found a list missing seven; see its CHECKPOINT §6.17).
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
_SPACES = re.compile(r"\s+")
_EXTENSION = re.compile(r"[a-z][a-z0-9]{0,9}")


def _clean(raw: str | None) -> str:
    """The last path segment of `raw`, without what is unsafe in a header, a path or a UI."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    name = _RESERVED.sub("", name)
    return _SPACES.sub(" ", name).strip().lstrip(".").strip()


def _split_name(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and _EXTENSION.fullmatch(ext.lower()):
        return stem, ext.lower()
    return name, ""


def extension_conflicts(raw_filename: str | None, kind: AttachmentKind) -> bool:
    """Whether the NAME promises another family than the CONTENT is (or an unknown type)."""
    _, ext = _split_name(_clean(raw_filename))
    if not ext or ext in kind.extensions:
        return False
    claimed = _KIND_BY_EXTENSION.get(ext)
    return claimed is None or claimed.family != kind.family


def safe_filename(raw_filename: str | None, kind: AttachmentKind) -> str:
    """A display name safe to store and render, with an extension honest about the bytes.

    May still carry PII (patients name files after themselves): stored and displayed,
    NEVER logged.
    """
    stem, ext = _split_name(_clean(raw_filename))
    if ext not in kind.extensions:
        ext = kind.extension
    stem = stem[: MAX_FILENAME_CHARS - len(ext) - 1].rstrip(" .") or "anexo"
    return f"{stem}.{ext}"


def check_attachment(head: bytes, size_bytes: int, raw_filename: str | None) -> CheckedAttachment:
    """Size, then the real kind, then whether the name agrees. Raises `AttachmentRefused`."""
    if size_bytes <= 0:
        raise AttachmentRefused(ATTACHMENT_EMPTY)
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise AttachmentRefused(ATTACHMENT_TOO_LARGE)
    kind = sniff_kind(head)
    if kind is None:
        raise AttachmentRefused(ATTACHMENT_TYPE_UNSUPPORTED)
    if extension_conflicts(raw_filename, kind):
        raise AttachmentRefused(ATTACHMENT_TYPE_MISMATCH)
    return CheckedAttachment(
        kind=kind, filename=safe_filename(raw_filename, kind), size_bytes=size_bytes
    )


# --- The stored record (`messages.attachment`) --------------------------------------------


class StoredAttachment(BaseModel):
    """`Message.attachment` as written: where the bytes are, and what they are.

    `r2_object_key` never leaves this service - every projection drops it, and the two
    media routes stream the bytes themselves (no URL, no redirect).
    """

    r2_object_key: str
    content_type: str
    size_bytes: int
    filename: str


def attachment_record(checked: CheckedAttachment, object_key: str) -> dict[str, Any]:
    """The JSON blob `messages.attachment` stores for an upload that passed the checks."""
    return StoredAttachment(
        r2_object_key=object_key,
        content_type=checked.kind.content_type,
        size_bytes=checked.size_bytes,
        filename=checked.filename,
    ).model_dump()


def stored_attachment_or_none(blob: object, *, message_id: object) -> StoredAttachment | None:
    """A row's blob, validated, or None - tolerant like `interactive_read_or_none`.

    A blob that no longer matches the contract (a type outside the five, a size outside
    (0, 20 MiB]) costs THAT message its file, never the whole thread; and a type outside the
    five is never streamed, whatever the row says.
    """
    if not blob:
        return None
    try:
        record = StoredAttachment.model_validate(blob)
    except ValidationError:
        record = None
    if (
        record is None
        or record.content_type not in ALLOWED_KINDS
        or not 0 < record.size_bytes <= MAX_ATTACHMENT_BYTES
        or not record.r2_object_key
    ):
        logger.warning("message_attachment_invalid", message_id=str(message_id))
        return None
    return record


#: What `body` holds for a file sent WITHOUT a caption. `body` is never empty on an
#: attachment row: it is the history line the agent reads (`ai/graph.py::_load_history`), the
#: text a console that predates `attachment` renders, and the channel-neutral record that a
#: file arrived. With a caption, `body` is the caption alone - as on WhatsApp - so a screen
#: that draws the file can omit exactly this string when it equals `body`.
ATTACHMENT_PLACEHOLDER_TEMPLATE = "[anexo: {filename}]"


def attachment_placeholder(filename: str) -> str:
    return ATTACHMENT_PLACEHOLDER_TEMPLATE.format(filename=filename)


def attachment_body(caption: str | None, filename: str) -> str:
    """The `body` of an attachment row: the caption, or the placeholder when there is none."""
    if caption is not None and caption.strip():
        return caption
    return attachment_placeholder(filename)


def content_disposition(content_type: str) -> str:
    """Inline for an image, download for a PDF, under a GENERIC name.

    The real name (possible PII) travels in the JSON listing, never in a header that
    proxies and browsers log.
    """
    kind = ALLOWED_KINDS[content_type]
    disposition = "inline" if kind.family == "image" else "attachment"
    return f'{disposition}; filename="anexo.{kind.extension}"'


#: Headers every media response carries, whichever route serves it.
MEDIA_RESPONSE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "private, no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
