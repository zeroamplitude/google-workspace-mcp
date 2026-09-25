"""docs_insert_image (0.13.0): placement, and the temporary Drive upload for a local file."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_doc
from test_docs_editing import _Call, _FakeDocs

from google_workspace_mcp import server


class _Resource:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def create(self, **kw):
        kw.pop("media_body", None)
        return _Call(self.log, f"{self.name}.create", resp={"id": "up-1"}, **kw)

    def delete(self, **kw):
        return _Call(self.log, f"{self.name}.delete", **kw)

    def update(self, **kw):
        return _Call(self.log, f"{self.name}.update", **kw)


class _FakeDrive:
    def __init__(self, log):
        self.log = log

    def files(self):
        return _Resource(self.log, "files")

    def permissions(self):
        return _Resource(self.log, "permissions")


@pytest.fixture
def fake(monkeypatch):
    docs = _FakeDocs(make_doc(*SAMPLE))
    docs.batch_reply = {"replies": [{"insertInlineImage": {"objectId": "kix.img"}}]}
    drive = _FakeDrive(docs.log)  # one shared log: the order across APIs matters
    monkeypatch.setattr(server.auth, "docs", lambda account: docs)
    monkeypatch.setattr(server.auth, "drive", lambda account: drive)
    return docs


def test_url_image_at_end_gets_its_own_paragraph(fake):
    out = server.docs_insert_image("personal", "doc-1", image_url="https://x/i.png", width_pt=200)
    reqs = fake.sent["requests"]
    assert reqs[0] == {"insertText": {"text": "\n", "location": {"index": 67, "tabId": "t.0"}}}
    assert reqs[1] == {"insertInlineImage": {
        "uri": "https://x/i.png",
        "location": {"index": 68, "tabId": "t.0"},
        "objectSize": {"width": {"magnitude": 200, "unit": "PT"}},
    }}
    assert out["object_id"] == "kix.img" and out["inserted_at"] == 68
    # Its paragraph is reset to plain, not left inheriting the neighbour's heading/list style.
    assert reqs[3]["updateParagraphStyle"]["range"] == {"startIndex": 68, "endIndex": 70, "tabId": "t.0"}
    assert reqs[3]["updateParagraphStyle"]["paragraphStyle"] == {"namedStyleType": "NORMAL_TEXT"}


def test_image_after_heading_goes_in_a_new_paragraph_before_the_next_section(fake):
    server.docs_insert_image("personal", "doc-1", image_url="https://x/i.png", at="after_heading", heading="Scope")
    reqs = fake.sent["requests"]
    assert reqs[0]["insertText"]["location"]["index"] == 43
    assert reqs[1]["insertInlineImage"]["location"]["index"] == 43
    assert "objectSize" not in reqs[1]["insertInlineImage"]


def test_image_inline_mid_paragraph(fake):
    server.docs_insert_image("personal", "doc-1", image_url="https://x/i.png", at="index", index=14)
    assert [next(iter(r)) for r in fake.sent["requests"]] == ["insertInlineImage"]  # no restyling inline


def test_local_image_is_shared_only_while_google_copies_it(fake, tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG fake")
    server.docs_insert_image("personal", "doc-1", local_path=str(img))
    steps = [n for n, _ in fake.log]
    assert steps == ["get", "files.create", "permissions.create", "batchUpdate", "permissions.delete", "files.update"]
    uri = next(r for r in fake.log[3][1]["body"]["requests"] if "insertInlineImage" in r)["insertInlineImage"]["uri"]
    assert "id=up-1" in uri
    assert fake.log[-1][1]["body"] == {"trashed": True}


def test_local_upload_is_cleaned_up_even_when_the_insert_fails(fake, tmp_path):
    img = tmp_path / "chart.jpg"
    img.write_bytes(b"jpeg")

    def boom(**kw):
        raise RuntimeError("Docs rejected the image")

    fake.batchUpdate = boom
    with pytest.raises(RuntimeError, match="rejected"):
        server.docs_insert_image("personal", "doc-1", local_path=str(img))
    assert [n for n, _ in fake.log][-2:] == ["permissions.delete", "files.update"]


def test_rejects_wrong_type_and_ambiguous_source(fake, tmp_path):
    svg = tmp_path / "logo.svg"
    svg.write_text("<svg/>")
    with pytest.raises(ValueError, match="PNG, JPEG or GIF"):
        server.docs_insert_image("personal", "doc-1", local_path=str(svg))
    with pytest.raises(ValueError, match="exactly one"):
        server.docs_insert_image("personal", "doc-1")
    assert "files.create" not in [n for n, _ in fake.log]
