"""docs_page_setup (document + section style), docs_model.sections, and
drive_revision_list / drive_revision_get, without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import make_body, make_doc
from test_docs_editing import _FakeDocs

from google_workspace_mcp import docs_model, server

# ─── docs_model.sections ─────────────────────────────────────────────────


def test_sections_with_no_explicit_break_is_the_whole_body():
    body = make_body(("NORMAL_TEXT", "Hi"))  # body_end 4
    assert docs_model.sections(body) == [{"start_index": 1, "end_index": 3}]


def test_sections_splits_on_an_explicit_sectionbreak_element():
    body = make_body(("NORMAL_TEXT", "Hi"))
    # A second section break, as insertSectionBreak would leave behind: one
    # index wide, sitting after the "Hi\n" paragraph (which now ends at 4).
    body["content"].append({"startIndex": 4, "endIndex": 5, "sectionBreak": {}})
    body["content"].append({
        "startIndex": 5, "endIndex": 9,
        "paragraph": {"elements": [{"startIndex": 5, "textRun": {"content": "Bye\n"}}],
                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}},
    })
    assert docs_model.sections(body) == [
        {"start_index": 1, "end_index": 4},
        {"start_index": 5, "end_index": 8},
    ]


# ─── docs_page_setup: document level ─────────────────────────────────────


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(("NORMAL_TEXT", "Hi")))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def test_page_setup_preset_orientation_margins_and_background(fake):
    server.docs_page_setup(
        "personal", "doc-1",
        preset="letter", orientation="landscape",
        margin_top_pt=36, margin_left_pt=54,
        background_color="#f5f5f5",
    )
    req = fake.sent["requests"][0]["updateDocumentStyle"]
    assert req["documentStyle"] == {
        "pageSize": {"width": {"magnitude": 612.0, "unit": "PT"}, "height": {"magnitude": 792.0, "unit": "PT"}},
        "flipPageOrientation": True,
        "marginTop": {"magnitude": 36, "unit": "PT"},
        "marginLeft": {"magnitude": 54, "unit": "PT"},
        "background": {"color": server._rgb("#f5f5f5")},
    }
    assert set(req["fields"].split(",")) == {"pageSize", "flipPageOrientation", "marginTop", "marginLeft", "background"}
    assert req["tabId"] == "t.0"


def test_page_setup_portrait_sets_flip_to_false(fake):
    server.docs_page_setup("personal", "doc-1", orientation="portrait")
    req = fake.sent["requests"][0]["updateDocumentStyle"]
    assert req["documentStyle"] == {"flipPageOrientation": False}
    assert req["fields"] == "flipPageOrientation"


def test_page_setup_width_only_falls_back_to_the_current_height(monkeypatch):
    doc = make_doc(("NORMAL_TEXT", "Hi"))
    doc["tabs"][0]["documentTab"]["documentStyle"] = {
        "pageSize": {"width": {"magnitude": 500, "unit": "PT"}, "height": {"magnitude": 700, "unit": "PT"}}
    }
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    server.docs_page_setup("personal", "doc-1", width_pt=480)
    req = svc.sent["requests"][0]["updateDocumentStyle"]
    assert req["documentStyle"]["pageSize"] == {
        "width": {"magnitude": 480, "unit": "PT"}, "height": {"magnitude": 700, "unit": "PT"},
    }


def test_page_setup_width_alone_with_no_existing_size_raises(fake):
    with pytest.raises(ValueError, match="Need both a width and a height"):
        server.docs_page_setup("personal", "doc-1", width_pt=480)


def test_page_setup_needs_at_least_one_param(fake):
    with pytest.raises(ValueError, match="Nothing to change"):
        server.docs_page_setup("personal", "doc-1")


# ─── docs_page_setup: section level ──────────────────────────────────────


def test_page_setup_section_margins_and_columns(fake):
    out = server.docs_page_setup(
        "personal", "doc-1", section_index=0,
        margin_left_pt=40, margin_right_pt=40,
        column_count=2, column_spacing_pt=18,
    )
    req = fake.sent["requests"][0]["updateSectionStyle"]
    assert req["range"] == {"startIndex": 1, "endIndex": 3, "tabId": "t.0"}
    assert req["sectionStyle"] == {
        "marginLeft": {"magnitude": 40, "unit": "PT"},
        "marginRight": {"magnitude": 40, "unit": "PT"},
        "columnProperties": [{"paddingEnd": {"magnitude": 18, "unit": "PT"}}, {"paddingEnd": {"magnitude": 18, "unit": "PT"}}],
    }
    assert set(req["fields"].split(",")) == {"marginLeft", "marginRight", "columnProperties"}
    assert out["section_index"] == 0


def test_page_setup_section_column_count_without_spacing(fake):
    server.docs_page_setup("personal", "doc-1", section_index=0, column_count=3)
    req = fake.sent["requests"][0]["updateSectionStyle"]
    assert req["sectionStyle"]["columnProperties"] == [{}, {}, {}]


def test_page_setup_unknown_section_index_raises(fake):
    with pytest.raises(ValueError, match="No section at section_index 1"):
        server.docs_page_setup("personal", "doc-1", section_index=1, margin_top_pt=10)


def test_page_setup_section_needs_at_least_one_param(fake):
    with pytest.raises(ValueError, match="Nothing to change"):
        server.docs_page_setup("personal", "doc-1", section_index=0)


def test_page_setup_column_count_out_of_range(fake):
    with pytest.raises(ValueError, match="column_count must be 1-3"):
        server.docs_page_setup("personal", "doc-1", section_index=0, column_count=4)


# ─── drive_revision_list / drive_revision_get ────────────────────────────


class _Call:
    def __init__(self, log, name, resp=None, **kw):
        log.append((name, kw))
        self._resp = resp if resp is not None else {}

    def execute(self):
        return self._resp


class _FakeDrive:
    def __init__(self, list_resp=None, get_resp=None):
        self.list_resp = list_resp or {}
        self.get_resp = get_resp or {}
        self.log = []

    def revisions(self):
        return self

    def list(self, **kw):
        return _Call(self.log, "revisions.list", resp=self.list_resp, **kw)

    def get(self, **kw):
        return _Call(self.log, "revisions.get", resp=self.get_resp, **kw)


class _FakeResp:
    def __init__(self, status):
        self.status = status


class _FakeHttp:
    def __init__(self, status=200, content=b""):
        self.status = status
        self.content = content
        self.requested = []

    def request(self, url, **kw):
        self.requested.append(url)
        return _FakeResp(self.status), self.content


def test_revision_list_passes_fields_and_page_size(monkeypatch):
    revs = [{"id": "1", "modifiedTime": "t1"}, {"id": "2", "modifiedTime": "t2"}]
    svc = _FakeDrive(list_resp={"revisions": revs, "nextPageToken": "np"})
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)

    out = server.drive_revision_list("personal", "file-1", page_size=10)

    name, kw = svc.log[0]
    assert name == "revisions.list"
    assert kw["fileId"] == "file-1" and kw["pageSize"] == 10
    assert "exportLinks" in kw["fields"] and "keepForever" in kw["fields"]
    assert out == {"revisions": revs, "next_page_token": "np"}


def test_revision_get_downloads_export_link_content(monkeypatch):
    meta = {
        "id": "rev-1", "modifiedTime": "t1", "keepForever": False,
        "exportLinks": {"text/plain": "https://export/rev-1.txt", "application/pdf": "https://export/rev-1.pdf"},
    }
    svc = _FakeDrive(get_resp=meta)
    http = _FakeHttp(content=b"Hello revision")
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)
    monkeypatch.setattr(server.auth, "authorized_http", lambda account: http)

    out = server.drive_revision_get("personal", "file-1", "rev-1")

    name, kw = svc.log[0]
    assert name == "revisions.get" and kw["fileId"] == "file-1" and kw["revisionId"] == "rev-1"
    assert http.requested == ["https://export/rev-1.txt"]
    assert out["content"] == "Hello revision"
    assert out["id"] == "rev-1"


def test_revision_get_respects_export_mime_type(monkeypatch):
    meta = {"id": "rev-1", "exportLinks": {"application/pdf": "https://export/rev-1.pdf"}}
    svc = _FakeDrive(get_resp=meta)
    http = _FakeHttp(content=b"%PDF-1.4 ...")
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)
    monkeypatch.setattr(server.auth, "authorized_http", lambda account: http)

    server.drive_revision_get("personal", "file-1", "rev-1", export_mime_type="application/pdf")
    assert http.requested == ["https://export/rev-1.pdf"]


def test_revision_get_without_export_link_skips_the_download(monkeypatch):
    meta = {"id": "rev-1", "mimeType": "image/png", "size": "1234"}  # a binary revision
    svc = _FakeDrive(get_resp=meta)
    http = _FakeHttp()
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)
    monkeypatch.setattr(server.auth, "authorized_http", lambda account: http)

    out = server.drive_revision_get("personal", "file-1", "rev-1")
    assert out == meta
    assert http.requested == []


def test_revision_get_raises_on_a_failed_download(monkeypatch):
    meta = {"id": "rev-1", "exportLinks": {"text/plain": "https://export/rev-1.txt"}}
    svc = _FakeDrive(get_resp=meta)
    http = _FakeHttp(status=403, content=b"nope")
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)
    monkeypatch.setattr(server.auth, "authorized_http", lambda account: http)

    with pytest.raises(ValueError, match="HTTP 403"):
        server.drive_revision_get("personal", "file-1", "rev-1")


def test_revision_get_falls_back_to_base64_for_binary_content(monkeypatch):
    meta = {"id": "rev-1", "exportLinks": {"application/pdf": "https://export/rev-1.pdf"}}
    svc = _FakeDrive(get_resp=meta)
    http = _FakeHttp(content=b"\xff\xd8\xff\xe0not-utf8")
    monkeypatch.setattr(server.auth, "drive", lambda account: svc)
    monkeypatch.setattr(server.auth, "authorized_http", lambda account: http)

    out = server.drive_revision_get("personal", "file-1", "rev-1", export_mime_type="application/pdf")
    assert "content" not in out
    assert out["content_base64"]
