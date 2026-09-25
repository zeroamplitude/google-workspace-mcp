"""docs_* tools (0.13.0): the batchUpdate bodies they send, without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_body, make_doc

from google_workspace_mcp import server


class _Call:
    def __init__(self, log, name, resp=None, **kw):
        log.append((name, kw))
        self._resp = resp if resp is not None else {}

    def execute(self):
        return self._resp


class _FakeDocs:
    def __init__(self, doc):
        self.doc = doc
        self.log = []
        self.batch_reply = {"writeControl": {"requiredRevisionId": "rev-2"}, "replies": []}

    def documents(self):
        return self

    def get(self, **kw):
        return _Call(self.log, "get", resp=self.doc, **kw)

    def batchUpdate(self, **kw):
        return _Call(self.log, "batchUpdate", resp=self.batch_reply, **kw)

    @property
    def sent(self):
        name, kw = self.log[-1]
        assert name == "batchUpdate"
        return kw["body"]


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(*SAMPLE))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def test_get_returns_outline_tabs_and_revision(fake):
    out = server.docs_get("personal", "doc-1", include_paragraphs=True)
    assert fake.log[0] == ("get", {"documentId": "doc-1", "includeTabsContent": True})
    assert out["revision_id"] == "rev-1"
    assert out["tabs"] == [{"tab_id": "t.0", "title": "T.0", "depth": 0}]
    assert out["outline"][2] == {
        "level": 2, "heading": "Scope", "start_index": 27, "body_start_index": 33, "end_index": 43,
    }
    assert out["text"].startswith("Plan\nIntro\nHello 👋 world\n")
    assert out["paragraphs"][1]["style"] == "HEADING_1"
    assert out["headers"] == {}
    assert out["footers"] == {}
    assert out["has_pending_suggestions"] is False


def test_get_suggestions_view_mode_is_passed_through(fake):
    server.docs_get("personal", "doc-1", suggestions_view_mode="accepted")
    assert fake.log[0] == (
        "get",
        {"documentId": "doc-1", "includeTabsContent": True, "suggestionsViewMode": "PREVIEW_SUGGESTIONS_ACCEPTED"},
    )


def _para(text):
    return {"paragraph": {
        "elements": [{"textRun": {"content": text}}], "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
    }}


def _doc_with_headers_footers_footnotes():
    body = {"content": [{"startIndex": 1, "endIndex": 13, "paragraph": {
        "elements": [
            {"startIndex": 1, "textRun": {"content": "See "}},
            {"startIndex": 5, "footnoteReference": {"footnoteId": "fn1", "footnoteNumber": "1"}},
            {"startIndex": 6, "textRun": {"content": " note.\n"}},
        ],
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
    }}]}
    return {
        "documentId": "doc-1", "title": "Doc", "revisionId": "rev-1",
        "tabs": [{
            "tabProperties": {"tabId": "t.0", "title": "T.0"},
            "documentTab": {
                "body": body,
                "headers": {"h1": {"content": [_para("Header text\n")]}},
                "footers": {"f1": {"content": [_para("Footer text\n")]}},
                "footnotes": {"fn1": {"content": [_para("Note text.\n")]}},
            },
        }],
    }


def test_get_includes_headers_footers_and_footnotes_in_text(monkeypatch):
    svc = _FakeDocs(_doc_with_headers_footers_footnotes())
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    out = server.docs_get("personal", "doc-1")
    assert out["headers"] == {"h1": "Header text"}
    assert out["footers"] == {"f1": "Footer text"}
    assert "See [1] note." in out["text"]
    assert "[1] Note text." in out["text"]


def test_get_includes_headers_footers_and_footnotes_in_markdown(monkeypatch):
    svc = _FakeDocs(_doc_with_headers_footers_footnotes())
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    out = server.docs_get("personal", "doc-1", format="markdown")
    assert out["headers"] == {"h1": "Header text"}
    assert out["footers"] == {"f1": "Footer text"}
    assert out["markdown"] == "See [^1] note.\n\n[^1]: Note text."


def test_get_reports_pending_suggestions(monkeypatch):
    doc = _doc_with_headers_footers_footnotes()
    doc["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["suggestedInsertionIds"] = ["s1"]
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    out = server.docs_get("personal", "doc-1")
    assert out["has_pending_suggestions"] is True


def test_every_write_is_pinned_to_the_revision_it_read(fake):
    server.docs_insert("personal", "doc-1", "hi")
    assert fake.sent["writeControl"] == {"requiredRevisionId": "rev-1"}


def test_a_stale_revision_is_refused_before_writing(fake):
    with pytest.raises(ValueError, match="changed since revision rev-0"):
        server.docs_insert("personal", "doc-1", "hi", revision_id="rev-0")
    assert [n for n, _ in fake.log] == ["get"]


def test_insert_at_end_goes_before_the_final_newline(fake):
    out = server.docs_insert("personal", "doc-1", "More.")
    first = fake.sent["requests"][0]["insertText"]
    assert first == {"text": "\nMore.", "location": {"index": 67, "tabId": "t.0"}}
    assert out["inserted_at"] == 67 and out["revision_id"] == "rev-2"


def test_insert_after_heading_lands_after_its_subsections(fake):
    server.docs_insert("personal", "doc-1", "New para", at="after_heading", heading="intro")
    first = fake.sent["requests"][0]["insertText"]
    assert first["location"]["index"] == 54  # start of "Budget", after Scope and Risks
    assert first["text"] == "New para\n"


def test_insert_at_start_and_at_index(fake):
    server.docs_insert("personal", "doc-1", "Top", at="start")
    assert fake.sent["requests"][0]["insertText"]["location"]["index"] == 1
    server.docs_insert("personal", "doc-1", "mid", at="index", index=14)
    assert fake.sent["requests"][0]["insertText"]["text"] == "mid"  # inline: no newline
    with pytest.raises(ValueError, match="between 1 and 67"):
        server.docs_insert("personal", "doc-1", "x", at="index", index=68)


def test_replace_section_deletes_body_then_inserts_at_the_same_index(fake):
    out = server.docs_replace_section("personal", "doc-1", "Scope", "Out of scope: **nothing**.")
    reqs = fake.sent["requests"]
    assert reqs[0] == {"deleteContentRange": {"range": {"startIndex": 33, "endIndex": 43, "tabId": "t.0"}}}
    assert reqs[1]["insertText"] == {
        "text": "Out of scope: nothing.\n", "location": {"index": 33, "tabId": "t.0"},
    }
    assert out["section"] == "Scope" and out["replaced_range"] == [33, 43]


def test_replace_section_without_heading_replaces_the_heading_too(fake):
    server.docs_replace_section("personal", "doc-1", "Risks", "## Hazards\nMany.", keep_heading=False)
    assert fake.sent["requests"][0]["deleteContentRange"]["range"]["startIndex"] == 43


def test_replace_last_section_fills_the_emptied_final_paragraph(fake):
    server.docs_replace_section("personal", "doc-1", "Budget", "Pricey.")
    reqs = fake.sent["requests"]
    assert reqs[0]["deleteContentRange"]["range"] == {"startIndex": 61, "endIndex": 67, "tabId": "t.0"}
    assert reqs[1]["insertText"]["text"] == "Pricey."  # no trailing newline: the final one remains


def test_replace_empty_last_section(monkeypatch):
    svc = _FakeDocs(make_doc(("NORMAL_TEXT", "x"), ("HEADING_1", "Tail")))  # 1..3, 3..8
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    server.docs_replace_section("personal", "doc-1", "Tail", "body")
    reqs = svc.sent["requests"]
    assert "deleteContentRange" not in reqs[0]
    assert reqs[0]["insertText"] == {"text": "\nbody", "location": {"index": 7, "tabId": "t.0"}}


def test_replace_section_may_delete_a_table(monkeypatch):
    svc = _FakeDocs(make_doc(("HEADING_1", "Data"), ("table", [["a"]]), ("HEADING_1", "Next")))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    out = server.docs_replace_section("personal", "doc-1", "Data", "gone")
    reqs = svc.sent["requests"]
    assert reqs[0] == {"deleteContentRange": {"range": {"startIndex": 6, "endIndex": 12, "tabId": "t.0"}}}
    assert reqs[1]["insertText"] == {"text": "gone\n", "location": {"index": 6, "tabId": "t.0"}}
    assert out["replaced_range"] == [6, 12]
    assert [n for n, _ in svc.log] == ["get", "batchUpdate"]  # no table in the new markdown: one batch, as before


def test_replace_section_still_refuses_a_section_break(monkeypatch):
    body = make_body(("HEADING_1", "Data"), ("NORMAL_TEXT", "x"), ("HEADING_1", "Next"))
    # Splice a sectionBreak into the section, the way structural_in_range looks for one.
    body["content"].insert(2, {"startIndex": 6, "endIndex": 7, "sectionBreak": {}})
    svc = _FakeDocs({
        "documentId": "doc-1", "title": "Doc", "revisionId": "rev-1",
        "tabs": [{"tabProperties": {"tabId": "t.0", "title": "T.0"}, "documentTab": {"body": body}}],
    })
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    with pytest.raises(ValueError, match="contains a sectionBreak"):
        server.docs_replace_section("personal", "doc-1", "Data", "gone")


def test_edits_address_the_requested_tab(monkeypatch):
    doc = make_doc(tabs={"t.0": make_body(("HEADING_1", "A")), "t.1": make_body(("HEADING_1", "B"), ("NORMAL_TEXT", "b"))})
    svc = _FakeDocs(doc)
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    server.docs_replace_section("personal", "doc-1", "B", "c", tab_id="t.1")
    assert svc.sent["requests"][0]["deleteContentRange"]["range"]["tabId"] == "t.1"


def test_replace_text_counts_occurrences_and_scopes_tabs(fake):
    fake.batch_reply = {"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]}
    out = server.docs_replace_text("personal", "doc-1", "Q3", "Q4", tab_id="t.0", revision_id="rev-1")
    assert fake.sent == {
        "requests": [{"replaceAllText": {
            "containsText": {"text": "Q3", "matchCase": True},
            "replaceText": "Q4",
            "tabsCriteria": {"tabIds": ["t.0"]},
        }}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }
    assert out["occurrences_changed"] == 3


def test_replace_text_without_revision_sends_no_write_control(fake):
    server.docs_replace_text("personal", "doc-1", "a", "b")
    assert "writeControl" not in fake.sent


def test_delete_range_validates_and_sends(fake):
    with pytest.raises(ValueError):
        server.docs_delete_range("personal", "doc-1", 5, 5)
    server.docs_delete_range("personal", "doc-1", 33, 43, revision_id="rev-1")
    assert fake.sent["requests"] == [{"deleteContentRange": {"range": {"startIndex": 33, "endIndex": 43}}}]


def test_a_non_doc_is_explained(monkeypatch):
    from googleapiclient.errors import HttpError

    class _Resp(dict):
        status = 400
        reason = "Bad Request"

    class _Broken(_FakeDocs):
        def get(self, **kw):
            raise HttpError(_Resp(), b"{}")

    monkeypatch.setattr(server.auth, "docs", lambda account: _Broken({}))
    with pytest.raises(ValueError, match="not a Google Doc"):
        server.docs_get("personal", "pdf-1")


def _styles(fake):
    return [(r["updateTextStyle"]["range"]["startIndex"], r["updateTextStyle"]["range"]["endIndex"],
             r["updateTextStyle"]["textStyle"], r["updateTextStyle"]["fields"])
            for r in fake.sent["requests"]]


def test_format_heading_line_with_color_font_and_size(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", color="#1a73e8", font="Georgia", size_pt=18)
    [(s, e, style, fields)] = _styles(fake)
    assert (s, e) == (27, 32)  # "Scope", newline excluded
    assert style["foregroundColor"]["color"]["rgbColor"] == pytest.approx({"red": 26 / 255, "green": 115 / 255, "blue": 232 / 255})
    assert style["weightedFontFamily"] == {"fontFamily": "Georgia"}
    assert style["fontSize"] == {"magnitude": 18, "unit": "PT"}
    assert fields == "foregroundColor,weightedFontFamily,fontSize"
    assert fake.sent["writeControl"] == {"requiredRevisionId": "rev-1"}


def test_format_section_body_and_every_occurrence_of_text(fake):
    server.docs_format("personal", "doc-1", "section", heading="Risks", italic=True)
    assert _styles(fake) == [(49, 54, {"italic": True}, "italic")]
    server.docs_format("personal", "doc-1", "text", text="o", match_case=False, color="#e33")
    starts = [s for s, *_ in _styles(fake)]
    assert starts == [10, 16, 22, 29, 38]  # Intro, Hello, world (after the 2-unit emoji), Scope, scope.


def test_format_refuses_nothing_to_do_bad_color_and_missing_text(fake):
    with pytest.raises(ValueError, match="Nothing to change"):
        server.docs_format("personal", "doc-1", "heading", heading="Scope")
    with pytest.raises(ValueError, match="hex"):
        server.docs_format("personal", "doc-1", "heading", heading="Scope", color="blue")
    with pytest.raises(ValueError, match="does not appear"):
        server.docs_format("personal", "doc-1", "text", text="zebra", bold=True)
    assert "batchUpdate" not in [n for n, _ in fake.log]
