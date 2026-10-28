"""Literal evidence contracts through real Memory candidates and citations."""

from __future__ import annotations

import importlib
import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    ContextAssembly,
    ContextAssemblySection,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.evidence_selection import render_fragments, sentence_spans
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.scope import ScopeDraft

runner = importlib.import_module("challenges.context-01-evidence-selection.acceptance.runner")


async def store(runtime, key, text):
    assert runtime.scopes is not None
    scope = await runtime.scopes.create(ScopeDraft(title=key, summary="Literal evidence contract", idempotency_key=key))
    await runtime.sources.for_scope(scope.scope_id).capture(
        CaptureSource(source_id="document", content=text, metadata={})
    )
    memory = runtime.memory.for_scope(scope.scope_id)
    await memory.flush()
    page = await memory.search(SearchMemoryRequest(query="sensor", mode="fts", limit=16))
    return scope.scope_id, PreparedMemoryCandidates(scope.scope_id, page.memory_ref, page.hits)


async def check(runtime, build, budget):
    assert build.context.content_bytes == len((build.context.content or "").encode()) <= budget
    items = json.loads(build.context.content.splitlines()[-2])["items"] if build.context.content else []
    assert len(items) == len(build.fragments) == len(build.origins)
    for item, fragment, origin in zip(items, build.fragments, build.origins, strict=True):
        assert origin == fragment.address
        citation = MemoryCitation(
            memory_ref=fragment.address.memory.artifact,
            entry_id=fragment.address.entry_id,
            entry_version_id=fragment.address.entry_version_id,
        )
        exact = await runtime.memory.for_scope(fragment.address.memory.scope_id).get(
            GetMemoryEntryRequest(citation=citation)
        )
        assert exact.entry.text[fragment.start : fragment.end] == fragment.text == item["content"]
        assert type(fragment.start) is type(fragment.end) is int
        assert item["start"] == fragment.start and item["end"] == fragment.end
        assert item["citation"]["scope_id"] == fragment.address.memory.scope_id


async def test_identical_text_in_two_scopes_keeps_two_real_citations():
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=runner.LiteralPipeline()) as runtime:
        text = "The sensor detects peptide J. The assay predicts renal decline."
        a, group_a = await store(runtime, "a", text)
        b, group_b = await store(runtime, "b", text)
        request = PrepareContextRequest(query="sensor peptide J renal decline", max_bytes=1800, evidence_selection=True)
        build = PreparedContextBuilder().build_scopes_result(
            request=request, current_scope_id=a, memory_candidates=(group_a, group_b, group_a)
        )
        await check(runtime, build, 1800)
        assert {fragment.address.memory.scope_id for fragment in build.fragments} == {a, b}
        assert len({(f.address.memory.scope_id, f.address.entry_id, f.start, f.end) for f in build.fragments}) == len(
            build.fragments
        )


async def test_nonadjacent_fragments_and_query_changes_use_full_document():
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=runner.LiteralPipeline()) as runtime:
        first = "The cobalt sensor opens at 37 degrees."
        last = "The sensor retains 92 percent capacity after 300 cycles."
        text = first + " General administrative notes. " * 80 + last
        sid, group = await store(runtime, "long", text)
        builder = PreparedContextBuilder()
        request = PrepareContextRequest(
            query="cobalt sensor opens capacity after cycles", max_bytes=1400, evidence_selection=True
        )
        build = builder.build_scopes_result(request=request, current_scope_id=sid, memory_candidates=(group,))
        await check(runtime, build, 1400)
        assert len(build.fragments) >= 2
        assert first in "".join(fragment.text for fragment in build.fragments)
        assert last in "".join(fragment.text for fragment in build.fragments)
        late = builder.build_scopes_result(
            request=request.model_copy(update={"query": "retains capacity 300 cycles"}),
            current_scope_id=sid,
            memory_candidates=(group,),
        )
        assert last in "".join(fragment.text for fragment in late.fragments)
        empty = builder.build_scopes_result(
            request=request.model_copy(update={"query": "unrelated xenolith zephyr"}),
            current_scope_id=sid,
            memory_candidates=(group,),
        )
        assert empty.context.status == "empty" and empty.fragments == ()


async def test_unicode_escaping_exact_budget_probe_and_code_assembly_switch():
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=runner.LiteralPipeline()) as runtime:
        text = 'Dr. Rao tests the μ-sensor at 3.5 °C. The sensor reports "ready".\nPath C:\\sensor\\log is preserved.'
        sid, group = await store(runtime, "unicode", text)
        builder = PreparedContextBuilder()
        request = PrepareContextRequest(
            query="sensor Rao ready log",
            max_bytes=1800,
            evidence_selection=True,
            include_code=True,
            assembly=ContextAssembly(sections=(ContextAssemblySection(family="topic-memory", limit=1),)),
        )
        build = builder.build_scopes_result(request=request, current_scope_id=sid, memory_candidates=(group,))
        await check(runtime, build, 1800)
        probe = builder.probe_budget(request=request, current_scope_id=sid, memory_candidates=(group,))
        assert probe.delivered_items == len(build.fragments)
        assert probe.unused_bytes == 1800 - build.context.content_bytes
        exact_request = request.model_copy(update={"max_bytes": build.context.content_bytes})
        exact = builder.build_scopes_result(request=exact_request, current_scope_id=sid, memory_candidates=(group,))
        await check(runtime, exact, exact_request.max_bytes)
        small = builder.build_scopes_result(
            request=request.model_copy(update={"max_bytes": 512}), current_scope_id=sid, memory_candidates=(group,)
        )
        await check(runtime, small, 512)
        assert small.context.status == "empty"
        actual = await runtime.context.for_scope(sid).prepare(request)
        assert actual.content == build.context.content
        assert render_fragments(build.fragments) == build.context.content


def test_strict_flag_and_sentence_offsets():
    for value in [1, "true", None]:
        with pytest.raises(ValidationError):
            PrepareContextRequest.model_validate({"query": "sensor", "evidence_selection": value})
    text = "Dr. Rao uses 2.5 mg, i.e. a low dose. Survival is 18.5 days.\nNext line."
    spans = sentence_spans(text)
    assert [text[a:b] for a, b in spans] == [
        "Dr. Rao uses 2.5 mg, i.e. a low dose.",
        "Survival is 18.5 days.",
        "Next line.",
    ]
