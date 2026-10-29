"""Native seekdb hybrid filtering and lifecycle-to-citation acceptance."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

_PROFILE = EmbeddingProfile(
    profile_id="native-lifecycle", model="deterministic", dimension=3, distance="l2", normalization="unit"
)


class _Embedding:
    profile = _PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple((1.0, 0.0, 0.0) for _text in texts))


@pytest.mark.asyncio
async def test_tag_filters_apply_before_each_channel_limit(tmp_path: Path) -> None:
    database = Path(tempfile.mkdtemp(prefix="pc-tags-")) / "db"
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=database)), embedding_model=_Embedding()
    ) as contexts:
        service = (await contexts.get("tagged")).artifacts.memory
        head = await service.remember(
            memory=None,
            entries=(
                *(MemoryEntryInput(kind="fact", text=f"Quartz filtration decoy {index:03d}.") for index in range(40)),
                MemoryEntryInput(kind="fact", text="Quartz filtration selected exact evidence."),
            ),
            mode="append",
        )
        assert head is not None
        selected = next(entry for entry in await service.entries(head) if "selected" in entry.text)
        target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=selected.entry_id)
        tags = await contexts.records.get_tags("tagged", target)
        await contexts.records.replace_tags("tagged", target, ("selected", "verified"), expected_etag=tags.etag)
        for match in ("all", "any"):
            result = await service.search(
                "Quartz filtration",
                memories=(head,),
                mode="hybrid",
                limit=1,
                tag_filter=TagFilter(tags=("selected", "verified"), match=match),
            )
            assert len(result.hits) == 1
            assert result.hits[0].entry_id == selected.entry_id
            assert result.hits[0].matched_by == ("fts", "vector")
        absent = await service.search(
            "Quartz filtration", memories=(head,), mode="hybrid", limit=1, tag_filter=TagFilter(tags=("missing",))
        )
        assert absent.hits == ()
        (tmp_path / "tag-filter.json").write_text(
            json.dumps({
                "database": str(database),
                "decoys": 40,
                "selected_entry_id": selected.entry_id,
                "all_any_and_missing_verified": True,
            }),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_seekdb_lifecycle_search_context_and_exact_historical_citations(tmp_path: Path) -> None:
    database = Path(tempfile.mkdtemp(prefix="pc-life-")) / "db"
    text = "Quartz fibrosis controlled evidence reports measurement 17."
    observations = []

    class Pipeline:
        async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
            return tuple(
                MemoryEntryInput(kind="fact", text=source.content, sources=(source,)) for source in request.sources
            )

    async with open_builtin_runtime(
        BuiltinConfig(database=SeekDBConfig(path=database)), embedding_model=_Embedding(), candidate_pipeline=Pipeline()
    ) as runtime:
        assert runtime.scopes is not None and runtime.lifecycle is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Native lifecycle", summary="hybrid", idempotency_key="native")
        )
        sid = scope.scope_id
        receipt = await runtime.sources.for_scope(sid).capture(
            CaptureSource(source_id="native-evidence", content=text, metadata={})
        )
        memory = runtime.memory.for_scope(sid)
        while (await memory.flush()).processed:
            pass
        rows = (await memory.list()).entries
        assert len(rows) == 1
        original = rows[0].citation
        lifecycle = runtime.lifecycle.for_scope(sid)
        await lifecycle.register_source("root", receipt.source_ref, revision=1)
        await lifecycle.register_memory("memory", original, parents=("root",))
        topic = await runtime.records.for_scope(sid).create_artifact(
            "topic-memory",
            ArtifactWrite(
                content={
                    "title": "Quartz fibrosis",
                    "summary": "Quartz fibrosis supported measurement",
                    "detail": text,
                }
            ),
        )
        await lifecycle.register_topic(
            "topic",
            ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision),
            parents=("memory",),
        )
        for phase, available, sequence in [("initial", True, 0), ("withdrawn", False, 1), ("restored", True, 2)]:
            if sequence:
                await lifecycle.apply(
                    SourceLifecycleEvent(
                        node_id="root", revision=1, sequence=sequence, available=available, reason="native acceptance"
                    )
                )
            result = await memory.search(SearchMemoryRequest(query="Quartz fibrosis", mode="hybrid", limit=10))
            topics = await runtime.topic_memory.for_scope(sid).search(
                SearchTopicMemoryRequest(query="Quartz fibrosis", limit=10)
            )
            prepared = await runtime.context.for_scope(sid).prepare(
                PrepareContextRequest(query="Quartz fibrosis", max_bytes=4096)
            )
            evidence = await runtime.context.for_scope(sid).prepare(
                PrepareContextRequest(query="Quartz fibrosis", max_bytes=4096, evidence_selection=True)
            )
            assert (
                bool(result.hits) == bool(topics.hits) == bool(prepared.content) == bool(evidence.content) == available
            )
            if available:
                (hit,) = result.hits
                assert hit.text == text and hit.matched_by == ("fts", "vector")
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                )
                exact = await memory.get(GetMemoryEntryRequest(citation=citation))
                assert exact.state == "active" and exact.entry.text == text
                assert exact.entry.sources == (receipt.source_ref,)
                assert hit.entry_version_id == original.entry_version_id
                assert hit.memory_ref == (await memory.list()).memory_ref
            historical = await memory.get(GetMemoryEntryRequest(citation=original))
            assert historical.entry.text == text and historical.state == "active"
            current_head = (await memory.list(include_inactive=True)).memory_ref
            assert current_head is not None
            observations.append({
                "phase": phase,
                "available": available,
                "memory_hits": [hit.model_dump(mode="json") for hit in result.hits],
                "topic_hits": len(topics.hits),
                "context": prepared.model_dump(mode="json"),
                "evidence_context": evidence.model_dump(mode="json"),
                "original": original.model_dump(mode="json"),
                "current_head": current_head.model_dump(mode="json"),
            })
    (tmp_path / "native-lifecycle.json").write_text(
        json.dumps({"database": str(database), "phases": observations}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
