"""docs_format paragraph styling (align, line_spacing, spacing, indent): the
updateParagraphStyle requests it sends, without touching Google."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_doc
from test_docs_editing import _FakeDocs

from google_workspace_mcp import server


@pytest.fixture
def fake(monkeypatch):
    svc = _FakeDocs(make_doc(*SAMPLE))
    monkeypatch.setattr(server.auth, "docs", lambda account: svc)
    return svc


def _pstyles(fake):
    return [(r["updateParagraphStyle"]["range"]["startIndex"], r["updateParagraphStyle"]["range"]["endIndex"],
             r["updateParagraphStyle"]["paragraphStyle"], r["updateParagraphStyle"]["fields"])
            for r in fake.sent["requests"] if "updateParagraphStyle" in r]


def test_align_maps_to_the_docs_enum(fake):
    for align, enum in (("left", "START"), ("center", "CENTER"), ("right", "END"), ("justify", "JUSTIFIED")):
        server.docs_format("personal", "doc-1", "heading", heading="Scope", align=align)
        [(s, e, style, fields)] = _pstyles(fake)
        assert (s, e) == (27, 33)  # the whole heading paragraph, newline included
        assert style == {"alignment": enum}
        assert fields == "alignment"


def test_line_spacing_is_a_percentage(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", line_spacing=1.15)
    [(_, _, style, fields)] = _pstyles(fake)
    assert style == {"lineSpacing": pytest.approx(115)}
    assert fields == "lineSpacing"


def test_space_above_and_below(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", space_above_pt=10, space_below_pt=5)
    [(_, _, style, fields)] = _pstyles(fake)
    assert style == {
        "spaceAbove": {"magnitude": 10, "unit": "PT"},
        "spaceBelow": {"magnitude": 5, "unit": "PT"},
    }
    assert fields == "spaceAbove,spaceBelow"


def test_indent_pt_sets_start_and_first_line(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", indent_pt=18)
    [(_, _, style, fields)] = _pstyles(fake)
    assert style == {
        "indentStart": {"magnitude": 18, "unit": "PT"},
        "indentFirstLine": {"magnitude": 18, "unit": "PT"},
    }
    assert fields == "indentStart,indentFirstLine"


def test_combined_text_and_paragraph_style_send_both_kinds_of_request(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", bold=True, align="center")
    reqs = fake.sent["requests"]
    assert [next(iter(r)) for r in reqs] == ["updateTextStyle", "updateParagraphStyle"]
    text_req = reqs[0]["updateTextStyle"]
    assert (text_req["range"]["startIndex"], text_req["range"]["endIndex"]) == (27, 32)  # text: newline excluded
    assert text_req["textStyle"] == {"bold": True} and text_req["fields"] == "bold"
    para_req = reqs[1]["updateParagraphStyle"]
    assert (para_req["range"]["startIndex"], para_req["range"]["endIndex"]) == (27, 33)  # paragraph: newline included
    assert para_req["paragraphStyle"] == {"alignment": "CENTER"} and para_req["fields"] == "alignment"


def test_paragraph_only_request_needs_no_text_style(fake):
    server.docs_format("personal", "doc-1", "heading", heading="Scope", align="center")
    reqs = fake.sent["requests"]
    assert [next(iter(r)) for r in reqs] == ["updateParagraphStyle"]


def test_section_target_covers_the_whole_section_body(fake):
    server.docs_format("personal", "doc-1", "section", heading="Risks", align="right")
    assert _pstyles(fake) == [(49, 54, {"alignment": "END"}, "alignment")]


def test_text_target_covers_each_matchs_paragraph_deduplicated(fake):
    server.docs_format("personal", "doc-1", "text", text="o", match_case=False, align="left")
    # "o" occurs in Intro(6-12), Hello..world(12-27) twice, Scope(27-33), In scope.(33-43).
    assert _pstyles(fake) == [
        (6, 12, {"alignment": "START"}, "alignment"),
        (12, 27, {"alignment": "START"}, "alignment"),
        (27, 33, {"alignment": "START"}, "alignment"),
        (33, 43, {"alignment": "START"}, "alignment"),
    ]


def test_range_target_uses_the_given_range_directly(fake):
    server.docs_format("personal", "doc-1", "range", start_index=12, end_index=27, align="justify")
    assert _pstyles(fake) == [(12, 27, {"alignment": "JUSTIFIED"}, "alignment")]


def test_nothing_to_change_mentions_the_paragraph_params(fake):
    with pytest.raises(ValueError, match="Nothing to change") as exc:
        server.docs_format("personal", "doc-1", "heading", heading="Scope")
    assert "align" in str(exc.value) and "indent_pt" in str(exc.value)
    assert "batchUpdate" not in [n for n, _ in fake.log]


def test_line_spacing_must_be_positive(fake):
    with pytest.raises(ValueError, match="line_spacing"):
        server.docs_format("personal", "doc-1", "heading", heading="Scope", line_spacing=0)
