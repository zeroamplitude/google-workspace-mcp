"""docs_footnote: createFootnote + filling its segment, without touching
Google."""

from __future__ import annotations

import pytest
from docbuilder import make_doc
from test_docs_editing import _FakeDocs

from google_workspace_mcp import server


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(("NORMAL_TEXT", "Hi")))  # body_end 4
    svc.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createFootnote": {"footnoteId": "fn-1"}}],
    }
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def test_needs_text(fake):
    with pytest.raises(ValueError, match="required"):
        server.docs_footnote("personal", "doc-1", "")


def test_at_end_creates_then_fills_the_footnote_segment(fake):
    out = server.docs_footnote("personal", "doc-1", "See appendix.")

    assert [n for n, _ in fake.log] == ["get", "batchUpdate", "batchUpdate"]
    create_call = fake.log[1][1]["body"]
    assert create_call["requests"] == [{"createFootnote": {"location": {"index": 3, "tabId": "t.0"}}}]

    fill_call = fake.log[2][1]["body"]
    assert fill_call["requests"][0] == {
        "insertText": {"text": "See appendix.", "location": {"index": 0, "segmentId": "fn-1", "tabId": "t.0"}}
    }
    assert out["footnote_id"] == "fn-1"
    assert out["inserted_at"] == 3


def test_at_start_and_index_place_the_reference_like_docs_insert(fake):
    server.docs_footnote("personal", "doc-1", "Note.", at="start")
    assert fake.log[1][1]["body"]["requests"][0]["createFootnote"]["location"]["index"] == 1

    server.docs_footnote("personal", "doc-1", "Note.", at="index", index=2)
    assert fake.log[4][1]["body"]["requests"][0]["createFootnote"]["location"]["index"] == 2


def test_after_text_places_the_reference_right_after_the_quote(monkeypatch):
    doc = make_doc(("NORMAL_TEXT", "Hello world"))  # "Hello"(1..6) " world\n"
    svc = _FakeDocs(doc)
    svc.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createFootnote": {"footnoteId": "fn-1"}}],
    }
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_footnote("personal", "doc-1", "Note.", after_text="Hello")
    assert svc.log[1][1]["body"]["requests"][0]["createFootnote"]["location"]["index"] == 6
    assert out["inserted_at"] == 6


def test_after_text_not_found_is_refused(fake):
    with pytest.raises(ValueError, match="does not appear"):
        server.docs_footnote("personal", "doc-1", "Note.", after_text="nonexistent")
    assert [n for n, _ in fake.log] == ["get"]  # never reached batchUpdate
