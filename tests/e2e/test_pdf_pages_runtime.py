"""Exact captured PDF references survive restarts and current-head mutations."""

from __future__ import annotations

from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    RetireMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence
from tests.support.pdf_fixtures import make_pdf


@pytest.mark.asyncio
async def test_pdf_revisions_restart_current_state_and_non_pdf_filter(tmp_path: Path) -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
    expected = {}

    class PagePipeline:
        async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
            return tuple(
                MemoryEntryInput(
                    kind="pdf-page", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="PDF", summary="revisions", idempotency_key="pdf"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for texts in (["Cobalt revenue 100", "", "Cobalt debt 17"], ["Cobalt revenue 100", "", "Cobalt debt 19"]):
            pages = parse_pdf_document(PDFDocumentInput(document_id="Acme_2022", content=make_pdf(texts)))
            for page in pages:
                source = await registry.resolve(page)
                observation = project_source_for_transport(registry, source)
                expected[source.name] = TextEvidence.model_validate(
                    observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
                ).content.strip()
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
    async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass
        await memory.remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="note", text="Cobalt revenue 999"),))
        )
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        memory = runtime.memory.for_scope(scope.scope_id)
        citations = await search_pdf_pages(memory, "Cobalt")
        assert len(citations) == 4
        assert await search_pdf_pages(memory, "Cobalt", limit=20) == citations
        assert await search_pdf_pages(memory, "Cobalt", limit=1) == citations[:1]
        assert await search_pdf_pages(memory, "zzzxynonexistent") == ()
        seen = set()
        for citation in citations:
            record = await memory.get(GetMemoryEntryRequest(citation=citation))
            (reference,) = record.entry.sources
            assert record.state == "active" and record.entry.text == expected[reference.source_id]
            seen.add(reference.source_id)
        assert len(seen) == 4
        await memory.retire(RetireMemoryEntryRequest(citation=citations[0]))
        current = await search_pdf_pages(memory, "Cobalt")
        assert len(current) == 3
        assert citations[0].entry_id not in {citation.entry_id for citation in current}
        historical = await memory.get(GetMemoryEntryRequest(citation=citations[0]))
        assert historical.state == "active"
        assert historical.entry.text == expected[historical.entry.sources[0].source_id]
        empty_scope = await runtime.scopes.create(
            ScopeDraft(title="Empty", summary="empty", idempotency_key="pdf-empty")
        )
        assert await search_pdf_pages(runtime.memory.for_scope(empty_scope.scope_id), "revenue") == ()
        for limit in [0, -1, 101, True]:
            with pytest.raises(ValueError):
                await search_pdf_pages(memory, "revenue", limit=limit)


@pytest.mark.asyncio
async def test_explicit_statements_preserve_both_pages_without_column_label_false_positive(tmp_path: Path) -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'headings.db'}"))
    texts = [
        "Hedging instruments\nFair value losses\nCash flow hedges\nLosses recognized in\nIncome Statement\n"
        "Operating income depreciation capital expenditures cash flow income statement repeated explanation.",
        "Consolidated Statement of Income\nOperating profit 17. Net revenue 55. Restructuring is not a separate row.",
        "Consolidated Statement of Cash Flows\nDepreciation 3. Capital expenditures 2. Operating cash 8.",
        "Footnote detail. Operating income depreciation capital expenditures cash flow income statement.",
    ]

    class Pipeline:
        async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
            return tuple(
                MemoryEntryInput(
                    kind="pdf-page", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async with open_builtin_runtime(config, candidate_pipeline=Pipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Statements", summary="primary headings", idempotency_key="headings")
        )
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for page in parse_pdf_document(PDFDocumentInput(document_id="CEDAR_2024_ANNUAL", content=make_pdf(texts))):
            source = await registry.resolve(page)
            await runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=scope.scope_id, observation=project_source_for_transport(registry, source)
                )
            )
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass
        found = await search_pdf_pages(
            memory, "Review the income statement and statement of cash flows for Cedar 2024.", limit=2
        )
        pages = set()
        for citation in found:
            exact = await memory.get(GetMemoryEntryRequest(citation=citation))
            (ref,) = exact.entry.sources
            pages.add(int(ref.source_id.rsplit(":", 1)[1]))
        assert pages == {2, 3}
