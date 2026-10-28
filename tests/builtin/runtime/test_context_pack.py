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

"""Wire checks use independent JSON, Base64, digest and byte-count oracles."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


def _oracle(items):
    encoded = [base64.b64encode(item).decode() for item in items]
    unsigned = {"schema": "powercontext.context-pack.v1", "items": encoded}
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value = {**unsigned, "digest": "sha256:" + hashlib.sha256(canonical(unsigned)).hexdigest(), "byte_count": 0}
    while value["byte_count"] != len(canonical(value)):
        value["byte_count"] = len(canonical(value))
    return canonical(value)


@pytest.mark.parametrize(
    "content,limit",
    [
        ("", 1),
        ("ééé", 2),
        ("aé海🌊\r\n尾\x00", 4),
        ("海🌊é" * 100, 7),
        ("🌊" * 260, 512),
    ],
)
def test_bounded_unicode_chunks_keep_every_original_byte(content: str, limit: int) -> None:
    pack = ContextPack.from_content(content, chunk_bytes=limit)
    assert b"".join(pack.items) == content.encode()
    assert all(len(item) <= limit and item.decode().encode() == item for item in pack.items)
    assert pack.to_bytes() == _oracle(pack.items)
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


@pytest.mark.parametrize("size", [0, 1, 500, 610, 620, 630, 6400, 6500, 6600])
def test_self_inclusive_count_survives_decimal_digit_boundaries(size: int) -> None:
    pack = ContextPack.build((b"x" * size,))
    assert pack.to_bytes() == _oracle(pack.items)
    assert pack.byte_count == len(pack.to_bytes())


def test_ordinary_json_whitespace_and_key_order_decode_to_canonical_wire() -> None:
    pack = ContextPack.from_content("海洋\nspaced JSON")
    payload = dict(reversed(list(json.loads(pack.to_bytes()).items())))
    noncanonical = json.dumps(payload, indent=4).encode()
    assert len(noncanonical) > pack.byte_count
    assert ContextPack.from_bytes(noncanonical).to_bytes() == pack.to_bytes()
    target = ContextPack.build((*pack.items, b"tail"))
    delta = ContextPackDelta.between(pack, target)
    assert (
        apply_context_pack_delta(
            pack, ContextPackDelta.from_bytes(json.dumps(json.loads(delta.to_bytes()), indent=2).encode())
        )
        == target
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": 1},
        {"schema": "wrong"},
        {"items": "not-list"},
        {"items": [None]},
        {"items": ["bad!"]},
        {"items": ["/w=="]},
        {"items": ["YQ==="]},
        {"digest": "sha256:" + "a" * 64},
        {"byte_count": True},
        {"byte_count": 1.0},
        {"byte_count": 0},
    ],
)
def test_pack_decoder_rejects_wrong_fields_types_encoding_and_integrity(mutation) -> None:
    payload = {**json.loads(ContextPack.build((b"a",)).to_bytes()), **mutation}
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": 1},
        {"schema": "wrong"},
        {"appended": None},
        {"appended": [None]},
        {"appended": ["/w=="]},
        {"prefix_items": True},
        {"prefix_items": 1.0},
        {"prefix_items": None},
        {"prefix_items": -1},
        {"base_digest": 3},
        {"target_digest": "bad"},
    ],
)
def test_delta_decoder_rejects_invalid_wire(mutation) -> None:
    base = ContextPack.build((b"a",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"a", b"b")))
    payload = {**json.loads(delta.to_bytes()), **mutation}
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]", b"null"])
def test_both_decoders_normalize_invalid_wire_to_protocol_error(raw: bytes) -> None:
    for decoder in (ContextPack.from_bytes, ContextPackDelta.from_bytes):
        with pytest.raises(ContextPackError):
            decoder(raw)


@pytest.mark.parametrize("content,limit", [("海", 2), ("🌊", 3), ("a", 0), ("a", True), ("a", 2.5), ("\ud800", 512)])
def test_unrepresentable_chunk_boundaries_are_explicit_errors(content, limit) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content(content, chunk_bytes=limit)


def test_non_longest_prefix_and_empty_transitions_rebuild_without_sender_state() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build((b"a", b"b", b"new"))
    delta = ContextPackDelta(base.digest, target.digest, 1, target.items[1:])
    assert (
        apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes()))
        == target
    )
    empty = ContextPack.build(())
    for first, last in ((base, empty), (empty, target), (empty, empty), (base, base)):
        rebuilt = apply_context_pack_delta(first, ContextPackDelta.between(first, last))
        assert rebuilt.to_bytes() == last.to_bytes()
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, target.digest, 4, ()))
