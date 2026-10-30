"""Complete offline PDF capture with immutable physical-page identities."""

from __future__ import annotations

import hashlib
import io

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

from powercontext.sources._pdf_retrieval import search_pdf_pages as search_pdf_pages
from powercontext.sources._pdf_text import extract_page_text
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _nonblank_document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document ID must not be blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    document_id: str = Field(strict=True)
    content: bytes = Field(strict=True)

    _document_id = field_validator("document_id")(_nonblank_document_id)


class PDFPageInput(BaseModel):
    document_id: str = Field(strict=True)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    page_number: int = Field(strict=True, gt=0)
    text: str = Field(strict=True)

    _document_id = field_validator("document_id")(_nonblank_document_id)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page before publishing any result."""
    try:
        if not value.content or not value.content.startswith(b"%PDF-"):
            raise ValueError("invalid PDF header")  # noqa: TRY003, TRY301
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("a nonempty PDF password is required")  # noqa: TRY003, TRY301
        digest = hashlib.sha256(value.content).hexdigest()
        return tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=number,
                text=extract_page_text(page),
            )
            for number, page in enumerate(reader.pages, 1)
        )
    except Exception as error:
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _source_id(value: PDFPageInput) -> str:
    document = hashlib.sha256(value.document_id.encode("utf-8")).hexdigest()
    identity = f"pdf:{document}:{value.document_sha256}:{value.page_number}"
    if len(identity) > 256:
        raise ValueError("page identity exceeds the Source limit")  # noqa: TRY003
    return identity


class _PDFAdapter:
    input_class = PDFPageInput
    source_class = PDFPageSource
    name = "pdf-page"

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=_source_id(value), materialization=SourceMaterialization.CAPTURED, **value.model_dump()
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=source.document_id + "\n" + source.text,
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFAdapter(), projections=(_PDFTextProjection(),)
)
