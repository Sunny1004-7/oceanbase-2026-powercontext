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

"""HTTP protocol, global ordering, and failure atomicity through the public API."""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryReranker

ENDPOINT = "https://reranker.invalid/custom/score?model=local"


def _hit(identity: int, text: str = "  相同正文\r\n🌊\t ") -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id=f"m{identity}", revision=identity + 1),
        entry_id=f"entry-{identity}",
        entry_version_id=f"version-{identity}",
        text=text,
        score=1.0 / (identity + 1),
        matched_by=("fts",),
    )


@pytest.mark.parametrize("batch_size", [1, 2, 3, 6, 32])
@pytest.mark.parametrize("limit", [1, 6])
@pytest.mark.asyncio
async def test_global_order_and_exact_identity_are_independent_of_batches(batch_size: int, limit: int) -> None:
    # Input positions intentionally differ from IDs; equal text never merges identities.
    candidates = tuple(_hit(i) for i in (19, 4, 27, 2, 9, 8))
    before = tuple(candidate.model_dump(mode="json") for candidate in candidates)
    query = "  Query 舟山\nβ\t "
    scores = {1: -3, 2: 0, 3: 0.12500000000000003, 4: 0.125, 5: 0, 6: 9}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert request.headers["x-client-owner"] == "caller"
        body = json.loads(request.content)
        assert set(body) == {"query", "candidates"} and body["query"] == query
        assert 1 <= len(body["candidates"]) <= batch_size
        for item in body["candidates"]:
            assert set(item) == {"rank", "text"}
            assert type(item["rank"]) is int
            assert item["text"] == candidates[item["rank"] - 1].text
        seen.extend(body["candidates"])
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(body["candidates"])
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"x-client-owner": "caller"}
    ) as client:
        reranker: MemoryReranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=batch_size)
        decision = await reranker.rerank(query, candidates, limit)
        assert decision.selected_ranks == (6, 3, 4, 2, 5, 1)[:limit]
        assert decision.usage.requests == math.ceil(6 / batch_size)
        assert decision.usage.input_tokens is None and decision.usage.output_tokens is None
        assert decision.discarded_rank_count == 0 and decision.used_fallback is False
        assert reranker.policy_id == "http-cross-encoder-v1"
        assert not client.is_closed
    assert [item["rank"] for item in seen] == list(range(1, 7))
    assert tuple(candidate.model_dump(mode="json") for candidate in candidates) == before


@pytest.mark.asyncio
async def test_synthetic_173_candidate_pool_has_25_batches_and_invariant_selection(tmp_path: Path) -> None:
    # Scores repeat every 23 ranks. The expected top ten are independently listed:
    # residue 22 first, then residue 21; ties retain the original global rank.
    candidates = tuple(_hit(i, text=f"synthetic-{i}") for i in range(173))
    expected = (22, 45, 68, 91, 114, 137, 160, 21, 44, 67)
    audit = []
    for batch_size in (7, 1, 32, 200):
        batches = []

        def handler(request: httpx.Request, batches=batches) -> httpx.Response:
            items = json.loads(request.content)["candidates"]
            batches.append(items)
            return httpx.Response(
                200,
                json={"scores": [{"rank": item["rank"], "score": item["rank"] % 23 - 11} for item in reversed(items)]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await HTTPMemoryReranker(ENDPOINT, client=client, batch_size=batch_size).rerank(
                "synthetic protocol only", candidates, 10
            )
        assert result.selected_ranks == expected
        assert len(batches) == result.usage.requests == math.ceil(173 / batch_size)
        assert all(1 <= len(batch) <= batch_size for batch in batches)
        assert [item["rank"] for batch in batches for item in batch] == list(range(1, 174))
        assert [item["text"] for batch in batches for item in batch] == [hit.text for hit in candidates]
        audit.append({
            "batch_size": batch_size,
            "actual_requests": len(batches),
            "usage_requests": result.usage.requests,
            "selected_ranks": result.selected_ranks,
            "batches": batches,
        })
    (tmp_path / "synthetic-173-audit.json").write_text(
        json.dumps(
            {
                "scope": "synthetic HTTP protocol only; not the official SciFact corpus or quality metrics",
                "candidate_count": 173,
                "score_rule": "global rank modulo 23 minus 11",
                "expected_top10": expected,
                "runs": audit,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "scores",
        1,
        {},
        {"scores": [], "extra": 1},
        {"scores": None},
        {"scores": {}},
        {"scores": "[]"},
        {"scores": []},
        {"scores": [None]},
        {"scores": [[]]},
        {"scores": [{"score": 1}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"rank": 1, "score": 1, "extra": 0}]},
        {"scores": [{"rank": 1, "score": 1}, {"rank": 1, "score": 2}]},
        {"scores": [{"rank": 0, "score": 1}]},
        {"scores": [{"rank": 2, "score": 1}]},
    ],
)
@pytest.mark.asyncio
async def test_rejects_schema_and_coverage_without_fallback(payload: object) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=json.dumps(payload).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", (_hit(1),), 1)
        assert not client.is_closed
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("rank", True),
        ("rank", False),
        ("rank", "1"),
        ("rank", 1.0),
        ("rank", None),
        ("score", True),
        ("score", False),
        ("score", "1"),
        ("score", None),
        ("score", []),
        ("score", {}),
        ("score", float("nan")),
        ("score", float("inf")),
        ("score", -float("inf")),
    ],
)
@pytest.mark.asyncio
async def test_rejects_coercible_types_and_nonfinite_numbers(field: str, invalid: object) -> None:
    row: dict[str, object] = {"rank": 1, "score": 1}
    row[field] = invalid
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps({"scores": [row]}).encode()))
    ) as client:
        with pytest.raises(ValueError):
            await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", (_hit(1),), 1)


