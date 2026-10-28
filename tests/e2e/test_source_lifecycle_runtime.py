"""Lifecycle acceptance checks against native SQLite state and historical reads."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.sources import SourceRef

checks = importlib.import_module("challenges.memory-01-source-lifecycle.acceptance.checks")


async def test_scope_isolation_mixed_graph_restart_and_historical_bytes(tmp_path):
    settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'scopes.db'}"))
    async with open_builtin_runtime(settings) as runtime:
        a, service_a, graph_a, citations_a = await checks.build(runtime, suffix="a")
        b, _service_b, graph_b, citations_b = await checks.build(runtime, suffix="b")
        for root, seq, available in [("root0", 1, False), ("root1", 1, False), ("root0", 2, True)]:
            await service_a.apply(checks.event(root, seq, available))
        await checks.assert_runtime(runtime, a, graph_a, citations_a, checks.oracle(graph_a, {"root1"}))
        await checks.assert_runtime(runtime, b, graph_b, citations_b, checks.oracle(graph_b, set()))
    async with open_builtin_runtime(settings) as runtime:
        assert runtime.lifecycle is not None
        await runtime.lifecycle.for_scope(b).apply(checks.event("root0", 1, False))
        await runtime.lifecycle.for_scope(a).apply(checks.event("root1", 2, True))
        await checks.assert_runtime(runtime, a, graph_a, citations_a, checks.oracle(graph_a, set()))
        await checks.assert_runtime(runtime, b, graph_b, citations_b, checks.oracle(graph_b, {"root0"}))
        for sid, citations in [(a, citations_a), (b, citations_b)]:
            for node, citation in citations.items():
                if isinstance(citation, MemoryCitation):
                    exact = await runtime.memory.for_scope(sid).get(GetMemoryEntryRequest(citation=citation))
                    assert exact.entry.entry_version_id == citation.entry_version_id
                    assert exact.entry.text == f"{node} fixture evidence"


@pytest.mark.parametrize("restart", [False, True])
async def test_atomic_native_memory_failure_replay_same_event(tmp_path, restart):
    database = tmp_path / "atomic.db"
    settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    observations = {"restart": restart}
    async with open_builtin_runtime(settings) as runtime:
        sid, lifecycle, graph, citations = await checks.build(runtime)
        before = (await runtime.memory.for_scope(sid).list()).memory_ref
        assert before is not None
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_native_memory BEFORE INSERT ON pc_artifacts "
                "WHEN NEW.family = 'memory' BEGIN SELECT RAISE(ABORT, 'native lifecycle write interrupted'); END"
            )
        event = checks.event("root0", 1, False)
        with pytest.raises(IntegrityError, match="native lifecycle write interrupted"):
            await lifecycle.apply(event)
        await checks.assert_runtime(runtime, sid, graph, citations, checks.oracle(graph, set()))
        assert (await runtime.memory.for_scope(sid).list()).memory_ref == before
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT sequence,available,event FROM pc_source_lifecycle_nodes WHERE scope_id=? AND node_id='root0'",
                (sid,),
            ).fetchone()
            assert row == (0, 1, None)
            observations["after_failure"] = {"source_row": row, "memory_ref": before.model_dump(mode="json")}
            connection.execute("DROP TRIGGER fail_native_memory")
        if not restart:
            await lifecycle.apply(event)
    async with open_builtin_runtime(settings) as runtime:
        assert runtime.lifecycle is not None
        lifecycle = runtime.lifecycle.for_scope(sid)
        await lifecycle.apply(event)
        await checks.assert_runtime(runtime, sid, graph, citations, checks.oracle(graph, {"root0"}))
        after = (await runtime.memory.for_scope(sid).list()).memory_ref
        assert after is not None
        assert after.revision == before.revision + 1
        await lifecycle.apply(event)
        assert (await runtime.memory.for_scope(sid).list()).memory_ref == after
        await lifecycle.apply(checks.event("root0", 2, True))
        await checks.assert_runtime(runtime, sid, graph, citations, checks.oracle(graph, set()))
        observations["after_retry"] = after.model_dump(mode="json")
    (tmp_path / "atomic_observation.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")


async def test_registration_failure_rolls_back_node_and_initial_state(tmp_path):
    database = tmp_path / "registration.db"
    settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    async with open_builtin_runtime(settings) as runtime:
        sid, lifecycle, _, _ = await checks.build(runtime)
        await lifecycle.apply(checks.event("root0", 1, False))
        result = await runtime.memory.for_scope(sid).remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="registration needle evidence"),))
        )
        assert result.entry is not None
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_registration BEFORE INSERT ON pc_artifacts "
                "WHEN NEW.family = 'memory' BEGIN SELECT RAISE(ABORT, 'registration interrupted'); END"
            )
        with pytest.raises(IntegrityError, match="registration interrupted"):
            await lifecycle.register_memory("late", result.entry.citation, parents=("root0",), policy="all")
        with pytest.raises(SourceLifecycleError):
            await lifecycle.resolve("late", purpose="audit")
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute("SELECT count(*) FROM pc_source_lifecycle_nodes WHERE node_id='late'").fetchone()[0]
                == 0
            )
            connection.execute("DROP TRIGGER fail_registration")
        await lifecycle.register_memory("late", result.entry.citation, parents=("root0",), policy="all")
        with pytest.raises(SourceLifecycleError):
            await lifecycle.resolve("late")
        assert await lifecycle.resolve("late", purpose="audit") == result.entry.citation


async def test_reference_validation_strict_replay_and_unregistered_topic_pagination(tmp_path):
    settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}"))
    async with open_builtin_runtime(settings) as runtime:
        sid, lifecycle, _, citations = await checks.build(runtime)
        with pytest.raises(SourceLifecycleError):
            await lifecycle.register_source("absent", SourceRef(source_type="content", source_id="absent"), revision=3)
        with pytest.raises(SourceLifecycleError):
            await lifecycle.register_topic(
                "absent", ArtifactRef(family="topic-memory", artifact_id="absent", revision=1), parents=("root0",)
            )
        with pytest.raises(SourceLifecycleError):
            await lifecycle.register_memory(
                "absent", citations["needle0"].model_copy(update={"entry_version_id": "absent"}), parents=("root0",)
            )
        with pytest.raises(SourceLifecycleError):
            await lifecycle.register_memory("badparents", citations["needle0"], parents=("root0", "root0"))
        with pytest.raises(SourceLifecycleError):
            await lifecycle.resolve("root0", purpose="unknown")
        await lifecycle.apply(checks.event("root0", 1, False))
        with pytest.raises(SourceLifecycleError):
            await lifecycle.apply(checks.event("root0", 1, False).model_copy(update={"reason": "different"}))
        # Put many withdrawn matches before an unregistered legal match. The
        # eligibility predicate must precede native channel limits and browse.
        for number in range(34):
            topic = await runtime.records.for_scope(sid).create_artifact(
                "topic-memory",
                ArtifactWrite(
                    content={
                        "title": f"pagination {number}",
                        "summary": "pagination evidence",
                        "detail": "pagination evidence",
                    }
                ),
            )
            await lifecycle.register_topic(
                f"page{number}",
                ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision),
                parents=("root0",),
            )
        legal = await runtime.records.for_scope(sid).create_artifact(
            "topic-memory",
            ArtifactWrite(
                content={
                    "title": "pagination legal",
                    "summary": "pagination legal evidence",
                    "detail": "pagination legal evidence",
                }
            ),
        )
        page = await runtime.topic_memory.for_scope(sid).search(SearchTopicMemoryRequest(query="pagination", limit=1))
        assert [hit.artifact_ref.artifact_id for hit in page.hits] == [legal.artifact_id]
        browse = await runtime.topic_memory.for_scope(sid).browse(limit=1)
        assert browse[0].artifact_ref.artifact_id == legal.artifact_id
        prepared = await runtime.context.for_scope(sid).prepare(
            PrepareContextRequest(query="pagination", max_bytes=3000)
        )
        assert "pagination legal evidence" in (prepared.content or "")


def test_event_strict_field_boundaries():
    valid = {"node_id": "root", "revision": 3, "sequence": 1, "available": True, "reason": "controlled"}
    for field, invalids in {
        "node_id": ["", "  ", 1],
        "reason": ["", "\t", None],
        "revision": [True, "3", 3.0, 0, -1],
        "sequence": [False, "1", 1.0, 0, -1],
        "available": [1, "true", None],
    }.items():
        for value in invalids:
            with pytest.raises(ValidationError):
                SourceLifecycleEvent.model_validate({**valid, field: value})
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate({**valid, "extra": "no"})
