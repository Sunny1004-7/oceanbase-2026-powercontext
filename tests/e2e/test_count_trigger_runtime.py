"""Native count admission recovery beyond the fixed public scenarios."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3

import pytest

from powercontext.builtin.runtime import open_builtin_runtime

checks = importlib.import_module("challenges.processing-01-count-trigger.acceptance.checks")


def save(tmp_path, value):
    (tmp_path / "observation.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("family", ["memory", "topic-memory"])
async def test_provider_failure_retries_without_new_input(tmp_path, monkeypatch, family):
    database = tmp_path / "provider.db"
    prefix = family.replace("-", "_")
    with checks.provider(monkeypatch) as server:
        settings = checks.config(database, server, families=(family,), **{f"{prefix}_source_count_threshold": 2})
        async with open_builtin_runtime(settings) as runtime:
            sid = await checks.scope(runtime)
            server.fail_generation = True
            expected = checks.receipt_refs(await checks.capture(runtime, sid, checks.messages()[:2]))
            deadline = asyncio.get_running_loop().time() + 30
            while not server.generation_calls and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.1)
            assert server.generation_calls > 0
            await checks.stay_refs(runtime, sid, family, set())
            before = server.generation_calls
            server.fail_generation = False
            await checks.wait_refs(runtime, sid, family, expected)
            await checks.check_reads(runtime, sid, family, expected)
            await checks.stay_refs(runtime, sid, family, expected)
            assert runtime.artifact_processing_supervisor is not None
            save(
                tmp_path,
                {
                    "family": family,
                    "scope": sid,
                    "failed_calls": before,
                    "generation_calls": server.generation_calls,
                    "sources": sorted(expected),
                    "supervisor": runtime.artifact_processing_supervisor.family_status,
                },
            )


async def test_accepted_topic_wave_resumes_after_partial_output(tmp_path, monkeypatch):
    database = tmp_path / "wave.db"
    with checks.provider(monkeypatch) as server:
        settings = checks.config(
            database,
            server,
            families=("topic-memory",),
            topic_memory_source_count_threshold=3,
            topic_memory_source_window_limit=1,
        )
        async with open_builtin_runtime(settings) as runtime:
            sid = await checks.scope(runtime)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER interrupt_second_topic BEFORE INSERT ON pc_artifacts "
                    "WHEN NEW.family = 'topic-memory' AND (SELECT count(*) FROM pc_artifacts "
                    "WHERE family = 'topic-memory') >= 1 "
                    "BEGIN SELECT RAISE(ABORT, 'controlled partial wave failure'); END"
                )
            receipts = await checks.capture(runtime, sid, checks.messages()[:3])
            await checks.wait_refs(runtime, sid, "topic-memory", checks.receipt_refs(receipts[:1]))
            await checks.stay_refs(runtime, sid, "topic-memory", checks.receipt_refs(receipts[:1]))
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER interrupt_second_topic")
            before = connection.execute(
                "SELECT * FROM pc_artifact_processing_intents WHERE scope_id=?", (sid,)
            ).fetchall()
        async with open_builtin_runtime(settings) as runtime:
            expected = checks.receipt_refs(receipts)
            await checks.wait_refs(runtime, sid, "topic-memory", expected)
            await checks.check_reads(runtime, sid, "topic-memory", expected)
            await checks.stay_refs(runtime, sid, "topic-memory", expected)
            save(
                tmp_path,
                {
                    "scope": sid,
                    "durable_intents_before_restart": before,
                    "restored_sources": sorted(expected),
                    "provider_calls": server.generation_calls,
                },
            )
