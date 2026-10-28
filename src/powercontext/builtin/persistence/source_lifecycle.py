"""Shared predicates for the current generation eligibility of Topic revisions."""

from __future__ import annotations

from sqlalchemy import Table, select
from sqlalchemy.sql import ColumnElement

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE


def available_topic(table: Table) -> ColumnElement[bool]:
    """Filter before ranking/limiting; immutable historical reads stay untouched."""
    nodes = SOURCE_LIFECYCLE_NODES_TABLE
    return (
        ~select(nodes.c.node_id)
        .where(
            nodes.c.scope_id == table.c.scope_id,
            nodes.c.family == "topic-memory",
            nodes.c.artifact_id == table.c.artifact_id,
            nodes.c.artifact_revision == table.c.revision,
            nodes.c.available.is_(False),
        )
        .correlate(table)
        .exists()
    )
