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

"""Native durable checkpoint failure recovery and complete large Pi event recall."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CommitConnectorCheckpoint,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import (
    ConnectorBinding,
    ConnectorRunStatus,
    SourceDefinitionRegistry,
    SourceObservation,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class _SplitEvidence:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(kind=f"pi-part-{index}", text=part, sources=(source,))
            for source in request.sources
            for index, part in enumerate(self.parts(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"]))
        )

    @staticmethod
    def parts(text):
        return tuple(text[index : index + 1200] for index in range(0, len(text), 1200))


async def _flush_and_get_citations(config, scope_id):
    async with open_builtin_runtime(config, candidate_pipeline=_SplitEvidence()) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        while (await memory.flush()).processed:
            pass
        found = await memory.search(SearchMemoryRequest(query="quartzharbor", mode="fts", limit=100))
        assert found.hits
        cited_sources, readbacks = [], []
        for hit in found.hits:
            citation = MemoryCitation(
                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
            )
            exact = await memory.get(GetMemoryEntryRequest(citation=citation))
            cited_sources.extend(exact.entry.sources)
            readbacks.append({"citation": citation.model_dump(mode="json"), "entry": exact.model_dump(mode="json")})
        return cited_sources, readbacks


async def _read_events(config, scope_id, entries, expected_events, registry):
    readbacks = []
    async with open_builtin_contexts(config) as contexts:
        sources = (await contexts.get(scope_id)).sources
        for entry in entries:
            stored = await sources.get(entry.source)
            assert isinstance(stored, SourceObservation)
            hydrated = PI_EVENT_SOURCE_DEFINITION.source_class.model_validate(stored.payload)
            event = await registry.read(hydrated)
            assert isinstance(event, PiEventInput)
            assert event == expected_events[event.event_id]
            assert TextEvidence.model_validate(stored.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content == event.content
            readbacks.append(event.model_dump(mode="json"))
    return readbacks


@pytest.mark.parametrize("fault", ["exception", "rejected"])
@pytest.mark.asyncio
async def test_branch_progress_is_durable_and_partial_sink_failure_replays_idempotently(
    tmp_path: Path, fault: str
) -> None:
    path = tmp_path / "tree.jsonl"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pi.db'}"))
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    tool_text = "quartzharbor " + "工具🌊完整输出 " * 1800
    assert len(tool_text.encode()) > 8192
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared request"}},
        {
            "type": "message",
            "id": "left",
            "parentId": "root",
            "message": {"role": "assistant", "content": "left evidence"},
        },
        {
            "type": "message",
            "id": "left2",
            "parentId": "left",
            "message": {"role": "assistant", "content": "left second"},
        },
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "right",
            "parentId": "control",
            "message": {"role": "assistant", "content": "right evidence"},
        },
        {
            "type": "message",
            "id": "tool",
            "parentId": "right",
            "message": {"role": "toolResult", "toolName": "read", "toolCallId": "call", "content": tool_text},
        },
        {"type": "compaction", "id": "summary", "parentId": "tool", "summary": "retain all earlier evidence"},
    ]

    def write():
        path.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in [
                    {"type": "session", "version": 3, "id": "durable-tree"},
                    *records,
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    write()
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(ScopeDraft(title="Pi", summary="durable test", idempotency_key="pi"))
    binding = ConnectorBinding(
        scope_id=scope.scope_id, binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )

    async def run(leaf, inject=False):
        async with open_builtin_runtime(config) as runtime:
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))

            class Sink:
                calls = 0

                async def submit(self, active_binding, item_id, definition_name, value, /):
                    self.calls += 1
                    if inject and self.calls == 2:
                        if fault == "exception":
                            raise RuntimeError("injected-sink-failure")
                        raise ConnectorSubmissionRejectedError("injected-rejection")
                    source = await registry.resolve(value)
                    return (
                        await runtime.ingestion.submit(
                            SubmitSourceObservation(
                                scope_id=active_binding.scope_id,
                                observation=project_source_for_transport(registry, source),
                            )
                        )
                    ).source_ref

            class Checkpoints:
                async def load(self, active_binding, /):
                    return (await runtime.ingestion.checkpoint(active_binding)).checkpoint

                async def save(self, active_binding, checkpoint, /, *, expected):
                    await runtime.ingestion.commit(
                        CommitConnectorCheckpoint(binding=active_binding, checkpoint=checkpoint, expected=expected)
                    )

            return await ConnectorLifecycle(sink=Sink(), checkpoints=Checkpoints()).run(
                PiSessionConnector(path, leaf), binding
            )

    async def snapshot():
        async with open_builtin_runtime(config) as runtime:
            checkpoint = (await runtime.ingestion.checkpoint(binding)).checkpoint
        async with open_builtin_contexts(config) as contexts:
            entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
        return checkpoint, entries

    first = await run("left2")
    first_checkpoint = deepcopy(first.committed_checkpoint)
    if fault == "exception":
        with pytest.raises(RuntimeError, match="injected-sink-failure"):
            await run("summary", inject=True)
    else:
        rejected = await run("summary", inject=True)
        assert rejected.status is ConnectorRunStatus.INCOMPLETE
        assert rejected.committed_checkpoint == first_checkpoint
    failed_checkpoint, partial_entries = await snapshot()
    assert failed_checkpoint == first_checkpoint
    assert len(partial_entries) == 4  # right persisted before the second submission failed.
    recovered = await run("summary")
    assert len(recovered.items) == 3  # right is safely replayed, not duplicated.
    assert (await run("left2")).items == ()
    stable_checkpoint, entries = await snapshot()
    assert len(entries) == 6
    audit = [
        {"stage": "after-failure", "checkpoint": failed_checkpoint, "persisted": len(partial_entries)},
        {"stage": "recovered", "checkpoint": stable_checkpoint, "persisted": len(entries)},
    ]
    records.append({
        "type": "message",
        "id": "tail",
        "parentId": "left2",
        "message": {"role": "user", "content": "new left tail"},
    })
    # An imported off-branch record changed only outside its visible text.
    records[4]["timestamp"] = "changed-metadata"
    write()
    with pytest.raises(ValueError):
        await run("tail")
    assert (await snapshot())[0] == stable_checkpoint
    assert len((await snapshot())[1]) == 6
    records[4].pop("timestamp")
    write()
    path.write_bytes(path.read_bytes() + b"{bad json}\n")
    with pytest.raises(ValueError):
        await run("tail")
    write()
    records.append({"type": "model_change", "id": "bad-other", "parentId": "missing"})
    write()
    with pytest.raises(ValueError):
        await run("tail")
    assert (await snapshot())[0] == stable_checkpoint
    records.pop()
    write()
    appended = await run("tail")
    assert len(appended.items) == 1 and (await run("summary")).items == ()

    cited_sources, citation_readbacks = await _flush_and_get_citations(config, scope.scope_id)
    final_checkpoint, entries = await snapshot()
    expected_events = {
        event.event_id: event
        for leaf in ("tail", "summary")
        for event in parse_pi_session(PiSessionInput(content=path.read_bytes(), leaf_id=leaf))
    }
    readbacks = await _read_events(config, scope.scope_id, entries, expected_events, registry)
    tool_event = next(event for event in readbacks if event["event_id"] == "tool")
    assert tool_event["record"]["message"]["content"] == tool_text
    tool_name = (await registry.resolve(expected_events["tool"])).name
    assert tool_name in {reference.source_id for reference in cited_sources}
    (tmp_path / "pi-native-audit.json").write_text(
        json.dumps(
            {
                "backend": "real SQLite, ConnectorLifecycle, Runtime ingestion and citation get",
                "fault": fault,
                "stages": audit,
                "final_checkpoint": final_checkpoint,
                "source_count": len(entries),
                "readbacks": readbacks,
                "large_tool_content_utf8_bytes": len(tool_text.encode()),
                "citation_readbacks": citation_readbacks,
                "citations": [reference.model_dump(mode="json") for reference in cited_sources],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
