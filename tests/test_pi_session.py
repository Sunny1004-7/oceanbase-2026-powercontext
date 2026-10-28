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

"""Pi projection and identity checks beyond the published examples."""

from __future__ import annotations

import json

import pytest

from powercontext.sources import SourceDefinitionRegistry
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiSessionInput, parse_pi_session
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _archive(records, session_id="session"):
    return "\n".join(
        json.dumps(row, ensure_ascii=False)
        for row in [
            {"type": "session", "version": 3, "id": session_id},
            *records,
        ]
    ).encode()


@pytest.mark.asyncio
async def test_all_visible_event_kinds_preserve_complete_records_and_block_order() -> None:
    records = [
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "  第一行\r\n第二行  "},
        },
        {"type": "model_change", "id": "control", "parentId": "a"},
        {
            "type": "message",
            "id": "b",
            "parentId": "control",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": " before "},
                    {"type": "thinking", "thinking": "ignored"},
                    {"type": "image", "data": "ignored"},
                    {"type": "toolCall", "name": "read", "id": "call", "arguments": {"z": "舟山", "a": [2, 1]}},
                    {"type": "text", "text": " after "},
                ],
            },
            "timestamp": "original",
        },
        {
            "type": "message",
            "id": "c",
            "parentId": "b",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call",
                "content": [{"type": "text", "text": "一"}, {"type": "text", "text": "二"}],
            },
        },
        {"type": "compaction", "id": "d", "parentId": "c", "summary": "  摘要  "},
        {"type": "branch_summary", "id": "e", "parentId": "d", "summary": "原始分支摘要"},
        {
            "type": "custom_message",
            "id": "f",
            "parentId": "e",
            "content": [{"type": "text", "text": "定制"}, {"type": "text", "text": "🌊"}],
        },
        {
            "type": "message",
            "id": "g",
            "parentId": "f",
            "message": {"role": "bashExecution", "command": "printf data", "output": "  data\n", "content": []},
        },
        {"type": "message", "id": "h", "parentId": "g", "message": {"role": "user", "content": "second user"}},
    ]
    events = parse_pi_session(PiSessionInput(content=b"\xef\xbb\xbf" + _archive(records)))
    assert [event.content for event in events] == [
        "user:   第一行\r\n第二行  ",
        'assistant:  before \n[tool_call read call] {"a":[2,1],"z":"舟山"}\n after ',
        "toolResult: [tool_result read call] 一\n二",
        "compaction:   摘要  ",
        "branch_summary: 原始分支摘要",
        "custom: 定制\n🌊",
        "bashExecution: $ printf data\n  data\n",
        "user: second user",
    ]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    originals = {record["id"]: record for record in records}
    for event in events:
        assert event.record == originals[event.event_id]
        source = await registry.resolve(event)
        assert await registry.read(source) == event
        projection = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
        assert projection.content == event.content
        assert projection.metadata == {
            key: getattr(event, key) for key in ("session_id", "event_id", "parent_id", "event_type", "role")
        }


@pytest.mark.asyncio
async def test_long_and_ambiguous_identity_components_never_truncate_or_merge() -> None:
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    names = []
    for session_id, event_id in (("ab", "c"), ("a", "bc"), ("a:b", "c"), ("a", "b:c"), ("🌊" * 900, "船" * 900)):
        event = parse_pi_session(
            PiSessionInput(
                content=_archive(
                    [
                        {
                            "type": "message",
                            "id": event_id,
                            "parentId": None,
                            "message": {"role": "user", "content": "same"},
                        },
                    ],
                    session_id,
                )
            )
        )[0]
        source = await registry.resolve(event)
        assert len(source.name) <= 256
        assert (await registry.resolve(event)).name == source.name
        assert await registry.read(source) == event
        names.append(source.name)
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    "tail",
    [
        b'{"type":"model_change","id":"other","parentId":"missing"}',
        b'{"type":"model_change","id":"other"}',
        b'{"type":"model_change","id":"other","parentId":"future"}',
        b"{bad json}",
        b"\xff",
    ],
)
def test_invalid_unselected_tail_fails_before_returning_a_good_branch(tail: bytes) -> None:
    good = _archive([
        {"type": "message", "id": "ok", "parentId": None, "message": {"role": "user", "content": "valid"}}
    ])
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=good + b"\n" + tail, leaf_id="ok"))
