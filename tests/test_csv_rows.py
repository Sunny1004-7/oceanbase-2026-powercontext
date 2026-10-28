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

from urllib.parse import quote

import pytest

from powercontext.sources.csv_rows import (
    CSVDocumentInput,
    CSVRowInput,
    csv_row_source_id,
    parse_csv_document,
)


def test_encoded_multibyte_identity_accepts_exact_limit_and_rejects_one_over() -> None:
    document_id = "界" * 27 + "x" * 7
    expected = f"csv:{quote(document_id, safe='')}:k"
    assert len(expected) == 256
    assert csv_row_source_id(document_id, "k") == expected
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, "k ")
    assert len(csv_row_source_id("d", "界" * 27 + "x" * 7)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", "界" * 27 + "x" * 8)


def test_spaced_headers_and_keys_remain_distinct_with_lossless_values() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="control:港口",
            primary_key=" key ",
            content='key, key ,note,empty,code\r\nother, k ,"甲\r\n乙 ""引号""",,0007\r\nother,k ,, ,0008\r\n'.encode(),
        )
    )
    assert [row.primary_key_value for row in rows] == [" k ", "k "]
    assert rows[0].columns == ("key", " key ", "note", "empty", "code")
    assert rows[0].values == ("other", " k ", '甲\r\n乙 "引号"', "", "0007")
    assert rows[1].values == ("other", "k ", "", " ", "0008")
    assert csv_row_source_id(rows[0].document_id, rows[0].primary_key_value) != csv_row_source_id(
        rows[1].document_id,
        rows[1].primary_key_value,
    )
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="key", content=b" key ,name\nk,n\n"))


@pytest.mark.parametrize(
    "bad_tail",
    [
        b"one,duplicate\n",
        b"two\n",
        b"two,too,many\n",
        b" \t,blank\n",
        b'"",empty\n',
        b'two,"unclosed\n',
        b'two,"quoted"invalid\n',
        b"two,\xff\n",
    ],
)
def test_invalid_final_record_rejects_the_complete_document(bad_tail: bytes) -> None:
    value = CSVDocumentInput(document_id="atomic", primary_key="id", content=b"id,note\none,valid\n" + bad_tail)
    with pytest.raises(ValueError):
        parse_csv_document(value)


@pytest.mark.parametrize("header", [b"id,id\n", b"id,,note\n", b"id, \t\n", b"\n"])
def test_invalid_headers_are_rejected(header: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=header))


def test_only_empty_records_are_ignored_and_standard_unquoted_quotes_are_accepted() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(document_id="doc", primary_key="id", content=b'id,note\r\n\r\none,a"b\r\n')
    )
    assert rows[0].values == ("one", 'a"b')
    assert parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\n\n")) == ()
    for data in (b'id\n""\n', b"id\n \n"):
        with pytest.raises(ValueError):
            parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=data))


def test_only_initial_bom_is_consumed() -> None:
    row = parse_csv_document(
        CSVDocumentInput(
            document_id="doc",
            primary_key="id",
            content="\ufeffid,note\n1,\ufeffkeep\n".encode(),
        )
    )[0]
    assert row.values == ("1", "\ufeffkeep")


@pytest.mark.parametrize(
    "changes",
    [
        {"columns": ("id", "id")},
        {"columns": ("id", " ")},
        {"values": ("k",)},
        {"primary_key": " id "},
        {"primary_key_value": "different"},
        {"primary_key_value": " ", "values": (" ", "value")},
        {"document_id": " doc"},
        {"document_id": "界" * 29},
    ],
)
def test_direct_row_input_obeys_the_same_invariants(changes: dict[str, object]) -> None:
    fields = {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "k",
        "columns": ("id", "note"),
        "values": ("k", "value"),
    }
    with pytest.raises(ValueError):
        CSVRowInput(**(fields | changes))
