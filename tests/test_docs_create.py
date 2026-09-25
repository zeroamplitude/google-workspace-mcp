"""docs_create: a new Google Doc via Drive, optionally filled with Markdown."""

from __future__ import annotations

import pytest
from docbuilder import make_doc
from test_docs_editing import _Call, _FakeDocs

from google_workspace_mcp import server


class _FakeDriveFiles:
    def __init__(self, log, resp):
        self.log, self.resp = log, resp

    def create(self, **kw):
        return _Call(self.log, "files.create", resp=self.resp, **kw)


class _FakeDrive:
    def __init__(self, log, resp):
        self._files = _FakeDriveFiles(log, resp)

    def files(self):
        return self._files


@pytest.fixture
def fake(monkeypatch):
    # A brand-new doc's body is a single empty paragraph (index 1..2).
    docs = _FakeDocs(make_doc(("NORMAL_TEXT", "")))
    drive_resp = {"id": "doc-1", "name": "New Doc", "webViewLink": "https://docs.google.com/document/d/doc-1/edit"}
    drive = _FakeDrive(docs.log, drive_resp)
    monkeypatch.setattr(server.auth, "docs", lambda account: docs)
    monkeypatch.setattr(server.auth, "drive", lambda account: drive)
    return docs, drive


def test_create_without_markdown_only_calls_drive(fake):
    docs, _drive = fake
    out = server.docs_create("personal", "New Doc")
    assert [n for n, _ in docs.log] == ["files.create"]
    _, kw = docs.log[0]
    assert kw["body"] == {"name": "New Doc", "mimeType": "application/vnd.google-apps.document"}
    assert kw["fields"] == "id, name, webViewLink"
    assert kw["supportsAllDrives"] is True
    assert "parents" not in kw["body"]
    assert out == {"document_id": "doc-1", "title": "New Doc", "url": "https://docs.google.com/document/d/doc-1/edit"}


def test_folder_id_sets_parents(fake):
    docs, _drive = fake
    server.docs_create("personal", "New Doc", folder_id="folder-1")
    _, kw = docs.log[0]
    assert kw["body"]["parents"] == ["folder-1"]


def test_markdown_is_inserted_at_index_1_end_empty(fake):
    docs, _drive = fake
    out = server.docs_create("personal", "New Doc", markdown="# Hello")
    assert [n for n, _ in docs.log] == ["files.create", "get", "batchUpdate"]
    reqs = docs.sent["requests"]
    assert reqs[0]["insertText"] == {"text": "Hello", "location": {"index": 1, "tabId": "t.0"}}
    heading = next(r for r in reqs if "updateParagraphStyle" in r and
                    r["updateParagraphStyle"]["paragraphStyle"].get("namedStyleType") == "HEADING_1")
    assert heading["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 7, "tabId": "t.0"}
    assert out["document_id"] == "doc-1"
    assert out["title"] == "New Doc"
    assert out["url"] == "https://docs.google.com/document/d/doc-1/edit"
    assert out["requests_applied"] == len(reqs)
    assert out["revision_id"] == "rev-2"


def test_no_markdown_means_no_get_or_batch(fake):
    docs, _drive = fake
    server.docs_create("personal", "Blank")
    assert "get" not in [n for n, _ in docs.log]
    assert "batchUpdate" not in [n for n, _ in docs.log]
