"""Native count -> lifecycle -> evidence -> independent incremental receiver."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    ContextAssembly,
    ContextAssemblySection,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent

checks = importlib.import_module("challenges.processing-01-count-trigger.acceptance.checks")


async def test_native_count_lifecycle_context_and_receiver(tmp_path, monkeypatch):  # noqa: C901 - cross-family state and receiver acceptance
    database = tmp_path / "combined.db"
    observations = []
    streams = {False: [], True: []}
    targets = {False: [], True: []}
    bases = {False: None, True: None}
    with checks.provider(monkeypatch) as server:
        settings = checks.config(database, server, memory_source_count_threshold=2)
        async with open_builtin_runtime(settings) as runtime:
            sid = await checks.scope(runtime, "combined")
            receipts = await checks.capture(runtime, sid, checks.messages()[:2])
            expected = checks.receipt_refs(receipts)
            await checks.wait_refs(runtime, sid, "memory", expected)
            await checks.check_reads(runtime, sid, "memory", expected)
            assert runtime.lifecycle is not None
            lifecycle = runtime.lifecycle.for_scope(sid)
            for index, receipt in enumerate(receipts):
                await lifecycle.register_source(f"source{index}", receipt.source_ref, revision=7)
            rows = (await runtime.memory.for_scope(sid).list()).entries
            assert rows
            originals = {row.entry.entry_id: row.entry for row in rows}
            for index, row in enumerate(rows):
                await lifecycle.register_memory(
                    f"memory{index}", row.citation, parents=("source0", "source1"), policy="all"
                )
            topic = await runtime.records.for_scope(sid).create_artifact(
                "topic-memory",
                ArtifactWrite(
                    content={
                        "title": "CountEvidence combined topic",
                        "summary": "CountEvidence combined supported summary",
                        "detail": "CountEvidence combines both original messages.",
                    }
                ),
            )
            topic_ref = ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision)
            await lifecycle.register_topic(
                "topic", topic_ref, parents=tuple(f"memory{i}" for i in range(len(rows))), policy="all"
            )
            for phase, available, sequence in [("initial", True, 0), ("withdrawn", False, 1), ("restored", True, 2)]:
                if sequence:
                    await lifecycle.apply(
                        SourceLifecycleEvent(
                            node_id="source0",
                            revision=7,
                            sequence=sequence,
                            available=available,
                            reason="combined acceptance",
                        )
                    )
                memory_page = await runtime.memory.for_scope(sid).search(
                    SearchMemoryRequest(query="CountEvidence", mode="fts", limit=16)
                )
                topic_page = await runtime.topic_memory.for_scope(sid).search(
                    SearchTopicMemoryRequest(query="CountEvidence", limit=20), allow_embedding=False
                )
                assert bool(memory_page.hits) == bool(topic_page.hits) == available
                plain = await runtime.context.for_scope(sid).prepare(
                    PrepareContextRequest(query="CountEvidence", max_bytes=4096)
                )
                assert bool(plain.content) == available
                if available:
                    assert "CountEvidence combined supported summary" in (plain.content or "")
                for evidence in [False, True]:
                    request = PrepareContextRequest(
                        query="CountEvidence",
                        max_bytes=4096,
                        evidence_selection=evidence,
                        assembly=ContextAssembly(
                            sections=(
                                ContextAssemblySection(family="memory", limit=6),
                                ContextAssemblySection(family="topic-memory", limit=2),
                            )
                        ),
                    )
                    context = runtime.context.for_scope(sid)
                    ordinary = await context.prepare(request)
                    assert bool(ordinary.content) == available
                    if available:
                        if evidence:
                            assert ordinary.content is not None
                            items = json.loads(ordinary.content.splitlines()[-2])["items"]
                            assert items
                            for item in items:
                                citation = MemoryCitation.model_validate({
                                    key: value for key, value in item["citation"].items() if key != "scope_id"
                                })
                                exact = await runtime.memory.for_scope(sid).get(
                                    GetMemoryEntryRequest(citation=citation)
                                )
                                assert exact.entry.entry_id in originals
                                assert exact.entry.text[item["start"] : item["end"]] == item["content"]
                        else:
                            assert rows[0].entry.text in (ordinary.content or "")
                        assert ("CountEvidence combined supported summary" in (ordinary.content or "")) == (
                            not evidence
                        )
                    incremental = await context.prepare_incremental(request, base_pack=bases[evidence])
                    streams[evidence].append(base64.b64encode(incremental.to_bytes()).decode())
                    targets[evidence].append((ordinary.content or "").encode())
                    bases[evidence] = ContextPack.from_content(ordinary.content or "")
                for row in rows:
                    exact = await runtime.memory.for_scope(sid).get(GetMemoryEntryRequest(citation=row.citation))
                    assert exact.entry == originals[row.entry.entry_id]
                record = await runtime.records.for_scope(sid).get_artifact_revision(
                    "topic-memory", topic.artifact_id, topic.revision
                )
                assert record.content["summary"] == "CountEvidence combined supported summary"
                current = (await runtime.memory.for_scope(sid).list(include_inactive=True)).memory_ref
                assert current is not None
                with sqlite3.connect(database) as connection:
                    journal = connection.execute(
                        "SELECT source_type,source_id,journal_position FROM pc_sources WHERE scope_id=? ORDER BY journal_position",
                        (sid,),
                    ).fetchall()
                    cursors = connection.execute("SELECT * FROM pc_source_cursors WHERE scope_id=?", (sid,)).fetchall()
                observations.append({
                    "phase": phase,
                    "available": available,
                    "memory_ref": current.model_dump(mode="json"),
                    "memory_hit_count": len(memory_page.hits),
                    "topic_hit_count": len(topic_page.hits),
                    "journal": journal,
                    "cursor_rows": [
                        [value.hex() if isinstance(value, bytes) else value for value in row] for row in cursors
                    ],
                    "provider_calls": server.generation_calls,
                })
            for index, row in enumerate(rows):
                assert await lifecycle.resolve(f"memory{index}") == row.citation
        receiver = Path(__file__).parents[1] / "support/context_pack_receiver.py"
        for evidence, wires in streams.items():
            child = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(receiver)],
                input=json.dumps({"wires": wires}),
                capture_output=True,
                text=True,
                check=True,
            )
            decoded = json.loads(child.stdout)
            assert [base64.b64decode(value) for value in decoded["contents_base64"]] == targets[evidence]
            (tmp_path / f"receiver-{evidence}.json").write_text(child.stdout, encoding="utf-8")
            (tmp_path / f"wires-{evidence}.json").write_text(
                json.dumps({
                    "wires": wires,
                    "target_bytes": [base64.b64encode(value).decode() for value in targets[evidence]],
                }),
                encoding="utf-8",
            )
    (tmp_path / "combined_observation.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
