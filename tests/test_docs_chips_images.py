"""docs_insert_chip and docs_image: chips (@-mentions, dates, rich links)
and inline/positioned image list/replace/delete, without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_doc
from test_docs_editing import _FakeDocs
from test_docs_image import _FakeDrive

from google_workspace_mcp import server


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(*SAMPLE))  # body_end 68
    svc.batch_reply = {"writeControl": {"requiredRevisionId": "rev-2"}, "replies": [{}]}
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


# ─── docs_insert_chip ─────────────────────────────────────────────────────


def test_person_chip_needs_email():
    with pytest.raises(ValueError, match="needs `email`"):
        server.docs_insert_chip("personal", "doc-1", kind="person")


def test_person_chip_sends_insert_person(fake):
    out = server.docs_insert_chip("personal", "doc-1", kind="person", email="ada@x.com")
    reqs = fake.sent["requests"]
    assert reqs == [{"insertPerson": {
        "personProperties": {"email": "ada@x.com"},
        "location": {"index": 67, "tabId": "t.0"},
    }}]
    assert out["kind"] == "person" and out["inserted_at"] == 67


def test_date_chip_needs_date():
    with pytest.raises(ValueError, match="needs `date`"):
        server.docs_insert_chip("personal", "doc-1", kind="date")


def test_date_chip_sends_insert_date_with_timestamp_and_format(fake):
    server.docs_insert_chip(
        "personal", "doc-1", kind="date", date="2026-09-25", date_format="DATE_FORMAT_ISO8601",
    )
    req = fake.sent["requests"][0]["insertDate"]
    assert req["dateElementProperties"] == {
        "timestamp": "2026-09-25T00:00:00Z", "dateFormat": "DATE_FORMAT_ISO8601",
    }


def test_date_chip_without_timezone_is_treated_as_utc(fake):
    server.docs_insert_chip("personal", "doc-1", kind="date", date="2026-09-25T10:30:00")
    req = fake.sent["requests"][0]["insertDate"]
    assert req["dateElementProperties"] == {"timestamp": "2026-09-25T10:30:00Z"}


def test_date_chip_rejects_a_bad_date(fake):
    with pytest.raises(ValueError, match="ISO date or datetime"):
        server.docs_insert_chip("personal", "doc-1", kind="date", date="not-a-date")


def test_link_chip_needs_url():
    with pytest.raises(ValueError, match="needs `url`"):
        server.docs_insert_chip("personal", "doc-1", kind="link")


def test_link_chip_sends_insert_rich_link(fake):
    server.docs_insert_chip("personal", "doc-1", kind="link", url="https://docs.google.com/document/d/x")
    req = fake.sent["requests"][0]["insertRichLink"]
    assert req["richLinkProperties"] == {"uri": "https://docs.google.com/document/d/x"}


def test_chip_after_text_places_it_right_after_the_quote(monkeypatch):
    doc = make_doc(("NORMAL_TEXT", "Hello world"))
    svc = _FakeDocs(doc)
    svc.batch_reply = {"writeControl": {"requiredRevisionId": "rev-2"}, "replies": [{}]}
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)

    out = server.docs_insert_chip("personal", "doc-1", kind="person", email="a@x.com", after_text="Hello")
    assert svc.sent["requests"][0]["insertPerson"]["location"]["index"] == 6
    assert out["inserted_at"] == 6


def test_chip_after_text_not_found_is_refused(fake):
    with pytest.raises(ValueError, match="does not appear"):
        server.docs_insert_chip("personal", "doc-1", kind="person", email="a@x.com", after_text="nonexistent")
    assert [n for n, _ in fake.log] == ["get"]  # never reached batchUpdate


def test_chip_at_start_places_like_docs_insert(fake):
    server.docs_insert_chip("personal", "doc-1", kind="link", url="https://docs.google.com/x", at="start")
    assert fake.sent["requests"][0]["insertRichLink"]["location"]["index"] == 1


# ─── docs_image ─────────────────────────────────────────────────────────


def _doc_with_images():
    body = {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        {"startIndex": 1, "endIndex": 3, "paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "inline-1"}},
            {"startIndex": 2, "textRun": {"content": "\n"}},
        ]}},
    ]}
    return {
        "documentId": "doc-1", "title": "Doc", "revisionId": "rev-1",
        "tabs": [{
            "tabProperties": {"tabId": "t.0", "title": "T.0"},
            "documentTab": {
                "body": body,
                "inlineObjects": {"inline-1": {"inlineObjectProperties": {"embeddedObject": {
                    "imageProperties": {"contentUri": "https://c/inline-1", "sourceUri": "https://s/inline-1"},
                    "size": {"width": {"magnitude": 100, "unit": "PT"}, "height": {"magnitude": 50, "unit": "PT"}},
                }}}},
                "positionedObjects": {"pos-1": {"positionedObjectProperties": {"embeddedObject": {
                    "imageProperties": {"contentUri": "https://c/pos-1"},
                }}}},
            },
        }],
    }


@pytest.fixture
def image_fake(monkeypatch):
    svc = _FakeDocs(_doc_with_images())
    svc.batch_reply = {"writeControl": {"requiredRevisionId": "rev-2"}, "replies": [{}]}
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    drive = _FakeDrive(svc.log)
    monkeypatch.setattr(server.auth, "drive", lambda account: drive)
    return svc


def test_list_returns_inline_and_positioned_images(image_fake):
    out = server.docs_image("personal", "doc-1", action="list")
    assert out["images"] == [
        {
            "object_id": "inline-1", "kind": "inline", "width_pt": 100, "height_pt": 50,
            "content_uri": "https://c/inline-1", "source_uri": "https://s/inline-1",
        },
        {
            "object_id": "pos-1", "kind": "positioned", "width_pt": None, "height_pt": None,
            "content_uri": "https://c/pos-1", "source_uri": None,
        },
    ]


def test_replace_by_url_sends_replace_image(image_fake):
    out = server.docs_image("personal", "doc-1", action="replace", object_id="inline-1", image_url="https://x/new.png")
    reqs = image_fake.sent["requests"]
    assert reqs == [{"replaceImage": {
        "imageObjectId": "inline-1", "uri": "https://x/new.png",
        "imageReplaceMethod": "CENTER_CROP", "tabId": "t.0",
    }}]
    assert out["action"] == "replace" and out["object_id"] == "inline-1"


def test_replace_needs_exactly_one_source(image_fake):
    with pytest.raises(ValueError, match="exactly one"):
        server.docs_image("personal", "doc-1", action="replace", object_id="inline-1")
    with pytest.raises(ValueError, match="exactly one"):
        server.docs_image(
            "personal", "doc-1", action="replace", object_id="inline-1",
            image_url="https://x/a.png", local_path="/tmp/a.png",
        )


def test_replace_by_local_path_uploads_and_cleans_up(image_fake, tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG fake")
    server.docs_image("personal", "doc-1", action="replace", object_id="inline-1", local_path=str(img))
    steps = [n for n, _ in image_fake.log]
    assert steps == ["get", "files.create", "permissions.create", "batchUpdate", "permissions.delete", "files.update"]
    req = image_fake.log[3][1]["body"]["requests"][0]["replaceImage"]
    assert "id=up-1" in req["uri"]


def test_delete_inline_image_deletes_its_content_range(image_fake):
    out = server.docs_image("personal", "doc-1", action="delete", object_id="inline-1")
    assert image_fake.sent["requests"] == [{"deleteContentRange": {
        "range": {"startIndex": 1, "endIndex": 2, "tabId": "t.0"},
    }}]
    assert out["action"] == "delete" and out["object_id"] == "inline-1"


def test_delete_positioned_image_sends_delete_positioned_object(image_fake):
    server.docs_image("personal", "doc-1", action="delete", object_id="pos-1")
    assert image_fake.sent["requests"] == [{"deletePositionedObject": {"objectId": "pos-1", "tabId": "t.0"}}]


def test_delete_unknown_object_id_is_refused(image_fake):
    with pytest.raises(ValueError, match="No image 'nope'"):
        server.docs_image("personal", "doc-1", action="delete", object_id="nope")


def test_delete_and_replace_need_object_id(image_fake):
    with pytest.raises(ValueError, match="needs `object_id`"):
        server.docs_image("personal", "doc-1", action="delete")
    with pytest.raises(ValueError, match="needs `object_id`"):
        server.docs_image("personal", "doc-1", action="replace", image_url="https://x/a.png")
