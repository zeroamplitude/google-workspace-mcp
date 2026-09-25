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

    def copy(self, **kw):
        return _Call(self.log, "files.copy", resp=self.resp, **kw)


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


def test_template_id_copies_instead_of_creating(fake):
    docs, _drive = fake
    out = server.docs_create("personal", "New Doc", template_id="tmpl-1")
    assert [n for n, _ in docs.log] == ["files.copy"]
    _, kw = docs.log[0]
    assert kw["fileId"] == "tmpl-1"
    assert kw["body"] == {"name": "New Doc"}
    assert kw["supportsAllDrives"] is True
    assert out["document_id"] == "doc-1"


def test_template_id_with_folder_sets_parents(fake):
    docs, _drive = fake
    server.docs_create("personal", "New Doc", template_id="tmpl-1", folder_id="folder-1")
    _, kw = docs.log[0]
    assert kw["body"] == {"name": "New Doc", "parents": ["folder-1"]}


def test_replacements_run_one_batch_update_and_report_occurrences(fake):
    docs, _drive = fake
    docs.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"replaceAllText": {"occurrencesChanged": 2}}, {"replaceAllText": {"occurrencesChanged": 0}}],
    }
    out = server.docs_create(
        "personal", "New Doc", template_id="tmpl-1", replacements={"{{name}}": "Acme", "{{date}}": "2026-09-25"}
    )
    assert [n for n, _ in docs.log] == ["files.copy", "batchUpdate"]
    reqs = docs.sent["requests"]
    assert reqs == [
        {"replaceAllText": {"containsText": {"text": "{{name}}", "matchCase": True}, "replaceText": "Acme"}},
        {"replaceAllText": {"containsText": {"text": "{{date}}", "matchCase": True}, "replaceText": "2026-09-25"}},
    ]
    assert "writeControl" not in docs.sent
    assert out["occurrences_replaced"] == {"{{name}}": 2, "{{date}}": 0}


def test_template_id_with_replacements_and_markdown_appends_at_the_end(fake):
    docs, _drive = fake
    docs.batch_reply = {
        "writeControl": {"requiredRevisionId": "rev-2"},
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}],
    }
    out = server.docs_create(
        "personal", "New Doc", template_id="tmpl-1", replacements={"{{name}}": "Acme"}, markdown="More text"
    )
    assert [n for n, _ in docs.log] == ["files.copy", "batchUpdate", "get", "batchUpdate"]
    assert out["occurrences_replaced"] == {"{{name}}": 1}
    assert out["document_id"] == "doc-1"
