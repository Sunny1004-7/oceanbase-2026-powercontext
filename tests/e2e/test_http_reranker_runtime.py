# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Real SQLite and native Memory/Runtime calls with a local HTTP MockTransport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    ReviseMemoryEntryRequest,
    SearchMemoryRequest,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft


@pytest.mark.asyncio
async def test_http_selection_retains_exact_entries_across_native_reads_and_restarts(tmp_path: Path) -> None:
    database = tmp_path / "http-native.db"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    scores = {1: 0, 2: 10, 3: 5, 4: -1, 5: 10, 6: 0, 7: 12}
    expected_ranks = (7, 2, 5, 3)
    exchanges = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"] == "project"
        assert 1 <= len(body["candidates"]) <= 3
        response = {
            "scores": [{"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(body["candidates"])]
        }
        exchanges.append({"url": str(request.url), "method": request.method, "request": body, "response": response})
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = HTTPMemoryReranker(
            "https://reranker.invalid/native", client=client, batch_size=3, policy_id="native-http-test-v1"
        )
        async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="HTTP native integration", summary="synthetic seven-entry pool", idempotency_key="http-native"
                )
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            written = await memory.remember(
                RememberMemoryRequest(
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=f"  Project harbour {word}. 舟山\n🌊  ")
                        for word in ("amber", "birch", "cedar", "delta", "elm", "fir", "granite")
                    )
                )
            )
            originals = await memory.list()
            assert len(originals.entries) == 7
            original_by_version = {entry.entry.entry_version_id: entry for entry in originals.entries}
            assert len(original_by_version) == 7

        async with open_builtin_contexts(config) as contexts:
            service = (await contexts.get(scope.scope_id)).artifacts.memory
            head = await service.revision(written.memory_ref)
            assert (await service.get(head)).as_ref() == written.memory_ref
            coarse = await service.search("project", memories=(head,), limit=7, mode="fts")
            assert coarse.rerank is None and coarse.generation_calls == 0
            assert len(coarse.hits) == 7
            assert {hit.entry_version_id for hit in coarse.hits} == set(original_by_version)

        async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            result = await memory.search(SearchMemoryRequest(query="project", mode="fts", limit=4))
            trace = result.rerank
            assert trace is not None
            assert trace.candidate_hits == coarse.hits
            assert trace.selected_ranks == expected_ranks
            assert trace.policy_id == "native-http-test-v1" and trace.usage.requests == 3
            assert trace.discarded_rank_count == 0 and trace.used_fallback is False
            expected_hits = tuple(coarse.hits[rank - 1] for rank in expected_ranks)
            assert result.hits == expected_hits and result.hits != coarse.hits[:4]
            assert result.memory_ref == written.memory_ref
            citations = []
            runtime_gets = []
            for hit in result.hits:
                original = original_by_version[hit.entry_version_id]
                assert hit.memory_ref == original.memory_ref
                assert (hit.entry_id, hit.entry_version_id, hit.text) == (
                    original.entry.entry_id,
                    original.entry.entry_version_id,
                    original.entry.text,
                )
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                )
                exact = await memory.get(GetMemoryEntryRequest(citation=citation))
                assert exact == original
                citations.append(citation)
                runtime_gets.append(exact.model_dump(mode="json"))
        assert len(exchanges) == 3

        async with open_builtin_contexts(config, memory_reranker=reranker) as contexts:
            service = (await contexts.get(scope.scope_id)).artifacts.memory
            head = await service.revision(written.memory_ref)
            direct = await service.search("project", memories=(head,), limit=4, mode="fts")
            assert direct.hits == result.hits and direct.rerank is not None
            assert direct.rerank.selected_ranks == expected_ranks
            assert direct.generation_calls == 1 and direct.rerank.usage.requests == 3
            expanded = await service.expand(direct.hits)
            assert expanded == tuple(original_by_version[hit.entry_version_id].entry for hit in direct.hits)
            assert (await service.get(head)).as_ref() == written.memory_ref
        assert len(exchanges) == 6 and not client.is_closed

    for start in (0, 3):
        sent = [item for exchange in exchanges[start : start + 3] for item in exchange["request"]["candidates"]]
        assert sent == [{"rank": rank, "text": hit.text} for rank, hit in enumerate(coarse.hits, start=1)]

    # Advance the head, then prove the original citations still resolve the exact old versions.
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        revised = await memory.revise(
            ReviseMemoryEntryRequest(
                citation=citations[0], kind="fact", text="Project revised body; old citation must remain exact."
            )
        )
        assert revised.memory_ref != written.memory_ref
        assert revised.entry is not None
        assert revised.entry.entry.entry_id == citations[0].entry_id
        assert revised.entry.entry.entry_version_id != citations[0].entry_version_id

    restarted_gets = []
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for citation in citations:
            exact = await memory.get(GetMemoryEntryRequest(citation=citation))
            assert exact == original_by_version[citation.entry_version_id]
            restarted_gets.append(exact.model_dump(mode="json"))
    assert restarted_gets == runtime_gets
    assert database.is_file() and database.stat().st_size > 0
    (tmp_path / "http-runtime-audit.json").write_text(
        json.dumps(
            {
                "backend": "real SQLite BuiltinRuntime and Memory service",
                "http": "httpx.MockTransport; no online service or official quality corpus",
                "database": str(database),
                "scope_id": scope.scope_id,
                "written": written.model_dump(mode="json"),
                "original_entries": originals.model_dump(mode="json"),
                "coarse": coarse.model_dump(mode="json"),
                "score_by_global_rank": scores,
                "runtime_result": result.model_dump(mode="json"),
                "native_generation_calls": direct.generation_calls,
                "native_http_requests": direct.rerank.usage.requests,
                "http_exchanges": exchanges,
                "expanded": [entry.model_dump(mode="json") for entry in expanded],
                "runtime_gets": runtime_gets,
                "new_head": revised.model_dump(mode="json"),
                "exact_old_version_gets_after_restart": restarted_gets,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
