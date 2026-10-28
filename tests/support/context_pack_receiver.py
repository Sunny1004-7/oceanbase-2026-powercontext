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

"""Standalone receiver oracle: only serialized wire bytes enter this process."""

from __future__ import annotations

import base64
import hashlib
import json
import sys


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(encoded):
    unsigned = {"schema": "powercontext.context-pack.v1", "items": encoded}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def receive(wires):
    encoded = []
    current_digest = None
    contents, digests = [], []
    for wire_base64 in wires:
        raw = base64.b64decode(wire_base64, validate=True)
        value = json.loads(raw.decode("utf-8"))
        if value["schema"] == "powercontext.context-pack.v1":
            assert set(value) == {"schema", "items", "digest", "byte_count"}
            assert type(value["byte_count"]) is int and value["byte_count"] == len(_canonical(value))
            encoded = value["items"]
            assert _digest(encoded) == value["digest"]
            current_digest = value["digest"]
        else:
            assert set(value) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
            assert value["schema"] == "powercontext.context-pack-delta.v1"
            assert value["base_digest"] == current_digest
            prefix = value["prefix_items"]
            assert type(prefix) is int and 0 <= prefix <= len(encoded)
            encoded = encoded[:prefix] + value["appended"]
            current_digest = _digest(encoded)
            assert current_digest == value["target_digest"]
        chunks = [base64.b64decode(item, validate=True) for item in encoded]
        for chunk in chunks:
            chunk.decode("utf-8")
        contents.append(base64.b64encode(b"".join(chunks)).decode())
        digests.append(current_digest)
    return {"contents_base64": contents, "digests": digests}


if __name__ == "__main__":
    print(json.dumps(receive(json.load(sys.stdin)["wires"])))