@pytest.mark.asyncio
async def test_preserves_large_integer_precision_without_float_coercion() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"scores": [{"rank": 1, "score": 10**400}, {"rank": 2, "score": 10**400 + 1}]}
            )
        )
    ) as client:
        result = await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", (_hit(1), _hit(2)), 2)
    assert result.selected_ranks == (2, 1)


@pytest.mark.parametrize("status", [301, 307, 400, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_non_success_http_status_is_not_swallowed_or_followed(status: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Invalid JSON also confirms HTTP status is checked before parsing.
        return httpx.Response(status, content=b"not-json", headers={"location": "/redirected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", (_hit(1),), 1)
        assert error.value.response.status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
@pytest.mark.asyncio
async def test_preserves_exact_network_exception(error_type: type[httpx.RequestError]) -> None:
    failure = error_type("synthetic offline")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise failure

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error_type) as error:
            await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", (_hit(1),), 1)
        assert error.value is failure
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["bad-json", "wrong-batch", "missing-rank", "http", "timeout"])
@pytest.mark.asyncio
async def test_late_failure_aborts_without_retry_and_reuse_recovers(failure: str) -> None:
    broken = True
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        items = body["candidates"]
        if broken and items[0]["rank"] == 3:
            if failure == "bad-json":
                return httpx.Response(200, content=b"{")
            if failure == "wrong-batch":
                return httpx.Response(200, json={"scores": [{"rank": 1, "score": 9}, {"rank": 2, "score": 8}]})
            if failure == "missing-rank":
                return httpx.Response(200, json={"scores": [{"rank": 3, "score": 9}]})
            if failure == "http":
                return httpx.Response(503)
            raise httpx.ReadTimeout("late-batch", request=request)
        return httpx.Response(200, json={"scores": [{"rank": item["rank"], "score": item["rank"]} for item in items]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=2, policy_id="custom-policy")
        candidates = tuple(_hit(i) for i in range(5))
        error_type = (
            httpx.HTTPStatusError if failure == "http" else (httpx.ReadTimeout if failure == "timeout" else ValueError)
        )
        with pytest.raises(error_type):
            await reranker.rerank("broken", candidates, 2)
        assert len(calls) == 2  # No third batch, no retry, and no partial decision.
        broken = False
        result = await reranker.rerank("recovered", candidates, 2)
        assert result.selected_ranks == (5, 4) and result.usage.requests == 3
        again = await reranker.rerank("smaller", candidates[:1], 1)
        assert again.selected_ranks == (1,) and again.usage.requests == 1
        assert result.usage.requests == 3
        assert reranker.policy_id == "custom-policy" and not client.is_closed
    assert len(calls) == 6
    assert [body["query"] for body in calls] == ["broken"] * 2 + ["recovered"] * 3 + ["smaller"]


@pytest.mark.parametrize("value", [0, -1, True, False, "2", 2.0, None])
@pytest.mark.asyncio
async def test_invalid_batch_size_is_rejected_before_io(value) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            HTTPMemoryReranker(ENDPOINT, client=client, batch_size=value)
    assert calls == []


@pytest.mark.parametrize(
    "candidates,limit",
    [
        ((), 1),
        ((_hit(1),), 0),
        ((_hit(1),), -1),
        ((_hit(1),), 2),
        ((_hit(1),), True),
        ((_hit(1),), False),
        ((_hit(1),), "1"),
        ((_hit(1),), 1.0),
        ((_hit(1),), None),
    ],
)
@pytest.mark.asyncio
async def test_invalid_rerank_bounds_are_rejected_before_io(candidates, limit) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await HTTPMemoryReranker(ENDPOINT, client=client).rerank("q", candidates, limit)
    assert calls == []
