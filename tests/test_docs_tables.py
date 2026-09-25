"""Google Docs tables: markdown parsing, docs_get, docs_table_edit, and the
two-batchUpdate insertTable + fill dance in docs_insert / docs_replace_section
— all without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import make_body, make_doc
from test_docs_editing import _Call, _FakeDocs

from google_workspace_mcp import docs_markdown, docs_model, server


class _SeqFakeDocs(_FakeDocs):
    """Like _FakeDocs, but successive `get`s return successive docs (the last
    one repeating) — the states re-fetches see after a delete or insertTable."""

    def __init__(self, *docs):
        super().__init__(docs[0])
        self.docs = docs
        self.get_calls = 0

    def get(self, **kw):
        resp = self.docs[min(self.get_calls, len(self.docs) - 1)]
        self.get_calls += 1
        return _Call(self.log, "get", resp=resp, **kw)

    def batches(self):
        return [kw["body"] for n, kw in self.log if n == "batchUpdate"]


# ─── markdown table parsing ─────────────────────────────────────────────


def test_split_markdown_tables_pulls_a_table_out_of_surrounding_text():
    md = "Before text\n\n| H1 | H2 |\n|---|---|\n| a | b |\n\nAfter text"
    segs = docs_markdown.split_markdown_tables(md)
    assert [k for k, _ in segs] == ["text", "table", "text"]
    assert segs[1][1] == [["H1", "H2"], ["a", "b"]]
    assert segs[0][1] == "Before text\n"
    assert segs[2][1] == "\nAfter text"


def test_split_markdown_tables_pads_short_rows_and_unescapes_pipes():
    md = "| a | b | c |\n|---|---|---|\n| x\\|y | z |"
    [(_, rows)] = docs_markdown.split_markdown_tables(md)
    assert rows == [["a", "b", "c"], ["x|y", "z", ""]]


def test_split_markdown_tables_ignores_a_non_table_pipe_line():
    md = "a | b\nnot a table"
    segs = docs_markdown.split_markdown_tables(md)
    assert segs == [("text", "a | b\nnot a table")]


def test_no_table_markdown_is_a_single_segment():
    segs = docs_markdown.split_markdown_tables("just text")
    assert segs == [("text", "just text")]


# ─── docs_model.tables ───────────────────────────────────────────────────


def test_tables_reads_rows_columns_and_cell_ranges():
    body = make_body(("HEADING_1", "Data"), ("table", [["H1", "H2"], ["a", "b"]]))
    [t] = docs_model.tables(body)
    assert (t["start_index"], t["end_index"]) == (6, 24)
    assert (t["rows"], t["columns"]) == (2, 2)
    assert t["cells"] == [
        [{"start_index": 9, "end_index": 12, "text": "H1"}, {"start_index": 13, "end_index": 16, "text": "H2"}],
        [{"start_index": 18, "end_index": 20, "text": "a"}, {"start_index": 21, "end_index": 23, "text": "b"}],
    ]


def test_tables_is_empty_without_a_table():
    assert docs_model.tables(make_body(("NORMAL_TEXT", "hi"))) == []


# ─── docs_get includes tables ────────────────────────────────────────────


def test_docs_get_includes_tables(monkeypatch):
    doc = make_doc(("HEADING_1", "Data"), ("table", [["H1", "H2"], ["a", "b"]]))
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    out = server.docs_get("personal", "doc-1")
    assert out["tables"] == docs_model.tables(doc["tabs"][0]["documentTab"]["body"])


# ─── docs_insert with a markdown table ───────────────────────────────────


def test_insert_table_at_end_inserts_a_newline_then_fills_cells_in_reverse(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))
    doc2 = make_doc(("NORMAL_TEXT", "Hi"), ("table", [["", ""], ["", ""]]), revision="rev-2")
    svc = _SeqFakeDocs(doc1, doc2)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    md = "| H1 | H2 |\n|---|---|\n| a | b |"
    out = server.docs_insert("personal", "doc-1", md)

    assert [n for n, _ in svc.log] == ["get", "batchUpdate", "get", "batchUpdate", "get"]  # final get: where the next segment would go
    batches = svc.batches()
    assert batches[0] == {
        "requests": [
            {"insertText": {"text": "\n", "location": {"index": 3, "tabId": "t.0"}}},
            {"insertTable": {"rows": 2, "columns": 2, "location": {"index": 4, "tabId": "t.0"}}},
        ],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    # Cells fill in reverse document order (bottom-right first) and the
    # header row (H1/H2) gets an extra bold updateTextStyle.
    assert batches[1] == {
        "requests": [
            {"insertText": {"text": "b", "location": {"index": 14, "tabId": "t.0"}}},
            {"insertText": {"text": "a", "location": {"index": 12, "tabId": "t.0"}}},
            {"insertText": {"text": "H2", "location": {"index": 9, "tabId": "t.0"}}},
            {"updateTextStyle": {
                "range": {"startIndex": 9, "endIndex": 11, "tabId": "t.0"},
                "textStyle": {"bold": True}, "fields": "bold",
            }},
            {"insertText": {"text": "H1", "location": {"index": 7, "tabId": "t.0"}}},
            {"updateTextStyle": {
                "range": {"startIndex": 7, "endIndex": 9, "tabId": "t.0"},
                "textStyle": {"bold": True}, "fields": "bold",
            }},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    assert out["inserted_at"] == 3
    assert out["revision_id"] == "rev-2"


def test_insert_table_at_a_paragraph_start_needs_no_leading_newline(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))
    doc2 = make_doc(("table", [[""]]), ("NORMAL_TEXT", "Hi"), revision="rev-2")
    svc = _SeqFakeDocs(doc1, doc2)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    server.docs_insert("personal", "doc-1", "| x |\n|---|", at="start")
    batches = svc.batches()
    assert batches[0]["requests"] == [
        {"insertTable": {"rows": 1, "columns": 1, "location": {"index": 1, "tabId": "t.0"}}},
    ]
    assert batches[1]["requests"] == [
        {"insertText": {"text": "x", "location": {"index": 4, "tabId": "t.0"}}},
        {"updateTextStyle": {
            "range": {"startIndex": 4, "endIndex": 5, "tabId": "t.0"},
            "textStyle": {"bold": True}, "fields": "bold",
        }},
    ]


def test_insert_table_supports_inline_markdown_in_cells(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))
    doc2 = make_doc(("table", [[""]]), ("NORMAL_TEXT", "Hi"), revision="rev-2")
    svc = _SeqFakeDocs(doc1, doc2)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    server.docs_insert("personal", "doc-1", "| **bold** |\n|---|", at="start")
    fill = svc.batches()[1]["requests"]
    assert fill[0] == {"insertText": {"text": "bold", "location": {"index": 4, "tabId": "t.0"}}}
    styles = [r["updateTextStyle"] for r in fill[1:]]
    assert {"startIndex": 4, "endIndex": 8, "tabId": "t.0"} in [s["range"] for s in styles]


# ─── docs_replace_section allows a table in the new markdown ────────────


def test_replace_section_inserts_a_table_in_two_batches_after_the_delete(monkeypatch):
    doc1 = make_doc(("HEADING_1", "Sec"), ("NORMAL_TEXT", "old"), ("HEADING_1", "Next"))
    deleted = make_doc(("HEADING_1", "Sec"), ("HEADING_1", "Next"), revision="rev-1b")
    doc2 = make_doc(("HEADING_1", "Sec"), ("table", [[""]]), ("HEADING_1", "Next"), revision="rev-2")
    svc = _SeqFakeDocs(doc1, deleted, doc2)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_replace_section("personal", "doc-1", "Sec", "| x |\n|---|")

    # The re-read after the delete is what the insert measures from (a stale,
    # pre-delete read put the table inside earlier text, found live).
    assert [n for n, _ in svc.log] == ["get", "batchUpdate", "get", "batchUpdate", "get", "batchUpdate", "get"]
    batches = svc.batches()
    assert batches[0] == {
        "requests": [{"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 9, "tabId": "t.0"}}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    assert batches[1]["requests"] == [
        {"insertTable": {"rows": 1, "columns": 1, "location": {"index": 5, "tabId": "t.0"}}},
    ]
    assert batches[2]["requests"][0] == {"insertText": {"text": "x", "location": {"index": 8, "tabId": "t.0"}}}
    assert out["replaced_range"] == [5, 9]
    assert out["section"] == "Sec"


def test_replace_section_without_a_table_in_the_markdown_stays_one_batch(monkeypatch):
    # Guards the fast path: no regression from the table plumbing when
    # there's no table involved at all.
    svc = _FakeDocs(make_doc(("HEADING_1", "Sec"), ("NORMAL_TEXT", "old"), ("HEADING_1", "Next")))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    server.docs_replace_section("personal", "doc-1", "Sec", "new text")
    assert [n for n, _ in svc.log] == ["get", "batchUpdate"]


# ─── docs_table_edit ──────────────────────────────────────────────────────


@pytest.fixture
def table_fake(monkeypatch):
    doc = make_doc(("HEADING_1", "Data"), ("table", [["H1", "H2"], ["a", "b"]]))
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def test_table_edit_insert_row(table_fake):
    out = server.docs_table_edit("personal", "doc-1", 0, "insert_row", row=0, below=True)
    assert table_fake.sent["requests"] == [{"insertTableRow": {
        "tableCellLocation": {"tableStartLocation": {"index": 6, "tabId": "t.0"}, "rowIndex": 0, "columnIndex": 0},
        "insertBelow": True,
    }}]
    assert out["table_index"] == 0 and out["action"] == "insert_row"


def test_table_edit_insert_column_left(table_fake):
    server.docs_table_edit("personal", "doc-1", 0, "insert_column", row=0, column=1, right=False)
    assert table_fake.sent["requests"] == [{"insertTableColumn": {
        "tableCellLocation": {"tableStartLocation": {"index": 6, "tabId": "t.0"}, "rowIndex": 0, "columnIndex": 1},
        "insertRight": False,
    }}]


def test_table_edit_delete_row(table_fake):
    server.docs_table_edit("personal", "doc-1", 0, "delete_row", row=1)
    assert table_fake.sent["requests"] == [{"deleteTableRow": {
        "tableCellLocation": {"tableStartLocation": {"index": 6, "tabId": "t.0"}, "rowIndex": 1, "columnIndex": 0},
    }}]


def test_table_edit_delete_column(table_fake):
    server.docs_table_edit("personal", "doc-1", 0, "delete_column", row=0, column=1)
    assert table_fake.sent["requests"] == [{"deleteTableColumn": {
        "tableCellLocation": {"tableStartLocation": {"index": 6, "tabId": "t.0"}, "rowIndex": 0, "columnIndex": 1},
    }}]


def test_table_edit_set_cell_clears_then_inserts(table_fake):
    server.docs_table_edit("personal", "doc-1", 0, "set_cell", row=1, column=0, text="**x**")
    reqs = table_fake.sent["requests"]
    assert reqs[0] == {"deleteContentRange": {"range": {"startIndex": 18, "endIndex": 19, "tabId": "t.0"}}}
    assert reqs[1] == {"insertText": {"text": "x", "location": {"index": 18, "tabId": "t.0"}}}
    assert reqs[2] == {"updateTextStyle": {
        "range": {"startIndex": 18, "endIndex": 19, "tabId": "t.0"},
        "textStyle": {"bold": True}, "fields": "bold",
    }}


def test_table_edit_set_cell_needs_text(table_fake):
    with pytest.raises(ValueError, match="needs `text`"):
        server.docs_table_edit("personal", "doc-1", 0, "set_cell", row=0, column=0)


def test_table_edit_validates_table_row_and_column(table_fake):
    with pytest.raises(ValueError, match="No table at table_index"):
        server.docs_table_edit("personal", "doc-1", 1, "delete_row", row=0)
    with pytest.raises(ValueError, match="row must be between 0 and 1"):
        server.docs_table_edit("personal", "doc-1", 0, "delete_row", row=5)
    with pytest.raises(ValueError, match="column must be between 0 and 1"):
        server.docs_table_edit("personal", "doc-1", 0, "set_cell", row=0, column=9, text="x")
    assert [n for n, _ in table_fake.log] == ["get", "get", "get"]  # never reached batchUpdate


def test_the_empty_paragraph_insert_table_leaves_above_is_folded_away(monkeypatch):
    before = make_doc(("HEADING_1", "Sec"), ("NORMAL_TEXT", "next"))  # Sec 1..5, next 5..10
    # What a re-fetch sees after insertTable at 5: an empty paragraph, then the table.
    after = make_doc(("HEADING_1", "Sec"), ("NORMAL_TEXT", ""), ("table", [[""]]), ("NORMAL_TEXT", "next"))
    svc = _SeqFakeDocs(before, after)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    server.docs_insert("personal", "doc-1", "| x |\n|---|", at="index", index=5)
    # Deletes the heading's newline (4..5), not the one right before the table (5..6).
    assert svc.batches()[-1]["requests"] == [
        {"deleteContentRange": {"range": {"startIndex": 4, "endIndex": 5, "tabId": "t.0"}}}
    ]


def test_no_fold_when_the_gap_follows_another_table(monkeypatch):
    before = make_doc(("table", [["a"]]), ("NORMAL_TEXT", "next"))
    after = make_doc(("table", [["a"]]), ("NORMAL_TEXT", ""), ("table", [[""]]), ("NORMAL_TEXT", "next"))
    svc = _SeqFakeDocs(before, after)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    t0_end = server.docs_get("personal", "doc-1")["tables"][0]["end_index"]
    svc.get_calls = 0
    server.docs_insert("personal", "doc-1", "| x |\n|---|", at="index", index=t0_end)
    assert not any("deleteContentRange" in r for b in svc.batches() for r in b["requests"])
