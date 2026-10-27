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

"""Registered Atomic Memory metadata survives managed vector profile selection."""

import json
import sqlite3
from contextlib import closing

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.persistence.migrations.deployment import server_bundle
from powercontext.builtin.persistence.migrations.models import MigrationError
from powercontext.builtin.persistence.migrations.mysql_schema import MySQLSchema, _parse_table
from powercontext.builtin.persistence.migrations.sqlite import SQLiteMigrationRunner
from powercontext.builtin.runtime.config import BuiltinConfig
from powercontext.builtin.runtime.processing_registry import canonical_processing_manifest


@pytest.mark.parametrize("source_release", [None, "1.4.0"])
def test_managed_sqlite_vector_schema_includes_registered_nullable_description(tmp_path, source_release):
    profile = EmbeddingProfile(profile_id="description-space", model="fixture", dimension=3)
    bundle = server_bundle(embedding_profile=profile, source_release=source_release)
    configuration = {
        "embedding_enabled": True,
        "embedding_profile": profile.model_dump(mode="json"),
        "processing_manifest": canonical_processing_manifest(BuiltinConfig()),
    }
    database = tmp_path / "vector.db"
    runner = SQLiteMigrationRunner(database, bundle, configuration=configuration)
    result = runner.apply(plan_id=runner.plan().plan_id, accepted=True, maintenance_confirmed=True)
    assert result.revision == "v1_4_1_004"
    with closing(sqlite3.connect(database)) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(pc_atomic_memory_current)")}
        assert columns["description"][2:4] == ("VARCHAR(200)", 0)
    assert runner.verify().state == "ready"


def test_mysql_vector_admission_retains_description_and_exact_previous_revision():
    profile = EmbeddingProfile(profile_id="description-space", model="fixture", dimension=3)
    plain = server_bundle()
    vector = server_bundle(embedding_profile=profile)
    assert vector.vector_profile is not None
    inventory = {"tables": {}, "views": [], "triggers": []}
    for resource in json.loads((plain.directory / "manifest.json").read_text())["mysql_state_layouts"]:
        for entry in json.loads((plain.directory / resource).read_text())["revisions"].values():
            for change in entry["changes"]:
                for name, table in change.items():
                    if table is None:
                        del inventory["tables"][name]
                    else:
                        inventory["tables"][name] = table
    for name, sql in vector.vector_profile.mysql_tables().items():
        if name == "pc_atomic_memory_current":
            sql = sql.replace(
                "  PRIMARY KEY",
                "  `description` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL,\n  PRIMARY KEY",
            )
        inventory["tables"][name] = _parse_table(sql)[1]
    declaration = vector.vector_profile.layout["mysql_profile_table"]
    name, table = _parse_table(declaration)
    inventory["tables"][name] = table
    schema = MySQLSchema(vector)
    schema.verify(inventory, "v1_4_1_004")
    assert schema.identify(inventory) == "v1_4_1_004"
    description = inventory["tables"]["pc_atomic_memory_current"]["columns"].pop("description")
    schema.verify(inventory, "s1402")
    recovery = schema.recovery(inventory, "s1402")
    assert recovery.revision == "v1_4_1_004" and len(recovery.pending_statements) == 1
    with pytest.raises(MigrationError, match="incompatible_schema"):
        schema.verify(inventory, "v1_4_1_004")
    inventory["tables"]["pc_atomic_memory_current"]["columns"]["description"] = description
    description["nullable"] = False
    with pytest.raises(MigrationError, match="incompatible_schema"):
        schema.verify(inventory, "v1_4_1_004")
