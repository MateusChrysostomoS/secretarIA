"""Text extraction for context sources (TASK-026). Every document is built in memory."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from secretaria.services import text_extract
from secretaria.services.text_extract import (
    ExtractionError,
    clean_text,
    detect_kind,
    docx_to_text,
    extract_upload_text,
    html_to_text,
    pdf_to_text,
    txt_to_text,
)

TEXT = "Clínica Olhar Claro fica na Rua das Flores, 10, Recife. Estacionamento no local."


def _docx(*paragraphs: str, extra_xml: str = "") -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}{extra_xml}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _pdf_with_text(text: str) -> bytes:
    """A minimal valid one-page PDF whose single text object says `text`."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


# ------------------------------------------------------------------------------ html


def test_html_keeps_visible_text_title_and_description_and_drops_scripts() -> None:
    html = (
        "<html><head><title>Olhar Claro</title>"
        '<meta name="description" content="Oftalmologia em Recife">'
        "<style>.x{color:red}</style><script>var secret = 1;</script></head>"
        "<body><h1>Contato</h1><p>Rua das Flores, 10</p>"
        "<noscript>ative o javascript</noscript><div>Aberto de segunda a sexta</div></body></html>"
    )
    text = html_to_text(html)
    assert "Título da página: Olhar Claro" in text
    assert "Descrição da página: Oftalmologia em Recife" in text
    assert "Rua das Flores, 10" in text and "Aberto de segunda a sexta" in text
    assert "secret" not in text and "color:red" not in text and "ative o javascript" not in text


def test_html_block_elements_become_separate_lines_and_entities_are_decoded() -> None:
    text = html_to_text("<ul><li>Pix</li><li>Cart&atilde;o &amp; dinheiro</li></ul>")
    assert text.splitlines() == ["Pix", "Cartão & dinheiro"]


def test_html_json_ld_business_data_is_preserved() -> None:
    data = {
        "@type": "MedicalClinic",
        "address": {"streetAddress": "Rua A, 1"},
        "telephone": "+55 81",
    }
    html = (
        f'<html><body><p>Oi</p><script type="application/ld+json">{json.dumps(data)}</script>'
        "</body></html>"
    )
    text = html_to_text(html)
    assert "Dados estruturados da página:" in text and "Rua A, 1" in text


def test_html_garbage_does_not_raise() -> None:
    assert isinstance(html_to_text("<<<>>><div <p>texto solto"), str)


# ------------------------------------------------------------------------------ docx


def test_docx_paragraphs_become_lines() -> None:
    result = docx_to_text(_docx("Linha um da política de cancelamento", "Linha dois"))
    assert result.splitlines() == ["Linha um da política de cancelamento", "Linha dois"]


def test_docx_without_the_main_part_is_unreadable() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "x")
    with pytest.raises(ExtractionError) as caught:
        docx_to_text(buffer.getvalue())
    assert caught.value.code == "unreadable"


def test_a_decompression_bomb_docx_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_extract, "MAX_DOCX_XML_BYTES", 1000)
    with pytest.raises(ExtractionError) as caught:
        docx_to_text(_docx("a" * 5000))
    assert caught.value.code == "too_large"


def test_docx_with_broken_xml_is_unreadable() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:p>")
    with pytest.raises(ExtractionError) as caught:
        docx_to_text(buffer.getvalue())
    assert caught.value.code == "unreadable"


def test_xml_entity_expansion_never_blows_up() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&b;&b;&b;&b;&b;&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", bomb)
    # Either refused or expanded to a tiny, bounded string - never a memory blow-up.
    try:
        result = docx_to_text(buffer.getvalue())
    except ExtractionError:
        return
    assert len(result) < 10_000


# -------------------------------------------------------------------------------- pdf


def test_pdf_text_layer_is_extracted() -> None:
    pytest.importorskip("pypdf")
    assert "Rua das Flores" in pdf_to_text(_pdf_with_text(TEXT))


def test_a_scanned_pdf_without_text_is_reported_so_the_manager_can_paste() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError) as caught:
        pdf_to_text(buffer.getvalue())
    assert caught.value.code == "no_text"


def test_an_encrypted_pdf_is_refused() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("segredo")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError) as caught:
        pdf_to_text(buffer.getvalue())
    assert caught.value.code == "encrypted"


def test_a_corrupt_pdf_is_unreadable() -> None:
    pytest.importorskip("pypdf")
    with pytest.raises(ExtractionError) as caught:
        pdf_to_text(b"%PDF-1.4 this is not really a pdf")
    assert caught.value.code in {"unreadable", "no_text"}


