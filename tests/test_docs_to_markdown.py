"""docs_to_markdown: a Docs API tab body -> the Markdown subset docs_markdown writes."""

from __future__ import annotations

from docbuilder import make_body

from google_workspace_mcp.docs_markdown import parse_blocks
from google_workspace_mcp.docs_to_markdown import body_to_markdown

BULLETS = {
    "list1": {"listProperties": {"nestingLevels": [
        {"glyphSymbol": "●"}, {"glyphSymbol": "○"},
    ]}},
}
NUMBERS = {
    "list2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
}


def _bullet(list_id="list1", level=0):
    return {"listId": list_id, "nestingLevel": level}


def test_headings_and_title():
    body = make_body(("TITLE", "Plan"), ("HEADING_1", "Intro"), ("HEADING_3", "Deep"))
    assert body_to_markdown(body, {}) == "# Plan\n\n# Intro\n\n### Deep"


def test_plain_paragraphs_stay_separate_blocks():
    body = make_body(("NORMAL_TEXT", "one"), ("NORMAL_TEXT", "two"))
    md = body_to_markdown(body, {})
    assert md == "one\n\ntwo"
    paras = parse_blocks(md)
    assert [p.text for p in paras] == ["one", "two"]  # would merge into one without the blank line


def test_run_styles_bold_italic_strikethrough_code_link():
    body = make_body(("NORMAL_TEXT", [
        ("bold", {"bold": True}),
        (" ", {}),
        ("italic", {"italic": True}),
        (" ", {}),
        ("both", {"bold": True, "italic": True}),
        (" ", {}),
        ("gone", {"strikethrough": True}),
        (" ", {}),
        ("code", {"weightedFontFamily": {"fontFamily": "Roboto Mono"}}),
        (" ", {}),
        ("link", {"link": {"url": "http://x"}}),
    ]))
    md = body_to_markdown(body, {})
    assert md == "**bold** *italic* **_both_** ~~gone~~ `code` [link](http://x)"


def test_adjacent_runs_with_identical_style_merge():
    body = make_body(("NORMAL_TEXT", [("bo", {"bold": True}), ("ld", {"bold": True})]))
    assert body_to_markdown(body, {}) == "**bold**"


def test_whitespace_only_run_is_not_wrapped_in_markers():
    body = make_body(("NORMAL_TEXT", [("a", {"bold": True}), (" ", {"bold": True}), ("b", {})]))
    assert body_to_markdown(body, {}) == "**a** b"


def test_literal_markdown_characters_are_escaped():
    body = make_body(("NORMAL_TEXT", "2 * 3 and `x` and _y_"))
    md = body_to_markdown(body, {})
    assert md == r"2 \* 3 and \`x\` and \_y\_"
    assert parse_blocks(md)[0].text == "2 * 3 and `x` and _y_"


def test_code_text_is_not_escaped_inside_the_backticks():
    body = make_body(("NORMAL_TEXT", [("a*b", {"weightedFontFamily": {"fontFamily": "Consolas"}})]))
    assert body_to_markdown(body, {}) == "`a*b`"


def test_monospace_fonts_recognized_as_code():
    for font in ("Roboto Mono", "Courier New", "Consolas", "Source Code Pro"):
        body = make_body(("NORMAL_TEXT", [("x", {"weightedFontFamily": {"fontFamily": font}})]))
        assert body_to_markdown(body, {}) == "`x`"
    body = make_body(("NORMAL_TEXT", [("x", {"weightedFontFamily": {"fontFamily": "Georgia"}})]))
    assert body_to_markdown(body, {}) == "x"


def test_bulleted_list_and_nesting():
    body = make_body(
        ("NORMAL_TEXT", "a", {"bullet": _bullet("list1", 0)}),
        ("NORMAL_TEXT", "b", {"bullet": _bullet("list1", 1)}),
    )
    md = body_to_markdown(body, BULLETS)
    assert md == "- a\n  - b"
    paras = parse_blocks(md)
    assert [(p.list_kind, p.level, p.text) for p in paras] == [("bullet", 0, "a"), ("bullet", 1, "b")]


def test_numbered_list_glyph_type():
    body = make_body(
        ("NORMAL_TEXT", "first", {"bullet": _bullet("list2", 0)}),
        ("NORMAL_TEXT", "second", {"bullet": _bullet("list2", 0)}),
    )
    md = body_to_markdown(body, NUMBERS)
    assert md == "1. first\n1. second"
    paras = parse_blocks(md)
    assert [(p.list_kind, p.text) for p in paras] == [("number", "first"), ("number", "second")]


