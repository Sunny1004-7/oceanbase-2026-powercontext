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

"""Deterministic barriers exercise late sharing, cancellation, failures and billing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


def _vector(text: str) -> tuple[float, float]:
    return float(len(text)), float(sum(text.encode()))


class _BarrierProvider:
    profile = EmbeddingProfile(profile_id="r05-barrier", model="offline", dimension=2)

    def __init__(self) -> None:
        self.arrivals: asyncio.Queue[tuple[tuple[str, ...], asyncio.Event]] = asyncio.Queue()
        self.calls: list[tuple[str, ...]] = []
        self.fail: set[str] = set()
        self.responses = []

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        release = asyncio.Event()
        self.arrivals.put_nowait((texts, release))
        await release.wait()
        if self.fail.intersection(texts):
            raise RuntimeError("provider-failure")
        result = EmbeddingResult(
            vectors=tuple(_vector(text) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=0, output_tokens=None),
        )
        self.responses.append({"texts": texts, "usage": result.usage.model_dump()})
        return result


async def _entered_call(model, texts, entered):
    entered.set()
    return await model.embed(texts)


def _audit(tmp_path, provider, calls):
    (tmp_path / "embedding-audit.json").write_text(
        json.dumps(
            {
                "provider": "deterministic offline provider; event barriers, not a latency benchmark",
                "actual_provider_batches": provider.calls,
                "provider_receipts": provider.responses,
                "caller_results": calls,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("cache_size", [0, 3])
@pytest.mark.parametrize("cancel", ["first", "late"])
@pytest.mark.asyncio
async def test_late_waiter_and_cancellation_share_inflight_work(tmp_path: Path, cache_size: int, cancel: str) -> None:
    async with asyncio.timeout(5):
        base = _BarrierProvider()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0, cache_size=cache_size)
        first = asyncio.create_task(model.embed((" left ", "shared", "shared")))
        texts, release = await base.arrivals.get()
        assert texts == (" left ", "shared")
        entered = asyncio.Event()
        late = asyncio.create_task(_entered_call(model, ("shared", "shared"), entered))
        await entered.wait()
        victim, survivor = (first, late) if cancel == "first" else (late, first)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        release.set()
        result = await survivor
        expected = ("shared", "shared") if cancel == "first" else (" left ", "shared", "shared")
        assert result.vectors == tuple(_vector(text) for text in expected)
        assert result.usage == InferenceUsage(requests=3, input_tokens=0, output_tokens=None)
        assert base.calls == [(" left ", "shared")]
        if cache_size == 0:
            retry = asyncio.create_task(model.embed(("shared",)))
            again, released = await base.arrivals.get()
            assert again == ("shared",)
            released.set()
            repeated = await retry
            assert repeated.usage.requests == 3
        else:
            repeated = await model.embed(("shared",))
            assert repeated.usage.requests == 0
        assert repeated.vectors == (_vector("shared"),)
        _audit(tmp_path, base, [{"survivor": result.model_dump()}, {"after_completion": repeated.model_dump()}])


@pytest.mark.asyncio
async def test_failed_batch_isolated_from_an_independent_waiter_and_can_retry(tmp_path: Path) -> None:
    async with asyncio.timeout(5):
        base = _BarrierProvider()
        base.fail.add("bad")
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        mixed = asyncio.create_task(model.embed(("bad", "good")))
        survivor = asyncio.create_task(model.embed(("good",)))
        arrivals = dict([await base.arrivals.get(), await base.arrivals.get()])
        arrivals[("good",)].set()
        good = await survivor
        assert good.vectors == (_vector("good"),) and good.usage.requests == 3
        arrivals[("bad",)].set()
        with pytest.raises(RuntimeError, match="provider-failure"):
            await mixed
        assert (await model.embed(("good",))).usage.requests == 0
        base.fail.clear()
        retry = asyncio.create_task(model.embed(("bad",)))
        texts, release = await base.arrivals.get()
        assert texts == ("bad",)
        release.set()
        recovered = await retry
        assert recovered.vectors == (_vector("bad"),) and recovered.usage.requests == 3
        assert base.calls == [("bad",), ("good",), ("bad",)]
        _audit(tmp_path, base, [{"survivor": good.model_dump()}, {"retry": recovered.model_dump()}])


class _MeteredProvider:
    profile = _BarrierProvider.profile

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses = []
        self.invalid = False

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        usage = {
            ("a", "b"): InferenceUsage(requests=2, input_tokens=7, output_tokens=0),
            ("c",): InferenceUsage(requests=5, input_tokens=None, output_tokens=4),
        }.get(texts, InferenceUsage(requests=4, input_tokens=0, output_tokens=None))
        result = EmbeddingResult(vectors=() if self.invalid else tuple(_vector(text) for text in texts), usage=usage)
        self.responses.append({"texts": texts, "usage": usage.model_dump(), "vector_count": len(result.vectors)})
        return result


@pytest.mark.asyncio
async def test_usage_counts_distinct_batches_and_never_recharges_cache(tmp_path: Path) -> None:
    base = _MeteredProvider()
    model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0, cache_size=2)
    first = await model.embed(("a", "b", "c", "b"))
    assert first.vectors == tuple(_vector(text) for text in ("a", "b", "c", "b"))
    assert first.usage == InferenceUsage(requests=7, input_tokens=None, output_tokens=4)
    second = await model.embed(("b", "d", "e", "d"))
    assert second.vectors == tuple(_vector(text) for text in ("b", "d", "e", "d"))
    assert second.usage == InferenceUsage(requests=4, input_tokens=0, output_tokens=None)
    assert first.usage.requests == 7
    assert base.calls == [("a", "b"), ("c",), ("d", "e")]
    _audit(tmp_path, base, [first.model_dump(), second.model_dump()])


@pytest.mark.asyncio
async def test_vector_count_failure_invalidates_the_whole_batch(tmp_path: Path) -> None:
    base = _MeteredProvider()
    base.invalid = True
    model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
    with pytest.raises(ValueError):
        await model.embed(("x", "y"))
    base.invalid = False
    result = await model.embed(("x", "y"))
    assert result.vectors == (_vector("x"), _vector("y"))
    assert base.calls == [("x", "y"), ("x", "y")]
    assert (await model.embed(("x", "y", "x"))).usage.requests == 0
    _audit(tmp_path, base, [result.model_dump()])


@pytest.mark.asyncio
async def test_exact_text_keys_and_lru_access_refresh(tmp_path: Path) -> None:
    base = _MeteredProvider()
    model = CoalescingEmbeddingModel(base, max_batch_size=4, coalesce_ms=0, cache_size=3)
    first = await model.embed(("x", "X", " x", "x "))
    assert first.vectors == tuple(_vector(text) for text in ("x", "X", " x", "x "))
    assert base.calls[0] == ("x", "X", " x", "x ")
    await model.embed(("X",))  # Refresh X; x was already evicted by the fourth insertion.
    await model.embed(("new",))  # Evicts the oldest untouched " x".
    cached = await model.embed(("X",))
    assert cached.usage.requests == 0
    await model.embed((" x",))
    assert base.calls == [("x", "X", " x", "x "), ("new",), (" x",)]
    _audit(tmp_path, base, [first.model_dump(), cached.model_dump()])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_batch_size": 0},
        {"max_batch_size": True},
        {"max_batch_size": 2.0},
        {"cache_size": -1},
        {"cache_size": True},
        {"coalesce_ms": -1},
        {"coalesce_ms": float("nan")},
    ],
)
def test_invalid_limits_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_MeteredProvider(), **kwargs)
