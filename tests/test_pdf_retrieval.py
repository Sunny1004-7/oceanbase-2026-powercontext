"""Current-page routing and bounded consistency through a real Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.application import ScopedMemoryApplication
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY
from tests.support.pdf_fixtures import make_pdf


@asynccontextmanager
async def _pages(tmp_path: Path) -> AsyncIterator[ScopedMemoryApplication]:
    class Pipeline:
        async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
            return tuple(
                MemoryEntryInput(
                    kind="source-evidence",
                    text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                    sources=(source,),
                )
                for source in request.sources
            )

    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'search.db'}"))
    async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="PDF", summary="routing", idempotency_key="pdf"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for document, texts in [
            ("Birch\nIndustries_2024", ["Legal proceedings\nA pesticide lawsuit is pending in Ontario.", ""]),
            ("Birch\nIndustries_2023", ["Legal proceedings\nA pesticide lawsuit was resolved in Alberta."]),
            ("Alder_2024", ["Legal proceedings\nA pesticide lawsuit is pending in Ontario." * 4]),
            (" \nOpaque\nIdentifier ", ["Uranium reserves 992; unique mineral disclosure."]),
        ]:
            for page in parse_pdf_document(PDFDocumentInput(document_id=document, content=make_pdf(texts))):
                source = await registry.resolve(page)
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=scope.scope_id,
                        observation=project_source_for_transport(registry, source),
                    )
                )
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass
        yield memory


@pytest.mark.asyncio
async def test_multiline_document_identity_and_single_or_comparison_year(tmp_path: Path) -> None:
    async with _pages(tmp_path) as memory:
        one = await search_pdf_pages(memory, "Birch 2024 pesticide litigation", limit=1)
        exact = await memory.get(GetMemoryEntryRequest(citation=one[0]))
        assert exact.entry.text.startswith("Birch\nIndustries_2024\n")
        for year in ("FY2024", "FY 2024"):
            fiscal = await search_pdf_pages(memory, f"Birch {year} pesticide litigation", limit=1)
            exact_fiscal = await memory.get(GetMemoryEntryRequest(citation=fiscal[0]))
            assert exact_fiscal.entry.text.startswith("Birch\nIndustries_2024\n")
        comparison = await search_pdf_pages(memory, "Compare Birch pesticide litigation in 2023 and 2024", limit=20)
        texts = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text for citation in comparison]
        assert len(texts) == 2
        assert all(text.startswith("Birch\nIndustries_") for text in texts)
        both = await search_pdf_pages(memory, "Compare Birch and Alder pesticide litigation in 2024", limit=20)
        assert len(both) == 2
        opaque = await search_pdf_pages(memory, "uranium reserves", limit=1)
        assert len(opaque) == 1
        exact_opaque = await memory.get(GetMemoryEntryRequest(citation=opaque[0]))
        assert "Uranium reserves 992" in exact_opaque.entry.text


class _ConcurrentMemory:
    """A real competing write between public calls; no mocked Runtime storage."""

    def __init__(self, memory: ScopedMemoryApplication, *, every_search: bool = False) -> None:
        self.memory = memory
        self.every_search = every_search
        self.writes = 0

    async def list(self):
        return await self.memory.list()

    async def get(self, request):
        return await self.memory.get(request)

    async def search(self, request):
        result = await self.memory.search(request)
        if not self.writes or self.every_search:
            self.writes += 1
            await self.memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="note", text=f"Competing update {self.writes}"),))
            )
        return result


@pytest.mark.asyncio
async def test_head_change_retries_to_fresh_exact_citations(tmp_path: Path) -> None:
    async with _pages(tmp_path) as memory:
        concurrent = _ConcurrentMemory(memory)
        citations = await search_pdf_pages(cast(ScopedMemoryApplication, concurrent), "Birch 2024 pesticide", limit=2)
        assert citations
        current = await memory.list()
        assert all(citation.memory_ref == current.memory_ref for citation in citations)
        for citation in citations:
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).state == "active"


@pytest.mark.asyncio
async def test_repeated_head_changes_raise_explicitly_instead_of_empty_success(tmp_path: Path) -> None:
    async with _pages(tmp_path) as memory:
        concurrent = _ConcurrentMemory(memory, every_search=True)
        with pytest.raises(RuntimeError, match="head changed repeatedly"):
            await search_pdf_pages(cast(ScopedMemoryApplication, concurrent), "Birch pesticide", limit=2)


@pytest.mark.asyncio
async def test_full_current_view_survives_bounded_fts_pool(tmp_path: Path) -> None:
    async with _pages(tmp_path) as memory:

        class EmptyChannel:
            # Simulate candidate-channel truncation only; pages and exact citations
            # come from the real public list/get path, never a pre-parsed cache.
            async def list(self):
                return await memory.list()

            async def get(self, request):
                return await memory.get(request)

            async def search(self, request: SearchMemoryRequest):
                return (await memory.search(request)).model_copy(update={"hits": ()})

        citations = await search_pdf_pages(
            cast(ScopedMemoryApplication, EmptyChannel()), "Birch 2024 pesticide", limit=1
        )
        assert len(citations) == 1
        exact = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
        assert exact.entry.text.startswith("Birch\nIndustries_2024\n")
