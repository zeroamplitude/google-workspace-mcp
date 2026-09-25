"""docs_markdown (0.13.0): the Markdown subset -> Docs batchUpdate requests."""

from __future__ import annotations

import pytest

from google_workspace_mcp.docs_markdown import (
    MONOSPACE,
    markdown_to_requests,
    parse_blocks,
    parse_inline,
)


def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


def _text_styles(reqs):
    """(start, end, style) for every styling updateTextStyle (the reset excluded)."""
    return [
        (r["updateTextStyle"]["range"]["startIndex"], r["updateTextStyle"]["range"]["endIndex"],
         r["updateTextStyle"]["textStyle"])
        for r in reqs
        if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]
    ]


def test_inline_styles_nest():
    assert parse_inline("a **b *c* d** [d](http://x) `e`") == [
        ("a ", {}),
        ("b ", {"bold": True}),
        ("c", {"bold": True, "italic": True}),
        (" d", {"bold": True}),
        (" ", {}),
        ("d", {"link": {"url": "http://x"}}),
        (" ", {}),
        ("e", {"weightedFontFamily": {"fontFamily": MONOSPACE}}),
    ]


def test_unmatched_and_escaped_markers_stay_literal():
    assert parse_inline("2 * 3 and snake_case_name") == [("2 * 3 and snake_case_name", {})]
    assert parse_inline(r"\*not italic\*") == [("*not italic*", {})]


def test_blocks():
    paras = parse_blocks("# Title\n\nline one\nline two\n\n- a\n  - b\n1. c\n\n```\nx = 1\n```")
    assert [(p.style, p.list_kind, p.level, p.text) for p in paras] == [
        ("HEADING_1", None, 0, "Title"),
        ("NORMAL_TEXT", None, 0, "line one line two"),
        ("NORMAL_TEXT", "bullet", 0, "a"),
        ("NORMAL_TEXT", "bullet", 1, "b"),
        ("NORMAL_TEXT", "number", 0, "c"),
        ("NORMAL_TEXT", None, 0, "x = 1"),
    ]


def test_empty_markdown_is_refused():
    with pytest.raises(ValueError, match="empty"):
        markdown_to_requests("  \n\n", 5)


def test_paragraph_mode_inserts_once_resets_then_styles():
    reqs = markdown_to_requests("## Head\nsome **bold**", 10, "paragraph", "t.1")
    assert _kinds(reqs) == [
        "insertText", "deleteParagraphBullets", "updateParagraphStyle", "updateTextStyle",
        "updateParagraphStyle", "updateTextStyle",
    ]
    assert reqs[0]["insertText"] == {"text": "Head\nsome bold\n", "location": {"index": 10, "tabId": "t.1"}}
    # Reset covers exactly the inserted text, in the tab.
    assert reqs[2]["updateParagraphStyle"]["range"] == {"startIndex": 10, "endIndex": 25, "tabId": "t.1"}
    assert reqs[3]["updateTextStyle"]["fields"].startswith("bold,italic")
    heading = reqs[4]["updateParagraphStyle"]
    assert heading["paragraphStyle"] == {"namedStyleType": "HEADING_2"}
    assert (heading["range"]["startIndex"], heading["range"]["endIndex"]) == (10, 15)
    assert _text_styles(reqs) == [(20, 24, {"bold": True})]
    assert reqs[5]["updateTextStyle"]["fields"] == "bold"


def test_offsets_count_emoji_as_two_units():
    reqs = markdown_to_requests("👋 **x**", 1, "paragraph")
    assert _text_styles(reqs) == [(4, 5, {"bold": True})]  # 1 + emoji(2) + space(1)


def test_end_mode_prefixes_a_newline_and_drops_the_trailing_one():
    reqs = markdown_to_requests("# New", 30, "end")
    assert reqs[0]["insertText"]["text"] == "\nNew"
    # Paragraph styling starts after the prefix, so the existing last paragraph is untouched,
    # and ends on the document's own final newline.
    rng = reqs[2]["updateParagraphStyle"]["range"]
    assert (rng["startIndex"], rng["endIndex"]) == (31, 35)


def test_end_empty_mode_fills_the_empty_last_paragraph():
    reqs = markdown_to_requests("a\n\nb", 30, "end_empty")
    assert reqs[0]["insertText"]["text"] == "a\nb"


def test_inline_mode_never_restyles_paragraphs():
    reqs = markdown_to_requests("# not *really*", 7, "inline")
    assert reqs[0]["insertText"]["text"] == "not really"
    assert not any(k in ("updateParagraphStyle", "deleteParagraphBullets", "createParagraphBullets")
                   for k in _kinds(reqs))
    assert _text_styles(reqs) == [(11, 17, {"italic": True})]


def test_lists_group_and_bullets_come_last_in_reverse_order():
    reqs = markdown_to_requests("- a\n  - b\n\npara\n\n1. c", 1, "paragraph")
    assert reqs[0]["insertText"]["text"] == "a\n\tb\npara\nc\n"
    bullets = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    assert _kinds(reqs)[-2:] == ["createParagraphBullets", "createParagraphBullets"]
    # Later list first: its tab-stripping can't shift the earlier range.
    assert [(b["range"]["startIndex"], b["range"]["endIndex"], b["bulletPreset"]) for b in bullets] == [
        (11, 13, "NUMBERED_DECIMAL_ALPHA_ROMAN"),
        (1, 6, "BULLET_DISC_CIRCLE_SQUARE"),
    ]


def test_adjacent_lists_of_different_kinds_stay_separate():
    reqs = markdown_to_requests("- a\n- b\n\n1. c\n2. d", 1, "paragraph")
    bullets = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    assert [(b["range"]["startIndex"], b["range"]["endIndex"], b["bulletPreset"]) for b in bullets] == [
        (5, 9, "NUMBERED_DECIMAL_ALPHA_ROMAN"),
        (1, 5, "BULLET_DISC_CIRCLE_SQUARE"),
    ]


def test_inline_splice_keeps_surrounding_spaces():
    reqs = markdown_to_requests(" and **more** ", 7, "inline")
    assert reqs[0]["insertText"]["text"] == " and more "
    assert _text_styles(reqs) == [(12, 16, {"bold": True})]


def test_quote_becomes_an_indented_paragraph():
    paras = parse_blocks("before\n> quoted line one\n> line two\n>\n> second\nafter")
    assert [(p.text, p.indent) for p in paras] == [
        ("before", False), ("quoted line one line two", True), ("second", True), ("after", False),
    ]
    reqs = markdown_to_requests("> Indented.", 1, "paragraph")
    indents = [r["updateParagraphStyle"] for r in reqs
               if "updateParagraphStyle" in r and "indentStart" in r["updateParagraphStyle"]["paragraphStyle"]]
    assert indents == [{
        "range": {"startIndex": 1, "endIndex": 11},
        "paragraphStyle": {"indentStart": {"magnitude": 36, "unit": "PT"},
                           "indentFirstLine": {"magnitude": 36, "unit": "PT"}},
        "fields": "indentStart,indentFirstLine",
    }]
    # The reset clears inherited indents on everything inserted.
    assert "indentStart" in reqs[2]["updateParagraphStyle"]["fields"]
