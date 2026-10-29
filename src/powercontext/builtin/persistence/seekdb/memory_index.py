"""One native seekdb statement for independently bounded hybrid candidates."""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause
from typing_extensions import override

from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryFTSIndex, OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

_HYBRID_SQL = """
SELECT * FROM (
    SELECT 'fts' AS channel,
           f.memory_artifact_id, f.head_revision, f.entry_id, f.entry_version_id, v.text,
           NULL AS distance, MATCH(f.searchable_text) AGAINST (:query) AS channel_score
    FROM pc_memory_entry_heads AS f
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = f.scope_id
     AND v.memory_artifact_id = f.memory_artifact_id
     AND v.entry_version_id = f.entry_version_id
    WHERE f.scope_id = :scope_id
      AND f.memory_artifact_id IN :memory_artifact_ids
      AND MATCH(f.searchable_text) AGAINST (:query)
    {fts_tags}
    ORDER BY channel_score DESC, f.memory_artifact_id, f.entry_id, f.entry_version_id
    LIMIT :candidate_limit
) AS lexical_candidates
UNION ALL
SELECT * FROM (
    SELECT 'vector' AS channel,
           m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id, v.text,
           l2_distance(m.embedding, :query_vector) AS distance, NULL AS channel_score
    FROM pc_memory_vector_entries AS m
    JOIN pc_memory_entry_versions AS v
      ON v.scope_id = m.scope_id
     AND v.memory_artifact_id = m.memory_artifact_id
     AND v.entry_version_id = m.entry_version_id
    WHERE m.scope_id = :scope_id
      AND m.memory_artifact_id IN :memory_artifact_ids
    {vector_tags}
    ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
    LIMIT :candidate_limit
) AS vector_candidates
ORDER BY channel, channel_score DESC, distance, memory_artifact_id, entry_id, entry_version_id
"""


class SeekDBMemoryHybridIndex(CompositeMemoryIndex):
    """Reuse authoritative projection writes and validation, combining only reads.

    Each channel filters scope/artifact/tags before its own candidate limit. The
    final ordering stabilizes ties outside the ANN query shape, which must keep
    its single distance ORDER BY to retain the native vector index. There is no
    global limit before fusion and no hybrid fallback to two candidate reads.
    """

    def __init__(self, profile: EmbeddingProfile) -> None:
        self._vector = OceanBaseMemoryVectorIndex(profile)
        super().__init__(OceanBaseMemoryFTSIndex(), self._vector)
        self._hybrid_sql = self._statement(profile, tagged=False)
        self._tagged_hybrid_sql = self._statement(profile, tagged=True)

    @staticmethod
    def _statement(profile: EmbeddingProfile, *, tagged: bool) -> TextClause:
        statement = text(
            _HYBRID_SQL.format(
                fts_tags=memory_tag_sql("f") if tagged else "",
                vector_tags=memory_tag_sql("m") if tagged else "",
            )
        ).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )
        if tagged:
            statement = statement.bindparams(
                bindparam("tag_keys", expanding=True), bindparam("tag_hashes", expanding=True)
            )
        return statement

    @override
    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode != "hybrid":
            return await super().search(connection, scope_id, request)
        if request.embedding_profile != self._vector.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, self._vector.profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            (
                await connection.execute(
                    self._hybrid_sql if request.tag_filter is None else self._tagged_hybrid_sql,
                    {
                        "scope_id": scope_id,
                        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                        "query": request.analyzed_query,
                        "query_vector": validate_embedding(
                            request.query_vector, dimension=self._vector.profile.dimension
                        ),
                        "candidate_limit": request.candidate_limit,
                        **memory_tag_parameters(request.tag_filter),
                    },
                )
            )
            .mappings()
            .all()
        )
        # Projection head_revision can legitimately precede the requested head.
        # The shared mapper uses the current refs validated before/after search.
        return MemorySearchChannels(
            fts=memory_channel_hits((row for row in rows if row["channel"] == "fts"), request.memories),
            vector=memory_channel_hits((row for row in rows if row["channel"] == "vector"), request.memories),
        )
