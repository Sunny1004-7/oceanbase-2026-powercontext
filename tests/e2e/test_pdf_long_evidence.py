"""Regression for complete captured pages exceeding the ordinary Memory budget."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import InvalidMemoryCandidateError, MemoryEntryInput
from powercontext.builtin.artifacts.memory.canonical import entry_content_hash
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceObservation,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFDocumentInput, parse_pdf_document
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence
from tests.support.pdf_fixtures import make_pdf


def test_authored_page_token_multisets_use_the_published_metric() -> None:
    texts = [
        "Revenue 1,234.50; loss (12); margin -7.5%; Café and office ﬁnancial data.\nFooter 123456",
        "",
        "Debt 2024 (99) 12.75% co-\noperation; cash 999.00 " * 300 + "FinalFooter 998877",
    ]
    pages = parse_pdf_document(PDFDocumentInput(document_id="independent-control", content=make_pdf(texts)))
    assert len(pages) == len(texts)
    for page, known in zip(pages, texts, strict=True):
        actual = Counter(re.findall(r"[a-z]+|\d+", page.text.lower()))
        expected = Counter(re.findall(r"[a-z]+|\d+", known.lower()))
        assert actual == expected


@pytest.mark.asyncio
async def test_complete_large_pages_and_long_multiline_id_persist_restart_and_hash(tmp_path: Path) -> None:
    class EvidencePipeline:
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
    expected = {}
    observations = []
    for document, text in [
        ("below", "x" * 8185),
        ("at", "x" * 8189),
        ("above", "x" * 8187),
        ("dense", "Revenue (123) 17.5% financial-note " * 540 + " FOOTERPROOF"),
        ("unicode", "财务é" * 1400 + " Cafe\u0301\u2001UnicodeEnd"),
        ("document\n文档\n" * 1200, "LongIdentityEnd 123"),
    ]:
        raw = make_pdf([text, ""])
        for page in parse_pdf_document(PDFDocumentInput(document_id=document, content=raw)):
            source = await registry.resolve(page)
            observation = project_source_for_transport(registry, source)
            expected[source.name] = TextEvidence.model_validate(
                observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
            ).content.strip()
            observations.append(observation)
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="Pages", summary="large", idempotency_key="large"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for observation in (*observations, observations[0]):
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
    async with open_builtin_runtime(config, candidate_pipeline=EvidencePipeline()) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        while (await memory.flush()).processed:
            pass
        assert len((await memory.list()).entries) == len(expected)
        with pytest.raises(InvalidMemoryCandidateError, match="8192 UTF-8 bytes"):
            await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="note", text="界" * 2731),)))
        with pytest.raises(InvalidMemoryCandidateError, match="8192 UTF-8 bytes"):
            await memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="source-evidence", text="x" * 8193),))
            )
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        records = (await memory.list()).entries
        assert len(records) == len(expected)
        for record in records:
            exact = await memory.get(GetMemoryEntryRequest(citation=record.citation))
            (ref,) = exact.entry.sources
            assert exact.state == "active" and exact.entry.text == expected[ref.source_id]
            assert exact.entry.entry_content_hash == entry_content_hash(
                kind=exact.entry.kind,
                text=exact.entry.text,
                source_refs=(ref.model_dump(mode="json"),),
                artifact_refs=(),
            )
        for term in ("FOOTERPROOF", "UnicodeEnd", "LongIdentityEnd"):
            assert (await memory.search(SearchMemoryRequest(query=term, mode="fts", limit=20))).hits
    async with open_builtin_contexts(config) as contexts:
        context = await contexts.get(scope.scope_id)
        journal = await context.sources.journal.entries()
        assert len(journal) == len(expected)
        for item in journal:
            assert isinstance(item.source, SourceObservation)
            assert (
                TextEvidence.model_validate(item.source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content.strip()
                == expected[item.source_ref.source_id]
            )
        await context.artifacts.memory.rebuild_projections()
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        assert len((await memory.list()).entries) == len(expected)
        assert (await memory.search(SearchMemoryRequest(query="FOOTERPROOF", mode="fts", limit=20))).hits
