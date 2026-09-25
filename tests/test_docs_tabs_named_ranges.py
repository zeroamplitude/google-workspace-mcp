"""docs_tab (add/delete/rename tabs) and docs_named_range (create/delete/
replace/list named ranges), without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import make_body, make_doc
from test_docs_editing import _FakeDocs

from google_workspace_mcp import server


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(("NORMAL_TEXT", "Hello world")))  # tab "t.0"
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


@pytest.fixture
def two_tab_fake(monkeypatch):
    svc = _FakeDocs(make_doc(tabs={
        "t.0": make_body(("NORMAL_TEXT", "First")),
        "t.1": make_body(("NORMAL_TEXT", "Second")),
    }))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


# ─── docs_tab ─────────────────────────────────────────────────────────────


def test_add_sends_all_given_properties_and_returns_the_new_tab_id(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.1", "title": "New Tab"}}}],
    }
    out = server.docs_tab(
        "personal", "doc-1", "add",
        title="New Tab", parent_tab_id="t.0", index=1, icon_emoji="📌",
    )
    assert fake.sent["requests"] == [{"addDocumentTab": {"tabProperties": {
        "title": "New Tab", "parentTabId": "t.0", "index": 1, "iconEmoji": "📌",
    }}}]
    assert out["action"] == "add"
    assert out["tab_id"] == "t.1"


def test_add_with_no_properties_sends_an_empty_tabProperties(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.1"}}}],
    }
    server.docs_tab("personal", "doc-1", "add")
    assert fake.sent["requests"] == [{"addDocumentTab": {"tabProperties": {}}}]


def test_delete_needs_tab_id(fake):
    with pytest.raises(ValueError, match="needs `tab_id`"):
        server.docs_tab("personal", "doc-1", "delete")


def test_delete_unknown_tab_is_refused(fake):
    with pytest.raises(ValueError, match="No tab"):
        server.docs_tab("personal", "doc-1", "delete", tab_id="t.9")


def test_delete_sends_deleteTab(two_tab_fake):
    out = server.docs_tab("personal", "doc-1", "delete", tab_id="t.1")
    assert two_tab_fake.sent["requests"] == [{"deleteTab": {"tabId": "t.1"}}]
    assert out["action"] == "delete" and out["tab_id"] == "t.1"


def test_rename_needs_title(two_tab_fake):
    with pytest.raises(ValueError, match="needs `title`"):
        server.docs_tab("personal", "doc-1", "rename", tab_id="t.1")


def test_rename_sends_updateDocumentTabProperties(two_tab_fake):
    out = server.docs_tab("personal", "doc-1", "rename", tab_id="t.1", title="Renamed")
    assert two_tab_fake.sent["requests"] == [{"updateDocumentTabProperties": {
        "tabProperties": {"tabId": "t.1", "title": "Renamed"},
        "fields": "title",
    }}]
    assert out["tab_id"] == "t.1"


# ─── docs_named_range ───────────────────────────────────────────────────


def test_create_needs_name(fake):
    with pytest.raises(ValueError, match="needs `name`"):
        server.docs_named_range("personal", "doc-1", "create", text="Hello")


def test_create_needs_text_or_indices(fake):
    with pytest.raises(ValueError, match="needs `text`"):
        server.docs_named_range("personal", "doc-1", "create", name="greeting")


def test_create_via_text_locates_the_quote(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createNamedRange": {"namedRangeId": "nr-1"}}],
    }
    out = server.docs_named_range("personal", "doc-1", "create", name="greeting", text="Hello")
    assert fake.sent["requests"] == [{"createNamedRange": {
        "name": "greeting", "range": {"startIndex": 1, "endIndex": 6, "tabId": "t.0"},
    }}]
    assert out["named_range_id"] == "nr-1"
    assert out["start_index"] == 1 and out["end_index"] == 6


def test_create_via_explicit_indices(fake):
    fake.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"createNamedRange": {"namedRangeId": "nr-1"}}],
    }
    server.docs_named_range("personal", "doc-1", "create", name="greeting", start_index=1, end_index=6)
    assert fake.sent["requests"] == [{"createNamedRange": {
        "name": "greeting", "range": {"startIndex": 1, "endIndex": 6, "tabId": "t.0"},
    }}]


def test_create_text_not_found_is_refused(fake):
    with pytest.raises(ValueError, match="does not appear"):
        server.docs_named_range("personal", "doc-1", "create", name="greeting", text="nonexistent")


def test_delete_needs_id_or_name(fake):
    with pytest.raises(ValueError, match="needs `named_range_id` or `name`"):
        server.docs_named_range("personal", "doc-1", "delete")


def test_delete_by_id(fake):
    server.docs_named_range("personal", "doc-1", "delete", named_range_id="nr-1")
    assert fake.sent["requests"] == [{"deleteNamedRange": {"namedRangeId": "nr-1"}}]


def test_delete_by_name_scoped_to_a_tab(fake):
    server.docs_named_range("personal", "doc-1", "delete", name="greeting", tab_id="t.0")
    assert fake.sent["requests"] == [{"deleteNamedRange": {
        "name": "greeting", "tabsCriteria": {"tabIds": ["t.0"]},
    }}]


def test_replace_needs_text(fake):
    with pytest.raises(ValueError, match="needs `text`"):
        server.docs_named_range("personal", "doc-1", "replace", named_range_id="nr-1")


def test_replace_strips_markdown_to_plain_text(fake):
    server.docs_named_range("personal", "doc-1", "replace", named_range_id="nr-1", text="**bold** word")
    assert fake.sent["requests"] == [{"replaceNamedRangeContent": {
        "namedRangeId": "nr-1", "text": "bold word",
    }}]


def test_replace_by_name(fake):
    server.docs_named_range("personal", "doc-1", "replace", name="greeting", text="Hi")
    assert fake.sent["requests"] == [{"replaceNamedRangeContent": {
        "namedRangeName": "greeting", "text": "Hi",
    }}]


def _doc_with_named_range() -> dict:
    body = make_body(("NORMAL_TEXT", "Hello world"))  # "Hello" spans 1..6
    return {
        "documentId": "doc-1", "title": "Doc", "revisionId": "rev-1",
        "tabs": [{
            "tabProperties": {"tabId": "t.0", "title": "T.0"},
            "documentTab": {
                "body": body,
                "namedRanges": {
                    "greeting": {
                        "name": "greeting",
                        "namedRanges": [{"namedRangeId": "nr-1", "name": "greeting",
                                         "ranges": [{"startIndex": 1, "endIndex": 6}]}],
                    },
                },
            },
        }],
    }


def test_list_returns_names_ids_ranges_and_current_text(monkeypatch):
    svc = _FakeDocs(_doc_with_named_range())
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_named_range("personal", "doc-1", "list")
    assert out["named_ranges"] == [{
        "name": "greeting",
        "named_range_id": "nr-1",
        "ranges": [{"start_index": 1, "end_index": 6, "text": "Hello"}],
    }]
    assert [n for n, _ in svc.log] == ["get"]  # read-only


def test_list_with_no_named_ranges_is_empty(fake):
    out = server.docs_named_range("personal", "doc-1", "list")
    assert out["named_ranges"] == []
