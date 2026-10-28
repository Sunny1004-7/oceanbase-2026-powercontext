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

"""Capture complete Pi v3 branch events with durable immutable checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Never, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    """One complete archive and an optional explicit branch endpoint."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """A visible event retaining its complete original JSON record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class _InvalidPiSessionError(ValueError):
    """The archive or its committed immutable history is invalid."""


def _reject_constant(_value: str) -> Never:
    raise _InvalidPiSessionError("nonfinite-json-number")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise _InvalidPiSessionError("expected-string")
    return value


def _object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _InvalidPiSessionError("expected-record-object")
    return cast("dict[str, JsonValue]", value)


def _archive(value: PiSessionInput) -> tuple[str, dict[str, dict[str, JsonValue]], str | None]:
    rows = [
        json.loads(line, parse_constant=_reject_constant)
        for line in value.content.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    if not rows:
        raise _InvalidPiSessionError("missing-session-header")
    header = _object(rows[0])
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise _InvalidPiSessionError("invalid-session-header")
    session_id = _string(header.get("id"))
    if not session_id:
        raise _InvalidPiSessionError("empty-session-id")
    nodes: dict[str, dict[str, JsonValue]] = {}
    for row in rows[1:]:
        record = _object(row)
        event_id = _string(record.get("id"))
        if not event_id or event_id in nodes or "parentId" not in record:
            raise _InvalidPiSessionError("invalid-or-duplicate-node")
        parent = record["parentId"]
        if parent is not None and (not isinstance(parent, str) or (parent != "" and parent not in nodes)):
            raise _InvalidPiSessionError("parent-must-precede-node")
        if not _string(record.get("type")):
            raise _InvalidPiSessionError("empty-event-type")
        nodes[event_id] = record
    leaf = value.leaf_id if value.leaf_id is not None else next(reversed(nodes), None)
    if leaf is not None and leaf not in nodes:
        raise _InvalidPiSessionError("missing-leaf")
    return session_id, nodes, leaf


def _blocks(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        raise _InvalidPiSessionError("invalid-message-content")
    parts: list[str] = []
    for value_block in value:
        block = _object(value_block)
        if block.get("type") == "text":
            parts.append(_string(block.get("text")))
        elif block.get("type") == "toolCall":
            arguments = _canonical(block.get("arguments", {})).decode("utf-8")
            parts.append(f"[tool_call {_string(block.get('name'))} {_string(block.get('id'))}] {arguments}")
    return "\n".join(parts)


def _event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = _string(record["type"])
    if event_type in {"compaction", "branch_summary"}:
        role, body = event_type, _string(record.get("summary", ""))
    elif event_type == "custom_message":
        role, body = "custom", _blocks(record.get("content"))
    elif event_type == "message":
        message = _object(record.get("message"))
        role = _string(message.get("role"))
        if role == "bashExecution":
            command, output = _string(message.get("command", "")), _string(message.get("output", ""))
            body = f"$ {command}\n{output}" if command or output else ""
        else:
            body = _blocks(message.get("content"))
            if role == "toolResult" and body:
                body = f"[tool_result {_string(message.get('toolName', ''))} {_string(message.get('toolCallId', ''))}] {body}"
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=_string(record["id"]),
        parent_id=cast("str | None", record["parentId"]),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _branch(session_id: str, nodes: dict[str, dict[str, JsonValue]], leaf: str | None) -> tuple[PiEventInput, ...]:
    chain: list[PiEventInput] = []
    while leaf:
        record = nodes[leaf]
        event = _event(session_id, record)
        if event is not None:
            chain.append(event)
        leaf = cast("str | None", record["parentId"])
    return tuple(reversed(chain))


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the entire graph before returning the selected root-to-leaf evidence."""

    session_id, nodes, leaf = _archive(value)
    return _branch(session_id, nodes, leaf)


def _source_id(session_id: str, event_id: str) -> str:
    return "pi:" + hashlib.sha256(_canonical([session_id, event_id])).hexdigest()


class _PiEventSource(Source):
    event: PiEventInput


class _PiEventAdapter:
    input_class = PiEventInput
    source_class = _PiEventSource
    name = "pi-event"

    async def resolve(self, value: PiEventInput, /) -> _PiEventSource:
        return _PiEventSource(
            name=_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            event=value.model_copy(deep=True),
        )

    async def read(self, source: _PiEventSource, /) -> PiEventInput:
        return source.event.model_copy(deep=True)


class _PiTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = _PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: _PiEventSource, /) -> TextEvidence:
        event = source.event
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=event.content,
            metadata=event.model_dump(include={"session_id", "event_id", "parent_id", "event_type", "role"}),
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, _PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventAdapter(),
    projections=(_PiTextProjection(),),
)


class _Checkpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    imported: dict[str, str] = Field(default_factory=dict)


class PiSessionConnector:
    """Import new visible branch events; ConnectorLifecycle owns checkpoint commits."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, nodes, leaf = _archive(PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id))
        # Resolve the complete selected output before the first durable submission.
        events = _branch(session_id, nodes, leaf)
        previous = (
            _Checkpoint(session_id=session_id)
            if session.checkpoint is None
            else _Checkpoint.model_validate(session.checkpoint)
        )
        if previous.session_id != session_id:
            raise _InvalidPiSessionError("checkpoint-session-mismatch")
        hashes = {event_id: hashlib.sha256(_canonical(record)).hexdigest() for event_id, record in nodes.items()}
        if any(hashes.get(event_id) != digest for event_id, digest in previous.imported.items()):
            raise _InvalidPiSessionError("imported-event-missing-or-changed")
        imported = dict(previous.imported)
        for event in events:
            if event.event_id in imported:
                continue
            outcome = await session.submit(_source_id(session_id, event.event_id), "pi-event", event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE)
            imported[event.event_id] = hashes[event.event_id]
        checkpoint = _Checkpoint(session_id=session_id, imported=imported)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=cast("JsonValue", checkpoint.model_dump()),
        )
