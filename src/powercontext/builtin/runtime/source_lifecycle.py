"""Persistent explicit provenance and atomic native Memory state transitions.

Lifecycle revisions are independent of Artifact revisions. A scope transaction
serializes graph mutations and commits the event together with every native
state/index write, so an interrupted event is safely replayable unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_JOURNAL_HEADS_TABLE, SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.relational import RelationalContexts


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event or requested use violates its contract."""


class SourceLifecycleEvent(BaseModel):
    """An ordered availability event for one registered Source version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(strict=True)
    revision: int = Field(strict=True, gt=0)
    sequence: int = Field(strict=True, gt=0)
    available: bool = Field(strict=True)
    reason: str = Field(strict=True)

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")  # noqa: TRY003
        return value


class SourceLifecycleApplication:
    """Bind the lifecycle to the normal Runtime database and Artifact services."""

    def __init__(self, contexts: RelationalContexts) -> None:
        self._contexts = contexts

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._contexts, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    def __init__(self, contexts: RelationalContexts, scope_id: str) -> None:
        self._contexts = contexts
        self.scope_id = scope_id

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        await self._contexts.scopes.get(self.scope_id)
        async with self._contexts.database.transaction() as connection:
            # A scope's journal head exists from normal scope creation. Acquire
            # the same write lock on SQLite and MySQL before reading the graph.
            heads = SOURCE_JOURNAL_HEADS_TABLE
            await connection.execute(
                update(heads).where(heads.c.scope_id == self.scope_id).values(position=heads.c.position)
            )
            yield connection

    async def _nodes(self, connection: AsyncConnection) -> dict[str, dict[str, Any]]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id)
            )
        ).mappings()
        return {str(row["node_id"]): {**row, "spec": json.loads(row["registration"])} for row in rows}

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1 or not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("invalid-source-registration")
        await self._register(node_id, "source", source_ref, (), "all", revision)

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Literal["all", "any"] = "all"
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid-memory-citation")
        await self._register(node_id, "memory", citation, parents, policy, None)

    async def register_topic(
        self, node_id: str, reference: ArtifactRef, *, parents: tuple[str, ...], policy: Literal["all", "any"] = "all"
    ) -> None:
        if not isinstance(reference, ArtifactRef) or reference.family != "topic-memory":
            raise SourceLifecycleError("invalid-topic-reference")
        await self._register(node_id, "topic", reference, parents, policy, None)

    async def _register(
        self,
        node_id: str,
        kind: str,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str,
        revision: int | None,
    ) -> None:
        if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 255:
            raise SourceLifecycleError("invalid-node-identity")
        if (
            policy not in {"all", "any"}
            or isinstance(parents, str)
            or any(not isinstance(parent, str) or not parent.strip() for parent in parents)
        ):
            raise SourceLifecycleError("invalid-dependency-policy")
        if kind != "source" and (not parents or len(set(parents)) != len(parents) or node_id in parents):
            raise SourceLifecycleError("dependencies-must-be-nonempty-distinct-and-acyclic")
        spec = {
            "reference": reference.model_dump(mode="json"),
            "parents": sorted(parents),
            "policy": policy,
            "revision": revision,
        }
        async with self._transaction() as connection:
            nodes = await self._nodes(connection)
            existing = nodes.get(node_id)
            if existing is not None:
                if existing["kind"] != kind or existing["spec"] != spec:
                    raise SourceLifecycleError("node-registration-conflicts-with-its-immutable-binding")
                return
            if any(parent not in nodes for parent in parents):
                raise SourceLifecycleError("unknown-parent-node")
            await self._validate_reference(connection, kind, reference)
            support = [bool(nodes[parent]["available"]) for parent in parents]
            available = True if kind == "source" else all(support) if policy == "all" else any(support)
            ref = reference.memory_ref if isinstance(reference, MemoryCitation) else reference
            await connection.execute(
                insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                    scope_id=self.scope_id,
                    node_id=node_id,
                    kind=kind,
                    registration=_json(spec),
                    available=available,
                    sequence=0,
                    family=None if isinstance(ref, SourceRef) else ref.family,
                    artifact_id=None if isinstance(ref, SourceRef) else ref.artifact_id,
                    artifact_revision=None if isinstance(ref, SourceRef) else ref.revision,
                )
            )
            # Registration and initial native state are one transaction too.
            nodes[node_id] = {"kind": kind, "spec": spec, "available": available}
            await self._sync_memory(connection, nodes)

    async def _validate_reference(
        self, connection: AsyncConnection, kind: str, reference: SourceRef | MemoryCitation | ArtifactRef
    ) -> None:
        try:
            if isinstance(reference, SourceRef):
                await self._contexts.repositories.sources.get(connection, self.scope_id, reference)
            elif isinstance(reference, MemoryCitation):
                services = self._contexts._services_for(self.scope_id)
                _, catalog = services.sources(connection)
                await services.memory(catalog, connection).validate_citation(reference)
            else:
                await self._contexts.repositories.artifacts.get(connection, self.scope_id, reference)
        except (ValueError, LookupError) as error:
            raise SourceLifecycleError("reference-does-not-exist-in-this-scope") from error

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("expected-a-validated-lifecycle-event")
        async with self._transaction() as connection:
            nodes = await self._nodes(connection)
            source = nodes.get(event.node_id)
            if source is None or source["kind"] != "source" or source["spec"]["revision"] != event.revision:
                raise SourceLifecycleError("unknown-source-or-lifecycle-revision-mismatch")
            payload = _json(event.model_dump(mode="json"))
            if event.sequence < source["sequence"] or (
                event.sequence == source["sequence"] and source["event"] != payload
            ):
                raise SourceLifecycleError("stale-or-conflicting-event-sequence")
            # Equal successful events are idempotent. Native state comparison
            # below also repairs externally interrupted or altered projections.
            source["available"] = event.available
            targets = self._availability(nodes)
            await self._sync_memory(connection, nodes, targets)
            table = SOURCE_LIFECYCLE_NODES_TABLE
            for node_id, available in targets.items():
                if bool(nodes[node_id]["available"]) != available or node_id == event.node_id:
                    values: dict[str, Any] = {"available": available}
                    if node_id == event.node_id:
                        values.update(sequence=event.sequence, event=payload)
                    await connection.execute(
                        update(table)
                        .where(table.c.scope_id == self.scope_id, table.c.node_id == node_id)
                        .values(**values)
                    )
            return sorted(node for node, available in targets.items() if available)

    @staticmethod
    def _availability(nodes: dict[str, dict[str, Any]]) -> dict[str, bool]:
        active = {key: bool(row["available"]) for key, row in nodes.items() if row["kind"] == "source"}
        pending = set(nodes) - set(active)
        while pending:
            ready = sorted(
                node for node in pending if all(parent in active for parent in nodes[node]["spec"]["parents"])
            )
            if not ready:
                raise SourceLifecycleError("cyclic-persisted-dependency-graph")
            for node in ready:
                spec = nodes[node]["spec"]
                support = [active[parent] for parent in spec["parents"]]
                active[node] = all(support) if spec["policy"] == "all" else any(support)
                pending.remove(node)
        return active

    async def _sync_memory(
        self, connection: AsyncConnection, nodes: dict[str, dict[str, Any]], targets: dict[str, bool] | None = None
    ) -> None:
        services = self._contexts._services_for(self.scope_id)
        _, catalog = services.sources(connection)
        service = services.memory(catalog, connection)
        memory_nodes = [(node, row) for node, row in nodes.items() if row["kind"] == "memory"]
        if not memory_nodes:
            return
        current = await service.head(services.memory_artifact_id)
        entries = {entry.entry_id: entry for entry in await service.entries(current)}
        states = {item.entry_id: item.state for item in current.content.manifest.entries}
        for available in (False, True):
            changes = []
            for node, row in memory_nodes:
                citation = MemoryCitation.model_validate(row["spec"]["reference"])
                entry = entries.get(citation.entry_id)
                target = bool(row["available"]) if targets is None else targets[node]
                if entry is None or entry.entry_version_id != citation.entry_version_id:
                    raise SourceLifecycleError("registered-memory-entry-version-is-no-longer-current")
                if target == available and (states[citation.entry_id] == "active") != available:
                    changes.append(entry)
            if changes:
                operation = service.reactivate if available else service.forget
                current = await operation(current, entries=tuple(changes), reason="source lifecycle")

    async def resolve(
        self, node_id: str, *, purpose: Literal["generation", "audit"] = "generation"
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown-resolution-purpose")
        async with self._transaction() as connection:
            row = (await self._nodes(connection)).get(node_id)
            if row is None or (purpose == "generation" and not row["available"]):
                raise SourceLifecycleError("node-is-unknown-or-unavailable-for-generation")
            reference = row["spec"]["reference"]
            model = {"source": SourceRef, "memory": MemoryCitation, "topic": ArtifactRef}[row["kind"]]
            return model.model_validate(reference)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
