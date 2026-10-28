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

"""Canonical UTF-8 context packs and independently applicable prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Self, cast

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Malformed context wire data, chunks, or delta anchors."""


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("expected-bytes-tuple")
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("invalid-utf8-chunk") from error


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextPackError("expected-base64-string-list")
    items = []
    for encoded in cast("list[str]", value):
        try:
            item = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ContextPackError("invalid-base64") from error
        if base64.b64encode(item).decode("ascii") != encoded:
            raise ContextPackError("noncanonical-base64")
        items.append(item)
    decoded = tuple(items)
    _validate_items(decoded)
    return decoded


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("invalid-digest")
    return value


def _payload(raw: bytes, fields: set[str], schema: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise ContextPackError("expected-wire-bytes")
    try:
        payload: object = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ContextPackError("invalid-json") from error
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ContextPackError("invalid-wire-fields")
    result = cast("dict[str, object]", payload)
    if result["schema"] != schema:
        raise ContextPackError("invalid-schema")
    return result


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Immutable valid UTF-8 chunks, with derived digest and self-inclusive size."""

    items: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_items(self.items)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> Self:
        return cls(items)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> Self:
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("invalid-chunk-byte-limit")
        try:
            data = content.encode("utf-8")
        except (AttributeError, UnicodeError) as error:
            raise ContextPackError("invalid-content") from error
        items = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and end > start and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("codepoint-exceeds-chunk-byte-limit")
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    @property
    def digest(self) -> str:
        unsigned = {"schema": _PACK_SCHEMA, "items": _encode_items(self.items)}
        return "sha256:" + hashlib.sha256(_json(unsigned)).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        payload = {"schema": _PACK_SCHEMA, "items": _encode_items(self.items), "digest": self.digest, "byte_count": 0}
        while True:
            wire = _json(payload)
            if payload["byte_count"] == len(wire):
                return wire
            payload["byte_count"] = len(wire)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        payload = _payload(raw, {"schema", "items", "digest", "byte_count"}, _PACK_SCHEMA)
        pack = cls.build(_decode_items(payload["items"]))
        count = payload["byte_count"]
        if type(count) is not int or count != pack.byte_count or _digest(payload["digest"]) != pack.digest:
            raise ContextPackError("pack-digest-or-size-mismatch")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A retained base prefix and a complete ordered target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _digest(self.base_digest)
        _digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("invalid-prefix")
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> Self:
        prefix = 0
        while prefix < min(len(base.items), len(target.items)) and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        payload = _payload(raw, {"schema", "base_digest", "target_digest", "prefix_items", "appended"}, _DELTA_SCHEMA)
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("invalid-prefix")
        return cls(
            _digest(payload["base_digest"]),
            _digest(payload["target_digest"]),
            prefix,
            _decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild solely from base chunks and wire data, checking both anchors."""

    if delta.base_digest != base.digest or delta.prefix_items > len(base.items):
        raise ContextPackError("base-or-prefix-mismatch")
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return rebuilt
