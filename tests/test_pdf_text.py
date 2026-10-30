"""Synthetic, isolated PDF controls with independently specified visible text."""

from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document


def _pdf(
    content: bytes, *, form: bytes | None = None, alternate_glyphs: bool = False, to_unicode: bytes | None = None
) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
    })
    if alternate_glyphs:
        font[NameObject("/Encoding")] = DictionaryObject({
            NameObject("/Type"): NameObject("/Encoding"),
            NameObject("/Differences"): ArrayObject([FloatObject(65), NameObject("/a.ss01"), NameObject("/b.alt")]),
        })
    if to_unicode is not None:
        cmap = DecodedStreamObject()
        cmap.set_data(to_unicode)
        font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Resources")] = resources
    if form is not None:
        xobject = DecodedStreamObject()
        xobject.set_data(form)
        xobject.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (0, 0, 100, 100)]),
            NameObject("/Matrix"): ArrayObject([FloatObject(v) for v in (0.5, 0, 0, 0.5, 0, 0)]),
            NameObject("/Resources"): resources,
        })
        resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Form1"): writer._add_object(xobject)})
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _text(raw: bytes) -> str:
    pages = parse_pdf_document(PDFDocumentInput(document_id="isolated-synthetic-control", content=raw))
    assert len(pages) == 1
    return pages[0].text


def _terms(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z]+|\d+", text.lower()))


def test_transformed_form_obeys_its_bbox_and_physical_page() -> None:
    raw = _pdf(
        b"q 1 0 0 1 100 100 cm /Form1 Do Q "
        b"BT /F1 10 Tf 10 280 Td (Header 2024) Tj ET "
        b"q 1 0 0 1 -400 0 cm BT /F1 10 Tf 10 200 Td (Other physical page 2023) Tj ET Q",
        form=b"BT /F1 12 Tf 10 50 Td (Inside 120) Tj ET BT /F1 12 Tf 120 50 Td (Outside form 999) Tj ET",
    )
    assert _terms(_text(raw)) == _terms("Header 2024 Inside 120")


def test_clipping_path_is_transformed_and_restored() -> None:
    raw = _pdf(
        b"q 0 1 -1 0 180 40 cm 0 0 100 100 re W n "
        b"BT /F1 8 Tf 10 50 Td (Visible rotated 45) Tj ET "
        b"BT /F1 10 Tf 10 140 Td (Clipped inside page 999) Tj ET Q "
        b"BT /F1 10 Tf 10 20 Td (Footer 7) Tj ET"
    )
    assert _terms(_text(raw)) == _terms("Visible rotated 45 Footer 7")


def test_individually_positioned_glyphs_preserve_real_word_gaps() -> None:
    expected = "In February 2021 I am A CEO; U.S. cash (120) -5.0%"
    # Courier advances exactly six points at size ten, including a real word space.
    operations = [
        f"BT /F1 10 Tf 1 0 0 1 {10 + 6 * i} 200 Tm [({char}) -40000] TJ ET".encode()
        for i, char in enumerate(expected)
        if char != " " and char not in "()"
    ]
    # Parentheses are punctuation, so omitting their drawings does not alter the term oracle.
    text = _text(_pdf(b" ".join(operations)))
    assert _terms(text) == _terms(expected)
    assert "In February 2021" in " ".join(text.split())


def test_explicit_spaces_table_columns_and_real_edge_content_survive() -> None:
    raw = _pdf(
        b"BT /F1 8 Tf 0 292 Td (A I US GAAP 2024) Tj ET "
        b"BT /F1 10 Tf 0 140 Td (Net loss) Tj 150 0 Td (120) Tj 70 0 Td (5.0%) Tj ET "
        b"BT /F1 8 Tf 0 1 Td (Footnote 9) Tj ET"
    )
    assert _terms(_text(raw)) == _terms("A I US GAAP 2024 Net loss 120 5.0% Footnote 9")


def test_declared_stylistic_glyph_names_decode_to_the_base_letters() -> None:
    raw = _pdf(b"BT /F1 10 Tf 10 100 Td (AB) Tj ET", alternate_glyphs=True)
    assert _terms(_text(raw)) == _terms("ab")


def test_tounicode_text_takes_precedence_over_glyph_name_fallback() -> None:
    # A ToUnicode value is literal text, even when it resembles a glyph name.
    cmap = (
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"2 beginbfchar\n<41> <002F0074002E0073007300300034>\n<42> <005A>\nendbfchar"
    )
    raw = _pdf(b"BT /F1 10 Tf 10 100 Td (AB) Tj ET", alternate_glyphs=True, to_unicode=cmap)
    assert _text(raw) == "/t.ss04Z"
