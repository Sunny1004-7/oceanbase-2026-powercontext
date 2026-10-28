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

"""Per-event-loop embedding coalescing with bounded successful-vector reuse."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(frozen=True, slots=True, eq=False)
class _BatchReceipt:
    """Identity, rather than equal token values, distinguishes provider batches."""

    usage: InferenceUsage


@dataclass(frozen=True, slots=True)
class _ReadyVector:
    vector: EmbeddingVector
    receipt: _BatchReceipt | None = None


def _batch_receipt(result: EmbeddingResult, expected_count: int) -> _BatchReceipt:
    if len(result.vectors) != expected_count:
        raise ValueError("provider-vector-count-mismatch")
    return _BatchReceipt(result.usage.model_copy(deep=True))


def _observe_failure(future: asyncio.Future[_ReadyVector]) -> None:
    # All waiters may cancel; still retrieve failures without changing await semantics.
    if not future.cancelled():
        future.exception()


def _usage(receipts: set[_BatchReceipt]) -> InferenceUsage:
    def tokens(field: str) -> int | None:
        values = [getattr(receipt.usage, field) for receipt in receipts]
        return None if any(value is None for value in values) else sum(values)

    return InferenceUsage(
        requests=sum(receipt.usage.requests for receipt in receipts),
        input_tokens=tokens("input_tokens"),
        output_tokens=tokens("output_tokens"),
    )


class CoalescingEmbeddingModel:
    """Share exact-text work inside one event loop and one model instance.

    Only successful vectors enter the LRU. Pending vectors retain a receipt for
    their actual provider batch, so each waiter accounts for shared work once.
    Cancelling a caller does not cancel that shared work.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("invalid-max-batch-size")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("invalid-cache-size")
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (int, float))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("invalid-coalesce-window")
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, asyncio.Future[_ReadyVector]] = {}
        self._queue: deque[str] = deque()
        self._dispatcher: asyncio.Task[None] | None = None
        self._batches: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return ordered vectors and usage for the batches this call waited on."""

        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0, input_tokens=0, output_tokens=0))
        loop = asyncio.get_running_loop()
        slots: list[_ReadyVector | asyncio.Future[_ReadyVector]] = []
        # Registration has no await: another caller cannot miss newly pending text.
        for text in texts:
            if text in self._cache:
                vector = self._cache.pop(text)
                self._cache[text] = vector
                slots.append(_ReadyVector(vector))
                continue
            future = self._pending.get(text)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_observe_failure)
                self._pending[text] = future
                self._queue.append(text)
            slots.append(future)
        if self._queue and self._dispatcher is None:
            self._dispatcher = loop.create_task(self._dispatch())
        waiting = {slot for slot in slots if isinstance(slot, asyncio.Future)}
        if waiting:
            await asyncio.gather(*(asyncio.shield(future) for future in waiting))
        ready = [slot.result() if isinstance(slot, asyncio.Future) else slot for slot in slots]
        receipts = {item.receipt for item in ready if item.receipt is not None}
        return EmbeddingResult(vectors=tuple(item.vector for item in ready), usage=_usage(receipts))

    async def _dispatch(self) -> None:
        await asyncio.sleep(self._delay)
        queued = tuple(self._queue)
        self._queue.clear()
        self._dispatcher = None
        for start in range(0, len(queued), self._max_batch_size):
            texts = queued[start : start + self._max_batch_size]
            task = asyncio.create_task(self._run_batch(texts))
            self._batches.add(task)
            task.add_done_callback(self._batches.discard)

    async def _run_batch(self, texts: tuple[str, ...]) -> None:
        futures = tuple(self._pending[text] for text in texts)
        try:
            result = await self._model.embed(texts)
            receipt = _batch_receipt(result, len(texts))
        except (Exception, asyncio.CancelledError) as error:
            for text, future in zip(texts, futures, strict=True):
                self._pending.pop(text)
                future.set_exception(error)
        else:
            for text, vector, future in zip(texts, result.vectors, futures, strict=True):
                self._pending.pop(text)
                if self._cache_size:
                    self._cache[text] = vector
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future.set_result(_ReadyVector(vector, receipt))
