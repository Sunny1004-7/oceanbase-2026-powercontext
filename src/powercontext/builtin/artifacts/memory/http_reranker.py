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

"""Strict HTTP scoring with stable selection over the complete Memory pool."""

from __future__ import annotations

import math
from typing import cast

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Batch scores through a caller-owned client, retaining global candidate ranks.

    HTTP and schema failures abort the whole call. No partial selection, retry, or
    coarse-order fallback is returned. The caller remains responsible for closing
    the client.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Select by score descending, then by the original one-based rank."""

        if not candidates:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("limit must be an integer between one and the candidate count")  # noqa: TRY003

        scores: dict[int, int | float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            stop = min(start + self._batch_size, len(candidates))
            ranks = range(start + 1, stop + 1)
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in ranks],
                },
                # A redirect is a non-2xx error even if the shared client follows redirects.
                follow_redirects=False,
            )
            requests += 1
            response.raise_for_status()
            scores.update(_batch_scores(response.json(), ranks))

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _batch_scores(payload: object, expected_ranks: range) -> dict[int, int | float]:
    """Validate one complete batch before its scores enter the global selection."""

    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("response must contain exactly scores")  # noqa: TRY003
    rows = cast("dict[str, object]", payload)["scores"]
    if not isinstance(rows, list) or len(rows) != len(expected_ranks):
        raise ValueError("scores must list exactly one record per candidate in this batch")  # noqa: TRY003
    scores: dict[int, int | float] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"rank", "score"}:
            raise ValueError("each score must contain exactly rank and score")  # noqa: TRY003
        fields = cast("dict[str, object]", row)
        rank, score = fields["rank"], fields["score"]
        if type(rank) is not int or rank not in expected_ranks or rank in scores:
            raise ValueError("scores must cover each rank in this batch exactly once")  # noqa: TRY003
        # Integers are finite without converting them to a potentially overflowing float.
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or (isinstance(score, float) and not math.isfinite(score))
        ):
            raise ValueError("score must be a finite JSON number")  # noqa: TRY003
        scores[rank] = score
    if len(scores) != len(expected_ranks):
        raise ValueError("scores must cover each rank in this batch exactly once")  # noqa: TRY003
    return scores
