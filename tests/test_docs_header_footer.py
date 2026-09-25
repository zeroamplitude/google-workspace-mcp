"""docs_header_footer: create/replace/delete a Google Doc's default header or
footer, without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import make_body, make_doc
from test_docs_editing import _FakeDocs

from google_workspace_mcp import server


def _header_content(text: str) -> list[dict]:
    """A header/footer segment's raw `content`: one paragraph, indices
    starting at 0 (no leading sectionBreak, unlike a tab body)."""
    n = len(text) + 1  # ascii text + the paragraph's own newline
    return [{
        "startIndex": 0,
        "endIndex": n,
        "paragraph": {
            "elements": [{"startIndex": 0, "textRun": {"content": text + "\n"}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }]


def _doc_with_header(kind: str, hf_id: str, content_text: str | None) -> dict:
    style_field = "defaultHeaderId" if kind == "header" else "defaultFooterId"
    map_field = "headers" if kind == "header" else "footers"
    id_field = "headerId" if kind == "header" else "footerId"
    tab: dict = {"body": make_body(("NORMAL_TEXT", "Hi")), "documentStyle": {style_field: hf_id}}
    if content_text is not None:
        tab[map_field] = {hf_id: {id_field: hf_id, "content": _header_content(content_text)}}
    return {
        "documentId": "doc-1", "title": "Doc", "revisionId": "rev-1",
        "tabs": [{"tabProperties": {"tabId": "t.0", "title": "T.0"}, "documentTab": tab}],
    }


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(("NORMAL_TEXT", "Hi")))  # no header/footer yet
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def test_set_needs_text(fake):
    with pytest.raises(ValueError, match="needs `text`"):
        server.docs_header_footer("personal", "doc-1", "header", "set")


def test_set_creates_a_header_when_the_tab_has_none(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createHeader": {"headerId": "hdr-1"}}],
    }
    out = server.docs_header_footer("personal", "doc-1", "header", "set", text="Company Inc.")

    assert [n for n, _ in fake.log] == ["get", "batchUpdate", "batchUpdate"]
    create_call = fake.log[1][1]["body"]
    assert create_call["requests"] == [{"createHeader": {"type": "DEFAULT", "sectionBreakLocation": {"tabId": "t.0"}}}]
    assert create_call["writeControl"] == {"requiredRevisionId": "rev-1"}

    fill_call = fake.log[2][1]["body"]
    assert fill_call["requests"][0] == {
        "insertText": {"text": "Company Inc.", "location": {"index": 0, "segmentId": "hdr-1", "tabId": "t.0"}}
    }
    assert fill_call["writeControl"] == {"requiredRevisionId": "rev-2"}
    assert out["kind"] == "header" and out["action"] == "set" and out["header_id"] == "hdr-1"


def test_footer_uses_the_footer_request_and_response_names(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createFooter": {"footerId": "ftr-1"}}],
    }
    out = server.docs_header_footer("personal", "doc-1", "footer", "set", text="Page footer")

    create_call = fake.log[1][1]["body"]
    assert create_call["requests"] == [{"createFooter": {"type": "DEFAULT", "sectionBreakLocation": {"tabId": "t.0"}}}]
    assert out["footer_id"] == "ftr-1"


def test_set_replaces_an_existing_headers_content_in_one_batch(monkeypatch):
    doc = _doc_with_header("header", "hdr-1", "Old header")
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_header_footer("personal", "doc-1", "header", "set", text="New header")

    assert [n for n, _ in svc.log] == ["get", "batchUpdate"]
    reqs = svc.sent["requests"]
    assert reqs[0] == {
        "deleteContentRange": {"range": {"startIndex": 0, "endIndex": 10, "segmentId": "hdr-1", "tabId": "t.0"}}
    }
    assert reqs[1] == {
        "insertText": {"text": "New header", "location": {"index": 0, "segmentId": "hdr-1", "tabId": "t.0"}}
    }
    assert out["header_id"] == "hdr-1"


def test_delete_removes_the_header(monkeypatch):
    doc = _doc_with_header("header", "hdr-1", "Old header")
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    server.docs_header_footer("personal", "doc-1", "header", "delete")
    assert svc.sent["requests"] == [{"deleteHeader": {"headerId": "hdr-1", "tabId": "t.0"}}]


def test_delete_needs_an_existing_header(fake):
    with pytest.raises(ValueError, match="has no header to delete"):
        server.docs_header_footer("personal", "doc-1", "header", "delete")
    assert [n for n, _ in fake.log] == ["get"]  # never reached batchUpdate
