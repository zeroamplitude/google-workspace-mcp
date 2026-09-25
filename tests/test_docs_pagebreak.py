"""Page and section breaks: the `<!-- pagebreak -->` / `<!-- sectionbreak -->`
markers in the Markdown compiler, and docs_insert's insertPageBreak /
insertSectionBreak segment handling, without touching Google."""

from __future__ import annotations

from docbuilder import make_doc
from test_docs_tables import _SeqFakeDocs

from google_workspace_mcp import docs_markdown, server

# ─── split_markdown_tables recognizes the marker line ───────────────────


def test_pagebreak_line_is_its_own_segment():
    md = "Before\n\n<!-- pagebreak -->\n\nAfter"
    segs = docs_markdown.split_markdown_tables(md)
    assert segs == [("text", "Before\n"), ("pagebreak", None), ("text", "\nAfter")]


def test_pagebreak_marker_is_whitespace_and_case_tolerant():
    for line in ("<!--pagebreak-->", "  <!-- PageBreak -->  ", "<!--   pagebreak   -->"):
        assert docs_markdown.split_markdown_tables(line) == [("pagebreak", None)]


def test_pagebreak_marker_must_be_alone_on_its_line():
    md = "<!-- pagebreak --> and more"
    assert docs_markdown.split_markdown_tables(md) == [("text", md)]


def test_no_pagebreak_markdown_is_a_single_text_segment():
    assert docs_markdown.split_markdown_tables("just text") == [("text", "just text")]


# ─── docs_insert with a page break ───────────────────────────────────────


def test_insert_pagebreak_alone_sends_one_insertpagebreak_request(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))  # body_end 4; "end" mode at index 3
    svc = _SeqFakeDocs(doc1, doc1)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_insert("personal", "doc-1", "<!-- pagebreak -->")

    assert [n for n, _ in svc.log] == ["get", "batchUpdate", "get"]
    assert svc.batches()[0] == {
        "requests": [{"insertPageBreak": {"location": {"index": 3, "tabId": "t.0"}}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    assert out["inserted_at"] == 3
    assert out["revision_id"] == "rev-2"  # the fake's constant batchUpdate reply


def test_insert_pagebreak_then_text_recomputes_the_next_index_from_a_refetch(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))  # body_end 4
    # What a re-fetch after the page break sees: two paragraphs, the last one empty.
    doc2 = make_doc(("NORMAL_TEXT", "Hi"), ("NORMAL_TEXT", ""), revision="rev-2")
    svc = _SeqFakeDocs(doc1, doc2)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_insert("personal", "doc-1", "<!-- pagebreak -->\n\nPara two")

    assert [n for n, _ in svc.log] == ["get", "batchUpdate", "get", "batchUpdate", "get"]
    batches = svc.batches()
    assert batches[0] == {
        "requests": [{"insertPageBreak": {"location": {"index": 3, "tabId": "t.0"}}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    # The empty last paragraph doc2 shows fills directly, no leading newline.
    assert batches[1]["requests"][0] == {"insertText": {"text": "Para two", "location": {"index": 4, "tabId": "t.0"}}}
    assert batches[1]["writeControl"] == {"requiredRevisionId": "rev-2"}
    assert out["inserted_at"] == 3


def test_replace_section_allows_a_pagebreak_in_the_markdown(monkeypatch):
    doc1 = make_doc(("HEADING_1", "Sec"), ("NORMAL_TEXT", "old"), ("HEADING_1", "Next"))
    deleted = make_doc(("HEADING_1", "Sec"), ("HEADING_1", "Next"), revision="rev-1b")
    svc = _SeqFakeDocs(doc1, deleted, deleted)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_replace_section("personal", "doc-1", "Sec", "<!-- pagebreak -->")

    assert svc.batches()[0] == {
        "requests": [{"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 9, "tabId": "t.0"}}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    assert svc.batches()[1]["requests"] == [{"insertPageBreak": {"location": {"index": 5, "tabId": "t.0"}}}]
    assert out["section"] == "Sec"


# ─── split_markdown_tables recognizes the sectionbreak marker ───────────


def test_sectionbreak_line_is_its_own_segment():
    md = "Before\n\n<!-- sectionbreak -->\n\nAfter"
    segs = docs_markdown.split_markdown_tables(md)
    assert segs == [("text", "Before\n"), ("sectionbreak", "NEXT_PAGE"), ("text", "\nAfter")]


def test_sectionbreak_continuous_variant():
    assert docs_markdown.split_markdown_tables("<!-- sectionbreak continuous -->") == [("sectionbreak", "CONTINUOUS")]


def test_sectionbreak_marker_is_whitespace_and_case_tolerant():
    for line in ("<!--sectionbreak-->", "  <!-- SectionBreak -->  ", "<!--   sectionbreak   -->"):
        assert docs_markdown.split_markdown_tables(line) == [("sectionbreak", "NEXT_PAGE")]
    for line in ("<!--sectionbreak continuous-->", "  <!-- SectionBreak   Continuous -->  "):
        assert docs_markdown.split_markdown_tables(line) == [("sectionbreak", "CONTINUOUS")]


def test_sectionbreak_marker_must_be_alone_on_its_line():
    md = "<!-- sectionbreak --> and more"
    assert docs_markdown.split_markdown_tables(md) == [("text", md)]


# ─── docs_insert with a section break ────────────────────────────────────


def test_insert_sectionbreak_alone_sends_one_insertsectionbreak_request(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))  # body_end 4; "end" mode at index 3
    svc = _SeqFakeDocs(doc1, doc1)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_insert("personal", "doc-1", "<!-- sectionbreak -->")

    assert [n for n, _ in svc.log] == ["get", "batchUpdate", "get"]
    assert svc.batches()[0] == {
        "requests": [{"insertSectionBreak": {"sectionType": "NEXT_PAGE", "location": {"index": 3, "tabId": "t.0"}}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    assert out["inserted_at"] == 3


def test_insert_sectionbreak_continuous_sets_the_section_type(monkeypatch):
    doc1 = make_doc(("NORMAL_TEXT", "Hi"))
    svc = _SeqFakeDocs(doc1, doc1)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    server.docs_insert("personal", "doc-1", "<!-- sectionbreak continuous -->")

    assert svc.batches()[0]["requests"] == [
        {"insertSectionBreak": {"sectionType": "CONTINUOUS", "location": {"index": 3, "tabId": "t.0"}}}
    ]
