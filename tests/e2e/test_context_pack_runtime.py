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

"""Native prepare, authorizer forwarding and a standalone process receiver."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta
from powercontext.builtin.scope import ScopeDraft


@pytest.mark.asyncio
async def test_native_incremental_prepare_matches_plain_prepare_in_a_separate_receiver(
    tmp_path: Path, monkeypatch
) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"))
    wires, bodies, authorized = [], [], []
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Context", summary="independent receiver", idempotency_key="ctx")
        )
        memory = runtime.memory.for_scope(scope.scope_id)
        context = runtime.context.for_scope(scope.scope_id)
        ordinary_prepare = context.prepare
        calls = 0

        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await ordinary_prepare(*args, **kwargs)

        async def authorize(scopes):
            authorized.append(scopes)

        monkeypatch.setattr(context, "prepare", counted)
        base = None
        for index, query in enumerate(("project", "project", "harbour", "nohitquartz")):
            if index < 2:
                await memory.remember(
                    RememberMemoryRequest(
                        entries=(
                            MemoryEntryInput(
                                kind="fact", text=f"Project harbour {index} 舟山 🌊 " + ("evidence " * 60)
                            ),
                        )
                    )
                )
            request = PrepareContextRequest(query=query, max_bytes=5000)
            expected = await ordinary_prepare(request)
            before = calls
            value = await context.prepare_incremental(request, base_pack=base, authorize_scopes=authorize)
            assert calls == before + 1
            assert isinstance(value, ContextPack) if base is None else isinstance(value, ContextPackDelta)
            bodies.append((expected.content or "").encode())
            wires.append(value.to_bytes())
            # The sender only retains a normal target pack; the child receives no objects.
            base = ContextPack.from_content(expected.content or "")
        assert bodies[0] and bodies[1] and bodies[0] != bodies[1] and bodies[-1] == b""
        assert authorized == [(scope.scope_id,)] * 4

        async def denied(_scopes):
            raise PermissionError("denied")

        with pytest.raises(PermissionError, match="denied"):
            await context.prepare_incremental(PrepareContextRequest(query="project"), authorize_scopes=denied)
    receiver = Path(__file__).parents[1] / "support/context_pack_receiver.py"
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(receiver)],
        input=json.dumps({
            "wires": [base64.b64encode(wire).decode() for wire in wires],
        }),
        capture_output=True,
        text=True,
        check=True,
    )
    decoded = json.loads(child.stdout)
    assert [base64.b64decode(item) for item in decoded["contents_base64"]] == bodies
    (tmp_path / "context-runtime-audit.json").write_text(
        json.dumps(
            {
                "backend": "real SQLite BuiltinRuntime",
                "receiver": "separate stdlib-only Python process",
                "receiver_command": [sys.executable, str(receiver)],
                "receiver_exit": child.returncode,
                "wires_base64": [base64.b64encode(wire).decode() for wire in wires],
                "ordinary_contents_base64": [base64.b64encode(body).decode() for body in bodies],
                "receiver_results": decoded,
                "authorized_scopes": authorized,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
