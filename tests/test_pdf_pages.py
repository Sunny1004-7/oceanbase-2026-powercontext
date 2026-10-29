"""Whole-document parsing against independently authored page text."""

from __future__ import annotations

import hashlib
import io
import re
from collections import Counter

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from powercontext.sources import SourceDefinitionRegistry, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence
from tests.support.pdf_fixtures import make_pdf


def test_all_physical_pages_keep_unicode_numbers_and_duplicate_text() -> None:
    expected = ["Revenue 100\nExpenses 34\nNet profit 66", "", "Café £42.50. Δ 中文财报 2022.", "Revenue 100"]
    raw = make_pdf(expected)
    pages = parse_pdf_document(PDFDocumentInput(document_id="独立/财报:2022", content=raw))
    assert isinstance(pages, tuple)
    assert [page.page_number for page in pages] == [1, 2, 3, 4]
    assert pages[1].text == ""
    for page, original in zip(pages, expected, strict=True):
        assert Counter(re.findall(r"\w+|[^\w\s]", page.text)) == Counter(re.findall(r"\w+|[^\w\s]", original))
        assert page.document_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("password", ["", "secret"])
def test_permissions_and_nonempty_password(password: str) -> None:
    raw = make_pdf(["Revenue 123", "", "Debt 42"], user_password=password)
    if password:
        with pytest.raises(ValueError, match="completely parsed"):
            parse_pdf_document(PDFDocumentInput(document_id="permissions", content=raw))
    else:
        assert [page.text for page in parse_pdf_document(PDFDocumentInput(document_id="permissions", content=raw))] == [
            "Revenue 123",
            "",
            "Debt 42",
        ]


@pytest.mark.parametrize("content", [b"", b"not a PDF", b"%PDF-1.7\nbroken"])
def test_invalid_pdf_rejected(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=content))


def test_late_broken_page_rejects_the_whole_document() -> None:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(make_pdf(["Valid first page", "Second page"]))))
    broken = DecodedStreamObject()
    broken.set_data(b"BT (unterminated")
    writer.pages[1][NameObject("/Contents")] = writer._add_object(broken)
    buffer = io.BytesIO()
    writer.write(buffer)
    reader = PdfReader(io.BytesIO(buffer.getvalue()), strict=True)
    assert reader.pages[0].extract_text() == "Valid first page"
    with pytest.raises(ValueError, match="completely parsed"):
        parse_pdf_document(PDFDocumentInput(document_id="broken-late", content=buffer.getvalue()))


@pytest.mark.asyncio
async def test_source_identity_revision_and_exact_projection() -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    original = make_pdf(["same", "", "same"])
    revised = make_pdf(["same", "", "same", "new"])
    long_id = " 很长:/\n" + "文档" * 2000
    sources = []
    for raw in (original, revised):
        for page in parse_pdf_document(PDFDocumentInput(document_id=long_id, content=raw)):
            source = await registry.resolve(page)
            assert source.definition_version == "1" and source.materialization == "captured"
            assert await registry.read(source) == page
            assert await registry.resolve(page) == source
            assert len(source.name) <= 256
            observation = project_source_for_transport(registry, source)
            evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            assert evidence.content == long_id + "\n" + page.text
            assert evidence.metadata == {
                "document_id": long_id,
                "document_sha256": hashlib.sha256(raw).hexdigest(),
                "page_number": page.page_number,
            }
            sources.append(source.name)
    assert len(sources) == len(set(sources)) == 7
    different_id = PDFPageInput(
        document_id=long_id + "2", document_sha256=hashlib.sha256(original).hexdigest(), page_number=1, text="same"
    )
    assert (await registry.resolve(different_id)).name not in sources


@pytest.mark.parametrize("document_id", ["", " \n\t"])
def test_blank_document_id_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=document_id, content=make_pdf(["text"]))