def test_glyph_type_unspecified_or_none_is_still_a_bullet():
    lists = {"l": {"listProperties": {"nestingLevels": [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"}]}}}
    body = make_body(("NORMAL_TEXT", "a", {"bullet": _bullet("l", 0)}))
    assert parse_blocks(body_to_markdown(body, lists))[0].list_kind == "bullet"


def test_list_with_no_matching_entry_defaults_to_bullet():
    body = make_body(("NORMAL_TEXT", "a", {"bullet": _bullet("missing", 0)}))
    assert parse_blocks(body_to_markdown(body, {}))[0].list_kind == "bullet"


def test_list_and_heading_and_paragraph_together_round_trip_block_shape():
    body = make_body(
        ("HEADING_1", "Todo"),
        ("NORMAL_TEXT", "a", {"bullet": _bullet("list1", 0)}),
        ("NORMAL_TEXT", "b", {"bullet": _bullet("list1", 0)}),
        ("NORMAL_TEXT", "after"),
    )
    md = body_to_markdown(body, BULLETS)
    paras = parse_blocks(md)
    assert [(p.style, p.list_kind, p.level) for p in paras] == [
        ("HEADING_1", None, 0),
        ("NORMAL_TEXT", "bullet", 0),
        ("NORMAL_TEXT", "bullet", 0),
        ("NORMAL_TEXT", None, 0),
    ]
    assert [p.text for p in paras] == ["Todo", "a", "b", "after"]


def test_indented_paragraph_becomes_a_quote():
    body = make_body(("NORMAL_TEXT", "before"), ("NORMAL_TEXT", "quoted", {"indent": True}), ("NORMAL_TEXT", "after"))
    md = body_to_markdown(body, {})
    assert md == "before\n\n> quoted\n\nafter"
    paras = parse_blocks(md)
    assert [(p.text, p.indent) for p in paras] == [("before", False), ("quoted", True), ("after", False)]


def test_two_separate_indented_paragraphs_stay_separate():
    body = make_body(("NORMAL_TEXT", "one", {"indent": True}), ("NORMAL_TEXT", "two", {"indent": True}))
    md = body_to_markdown(body, {})
    paras = parse_blocks(md)
    assert [p.text for p in paras] == ["one", "two"]  # not merged into "one two"


def test_inline_image():
    body = make_body(("image", " caption"))
    assert body_to_markdown(body, {}) == "![image](img) caption"


def test_table_becomes_a_github_pipe_table():
    body = make_body(("table", [["a", "b"], ["c | d", "e"]]))
    md = body_to_markdown(body, {})
    assert md == "| a | b |\n| --- | --- |\n| c \\| d | e |"


def test_empty_paragraphs_and_blank_headings_are_skipped():
    body = make_body(("NORMAL_TEXT", "a"), ("NORMAL_TEXT", ""), ("HEADING_1", ""), ("NORMAL_TEXT", "b"))
    assert body_to_markdown(body, {}) == "a\n\nb"


def test_round_trip_recovers_paragraph_styles_and_list_kinds():
    body = make_body(
        ("TITLE", "Plan"),
        ("HEADING_2", "Scope"),
        ("NORMAL_TEXT", "para one"),
        ("NORMAL_TEXT", "a", {"bullet": _bullet("list1", 0)}),
        ("NORMAL_TEXT", "b", {"bullet": _bullet("list1", 1)}),
        ("NORMAL_TEXT", "first", {"bullet": _bullet("list2", 0)}),
        ("NORMAL_TEXT", "quoted", {"indent": True}),
    )
    lists = {**BULLETS, **NUMBERS}
    md = body_to_markdown(body, lists)
    paras = parse_blocks(md)
    assert [(p.style, p.list_kind, p.level, p.indent) for p in paras] == [
        ("HEADING_1", None, 0, False),  # TITLE has no markdown equivalent below H1; "#" parses as HEADING_1
        ("HEADING_2", None, 0, False),
        ("NORMAL_TEXT", None, 0, False),
        ("NORMAL_TEXT", "bullet", 0, False),
        ("NORMAL_TEXT", "bullet", 1, False),
        ("NORMAL_TEXT", "number", 0, False),
        ("NORMAL_TEXT", None, 0, True),
    ]