# -------------------------------------------------------------------------------- txt


def test_txt_utf8_and_latin1_fallback() -> None:
    assert txt_to_text(TEXT.encode()) == TEXT
    assert "São José" in txt_to_text("Documento da Clínica São José, Recife".encode("latin-1"))


def test_txt_with_nul_bytes_is_not_text() -> None:
    with pytest.raises(ExtractionError) as caught:
        txt_to_text(b"abc\x00def" * 10)
    assert caught.value.code == "unsupported_type"


def test_txt_too_short_to_be_useful() -> None:
    with pytest.raises(ExtractionError) as caught:
        txt_to_text(b"oi")
    assert caught.value.code == "no_text"


# ---------------------------------------------------------------- upload dispatching


def test_kind_is_decided_by_content_and_checked_against_the_extension() -> None:
    assert detect_kind(b"%PDF-1.7", "politica.pdf") == "pdf"
    assert detect_kind(b"%PDF-1.7", "politica.txt") == "pdf"  # content wins over a lying name
    assert detect_kind(b"PK\x03\x04", "politica.docx") == "docx"
    assert detect_kind(b"Ola mundo....", "notas.txt") == "txt"


@pytest.mark.parametrize(
    ("head", "name"),
    [
        (b"PK\x03\x04", "planilha.xlsx"),  # a zip that is not a docx
        (b"PK\x03\x04", "arquivo.zip"),
        (b"\x89PNG\r\n\x1a\n", "foto.png"),
        (b"MZ\x90\x00", "programa.exe"),
        (b"just text", "semextensao"),
        (b"just text", None),
        (b"<html>", "pagina.html"),
    ],
)
def test_unsupported_uploads_are_refused(head: bytes, name) -> None:
    with pytest.raises(ExtractionError) as caught:
        detect_kind(head, name)
    assert caught.value.code == "unsupported_type"


def test_extract_upload_text_end_to_end_and_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Estacionamento" in extract_upload_text(TEXT.encode(), "faq.txt")
    assert "Linha" in extract_upload_text(_docx("Linha de politica da clinica"), "p.docx")
    with pytest.raises(ExtractionError) as empty:
        extract_upload_text(b"", "faq.txt")
    assert empty.value.code == "empty"
    monkeypatch.setattr(text_extract, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ExtractionError) as big:
        extract_upload_text(TEXT.encode(), "faq.txt")
    assert big.value.code == "too_large"


# ------------------------------------------------------------------------------ clean


def test_clean_text_collapses_whitespace_and_caps_at_a_line_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert clean_text("a   b\t\tc\r\n\r\n\r\n\r\nd\x00") == "a b c\n\nd"
    monkeypatch.setattr(text_extract, "MAX_TEXT_CHARS", 30)
    capped = clean_text("linha um aqui\nlinha dois aqui\nlinha tres aqui\nlinha quatro")
    assert len(capped) <= 30 and capped.endswith("aqui") and "\n" in capped


# ------------------------------------------------- security review: bounded memory and CPU


def _docx_lying_about_its_size(inflated: int) -> bytes:
    """A DOCX whose central directory says document.xml is 1000 bytes while it inflates to more."""
    import struct

    xml = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    xml += b"A" * inflated + b"</w:document>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("word/document.xml", xml)
    data = bytearray(buffer.getvalue())
    at = data.index(b"PK\x01\x02")
    data[at + 24 : at + 28] = struct.pack("<I", 1000)  # uncompressed size, attacker-controlled
    return bytes(data)


def test_a_docx_that_lies_about_its_size_is_not_inflated_into_memory() -> None:
    import tracemalloc

    bomb = _docx_lying_about_its_size(80 * 1024 * 1024)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError):
            docx_to_text(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 30 * 1024 * 1024


def _pdf_with_content(stream: str) -> bytes:
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n"
    return out + trailer.encode() + b"%%EOF\n"


def test_a_pdf_with_a_huge_content_stream_is_refused_before_it_is_interpreted(monkeypatch) -> None:
    # The text extraction of pypdf is pure Python and superlinear: 3 MB of operators took 38 s.
    pypdf = pytest.importorskip("pypdf")
    calls: list[int] = []
    original = pypdf.PageObject.extract_text

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pypdf.PageObject, "extract_text", spy)
    heavy = "BT /F1 12 Tf 20 100 Td " + "(ab) Tj " * 300_000 + " ET"  # ~2.4 MB of content
    with pytest.raises(ExtractionError) as caught:
        pdf_to_text(_pdf_with_content(heavy))
    assert caught.value.code == "too_large" and calls == []
