"""drive_comment_* tools (0.13.0): listing by section, replying, resolving."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_doc

from google_workspace_mcp import server


class _Call:
    def __init__(self, log, name, resp=None, **kw):
        log.append((name, kw))
        self._resp = resp if resp is not None else {}

    def execute(self):
        return self._resp


def _comment(cid, quote=None, resolved=False, **extra):
    c = {
        "id": cid,
        "content": f"comment {cid}",
        "author": {"displayName": "Ana", "emailAddress": "ana@example.com"},
        "createdTime": "2026-09-01T00:00:00Z",
        "resolved": resolved,
        "replies": [],
        **extra,
    }
    if quote:
        c["quotedFileContent"] = {"mimeType": "text/html", "value": quote}
    return c


class _FakeDrive:
    def __init__(self, pages, mime="application/vnd.google-apps.document"):
        self.pages = pages
        self.mime = mime
        self.log = []

    def comments(self):
        self._res = "comments"
        return self

    def replies(self):
        self._res = "replies"
        return self

    def files(self):
        self._res = "files"
        return self

    def list(self, **kw):
        page = self.pages[len([n for n, _ in self.log if n == "comments.list"])]
        return _Call(self.log, "comments.list", resp=page, **kw)

    def get(self, **kw):
        return _Call(self.log, "files.get", resp={"mimeType": self.mime}, **kw)

    def create(self, **kw):
        prefix = "c" if self._res == "comments" else "r"
        return _Call(self.log, f"{self._res}.create", resp={"id": f"{prefix}-new", **kw["body"]}, **kw)


class _FakeDocs:
    def documents(self):
        return self

    def get(self, **kw):
        return _Call([], "get", resp=make_doc(*SAMPLE))


@pytest.fixture
def wire(monkeypatch):
    def _wire(pages, mime="application/vnd.google-apps.document"):
        drive = _FakeDrive(pages, mime)
        monkeypatch.setattr(server.auth, "drive", lambda account: drive)
        monkeypatch.setattr(server.auth, "docs", lambda account: _FakeDocs())
        return drive
    return _wire


def test_list_pages_through_and_hides_resolved_by_default(wire):
    drive = wire([
        {"comments": [_comment("c1", "world")], "nextPageToken": "p2"},
        {"comments": [_comment("c2", resolved=True), _comment("c3", deleted=True)]},
    ])
    out = server.drive_comment_list("personal", "doc-1")
    assert [c["id"] for c in out["comments"]] == ["c1"]
    tokens = [kw["pageToken"] for n, kw in drive.log if n == "comments.list"]
    assert tokens == [None, "p2"]

    wire([{"comments": [_comment("c1"), _comment("c2", resolved=True)]}])
    assert len(server.drive_comment_list("personal", "doc-1", include_resolved=True)["comments"]) == 2


def test_list_tags_each_comment_with_its_section(wire):
    wire([{"comments": [
        _comment("c1", "world"),
        _comment("c2", "Few."),
        _comment("c3", "text that was edited away"),
        _comment("c4"),  # file-level comment, no quote
    ]}])
    rows = {c["id"]: c for c in server.drive_comment_list("personal", "doc-1")["comments"]}
    assert rows["c1"]["section"] == "Intro" and rows["c1"]["section_path"] == ["Plan", "Intro"]
    assert rows["c2"]["section"] == "Risks" and rows["c2"]["tab_id"] == "t.0"
    assert rows["c3"]["section"] is None
    assert rows["c4"]["section"] is None
    assert rows["c1"]["author"] == "Ana <ana@example.com>"


def test_heading_filter_includes_subsections(wire):
    wire([{"comments": [_comment("c1", "world"), _comment("c2", "Few."), _comment("c3", "Cheap.")]}])
    ids = [c["id"] for c in server.drive_comment_list("personal", "doc-1", heading="intro")["comments"]]
    assert ids == ["c1", "c2"]


def test_quoted_html_entities_are_unescaped_before_matching(wire):
    wire([{"comments": [_comment("c1", "In scope&#46;")]}])
    row = server.drive_comment_list("personal", "doc-1")["comments"][0]
    assert row["quoted_text"] == "In scope." and row["section"] == "Scope"


def test_non_docs_are_listed_untagged_and_refuse_heading(wire):
    wire([{"comments": [_comment("c1", "x")]}], mime="application/pdf")
    row = server.drive_comment_list("personal", "f-1")["comments"][0]
    assert "section" not in row
    wire([{"comments": []}], mime="application/pdf")
    with pytest.raises(ValueError, match="needs a Google Doc"):
        server.drive_comment_list("personal", "f-1", heading="x")


def test_deleted_replies_are_dropped_and_actions_kept(wire):
    replies = [
        {"id": "r1", "content": "ok", "author": {"displayName": "Bo"}},
        {"id": "r2", "deleted": True},
        {"id": "r3", "content": "", "action": "resolve", "author": {"displayName": "Bo"}},
    ]
    wire([{"comments": [_comment("c1", replies=replies)]}])
    got = server.drive_comment_list("personal", "doc-1")["comments"][0]["replies"]
    assert [(r["id"], r.get("action")) for r in got] == [("r1", None), ("r3", "resolve")]


def test_reply_and_resolve_bodies(wire):
    drive = wire([])
    server.drive_comment_reply("personal", "doc-1", "c1", "Done — updated.")
    assert drive.log[-1][1]["body"] == {"content": "Done — updated."}
    assert drive.log[-1][1]["commentId"] == "c1"

    server.drive_comment_resolve("personal", "doc-1", "c1")
    assert drive.log[-1][1]["body"] == {"action": "resolve"}
    server.drive_comment_resolve("personal", "doc-1", "c1", resolved=False, content="Reopening")
    assert drive.log[-1][1]["body"] == {"action": "reopen", "content": "Reopening"}


def test_create_without_quote_posts_plain_comment_and_tags_no_section(wire):
    drive = wire([])
    row = server.drive_comment_create("personal", "doc-1", "New comment")
    assert [n for n, _ in drive.log] == ["files.get", "comments.create"]
    _, kw = drive.log[-1]
    assert kw["body"] == {"content": "New comment"}
    assert row["content"] == "New comment"
    assert row["section"] is None and row["section_path"] == [] and row["tab_id"] is None


def test_create_with_quote_verifies_it_and_tags_the_section(wire):
    drive = wire([])
    row = server.drive_comment_create("personal", "doc-1", "Re: this", quoted_text="world")
    assert [n for n, _ in drive.log] == ["files.get", "comments.create"]
    _, kw = drive.log[-1]
    assert kw["body"] == {
        "content": "Re: this",
        "quotedFileContent": {"mimeType": "text/html", "value": "world"},
    }
    assert row["section"] == "Intro" and row["section_path"] == ["Plan", "Intro"] and row["tab_id"] == "t.0"
    assert row["quoted_text"] == "world"


def test_create_with_a_quote_not_in_the_document_raises_and_creates_nothing(wire):
    drive = wire([])
    with pytest.raises(ValueError, match="Quote not found"):
        server.drive_comment_create("personal", "doc-1", "Re: this", quoted_text="nope, not in there")
    assert "comments.create" not in [n for n, _ in drive.log]


def test_create_on_a_non_doc_skips_verification(wire):
    drive = wire([], mime="application/pdf")
    row = server.drive_comment_create("personal", "f-1", "hi", quoted_text="anything at all")
    assert [n for n, _ in drive.log] == ["files.get", "comments.create"]
    assert "section" not in row
    assert row["quoted_text"] == "anything at all"
