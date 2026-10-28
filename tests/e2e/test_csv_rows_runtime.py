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

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    parse_csv_document,
)


@pytest.mark.asyncio
async def test_independent_rows_persist_across_restarts_and_resolve_exact_citations(tmp_path: Path) -> None:
    columns = ("key", " key ", "note", "empty", "code")
    records = [
        ("different-key", " 01 ", 'quartzharbor 舟山\r\n检修 "双引号"', "", "0007"),
        ("different-key", "01 ", "amberstation 延期", " ", "0008"),
        ("different-key", "01", "cedararchive 完成", "", "0009"),
    ]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(records)
    document_id = "港口:巡检/2026"
    rows = parse_csv_document(
        CSVDocumentInput(document_id=document_id, primary_key=" key ", content=output.getvalue().encode())
    )
    expected = {}
    for record in records:
        mapping = dict(zip(columns, record, strict=True))
        source_id = f"csv:{quote(document_id, safe='')}:{quote(record[1], safe='')}"
        expected[source_id] = TextEvidence(
            source_type="csv-row",
            source_id=source_id,
            content=json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": document_id,
                "primary_key": " key ",
                "primary_key_value": record[1],
                "columns": list(columns),
                "row": mapping,
            },
        )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    database = tmp_path / "independent-rows.db"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))

    class RowPipeline:
        async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
            return tuple(
                MemoryEntryInput(
                    kind="csv-row", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async with open_builtin_runtime(config) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title="港口巡检", summary="independent CSV", idempotency_key="csv-independent")
        )
        await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
        for row in rows:
            source = await registry.resolve(row)
            assert await registry.read(source) == row
            await runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=scope.scope_id, observation=project_source_for_transport(registry, source)
                )
            )

    assert database.is_file() and database.stat().st_size > 0
    citations = {}
    async with open_builtin_runtime(config, candidate_pipeline=RowPipeline()) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        processed = 0
        while True:
            flushed = await memory.flush()
            if not flushed.processed:
                break
            processed += flushed.source_count
        assert processed == len(records)
        for record in records:
            source_id = f"csv:{quote(document_id, safe='')}:{quote(record[1], safe='')}"
            query = record[2].split()[0]
            search = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=20))
            for hit in search.hits:
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                )
                entry = await memory.get(GetMemoryEntryRequest(citation=citation))
                if any(source.source_id == source_id for source in entry.entry.sources):
                    assert entry.entry.text == expected[source_id].content
                    citations[source_id] = citation
                    break
        assert set(citations) == set(expected)

    async with open_builtin_contexts(config) as contexts:
        entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
        actual = {
            entry.source_ref.source_id: TextEvidence.model_validate(
                entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY)
            )
            for entry in entries
        }
        assert actual == expected

    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for source_id, citation in citations.items():
            entry = await memory.get(GetMemoryEntryRequest(citation=citation))
            assert entry.entry.text == expected[source_id].content
            assert [source.source_id for source in entry.entry.sources] == [source_id]

    (tmp_path / "csv-runtime-audit.json").write_text(
        json.dumps(
            {
                "backend": "SQLite BuiltinRuntime",
                "database": str(database),
                "scope_id": scope.scope_id,
                "row_count": len(records),
                "persisted_projections": {
                    source_id: projection.model_dump(mode="json") for source_id, projection in actual.items()
                },
                "exact_citations": {
                    source_id: citation.model_dump(mode="json") for source_id, citation in citations.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
