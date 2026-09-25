"""docs_model (0.13.0): outline, section spans, UTF-16 offsets, tabs, quotes."""

from __future__ import annotations

import pytest
from docbuilder import SAMPLE, make_body, make_doc

from google_workspace_mcp import docs_model as m


def test_utf16_len_counts_surrogate_pairs():
    assert m.utf16_len("a") == 1
    assert m.utf16_len("👋") == 2
    assert m.utf16_len("é中") == 2


def test_outline_spans_nest_and_stop_before_the_final_newline():
    body = make_body(*SAMPLE)
    assert m.body_end(body) == 68
    got = [(s.level, s.heading, s.heading_start, s.body_start, s.end) for s in m.outline(body)]
    assert got == [
        (0, "Plan", 1, 6, 67),
        (1, "Intro", 6, 12, 54),  # runs through its H2s, up to the next H1
        (2, "Scope", 27, 33, 43),
        (2, "Risks", 43, 49, 54),
        (1, "Budget", 54, 61, 67),  # last section: final newline excluded
    ]


def test_emoji_shifts_later_indices_by_two():
    body = make_body(("NORMAL_TEXT", "👋"), ("HEADING_1", "After"))
    assert m.outline(body)[0].heading_start == 4  # 1 + 2 (emoji) + 1 (newline)


def test_find_section_ignores_case_hashes_and_spacing():
    sections = m.outline(make_body(*SAMPLE))
    assert m.find_section(sections, "##  scope ").heading == "Scope"


def test_find_section_missing_lists_headings():
    with pytest.raises(ValueError, match=r"No heading matches 'Nope'.*Intro"):
        m.find_section(m.outline(make_body(*SAMPLE)), "Nope")


def test_find_section_refuses_to_guess_between_duplicates():
    body = make_body(("HEADING_2", "Notes"), ("NORMAL_TEXT", "a"), ("HEADING_2", "Notes"))
    with pytest.raises(ValueError, match="2 headings match"):
        m.find_section(m.outline(body), "notes")


def test_blank_heading_paragraphs_are_not_sections():
    body = make_body(("HEADING_1", ""), ("HEADING_1", "Real"))
    assert [s.heading for s in m.outline(body)] == ["Real"]


def test_structural_in_range_finds_tables_and_images():
    body = make_body(("HEADING_1", "A"), ("table", [["x", "y"]]), ("HEADING_1", "B"), ("image", "cap"))
    a, b = m.outline(body)
    assert m.structural_in_range(body, a.body_start, a.end) == "table"
    assert m.structural_in_range(body, b.body_start, b.end) == "image"
    assert m.structural_in_range(body, a.heading_start, a.body_start) is None


def test_resolve_tab_defaults_to_first_and_rejects_unknown():
    doc = make_doc(tabs={"t.0": make_body(("NORMAL_TEXT", "zero")), "t.1": make_body(("NORMAL_TEXT", "one"))})
    assert m.resolve_tab(doc, None)["tab_id"] == "t.0"
    assert m.plain_text(m.resolve_tab(doc, "t.1")["body"]) == "one\n"
    with pytest.raises(ValueError, match="No tab 't.9'.*t.1"):
        m.resolve_tab(doc, "t.9")


def test_flatten_tabs_walks_child_tabs_and_untabbed_docs():
    doc = {"tabs": [{
        "tabProperties": {"tabId": "a", "title": "A"},
        "childTabs": [{"tabProperties": {"tabId": "b", "title": "B"}}],
    }]}
    assert [(t["tab_id"], t["depth"]) for t in m.flatten_tabs(doc)] == [("a", 0), ("b", 1)]
    assert m.flatten_tabs({"body": {"content": []}})[0]["tab_id"] is None


def test_locate_quote_maps_to_document_index_and_section():
    body = make_body(*SAMPLE)
    at = m.locate_quote(body, "world")
    assert at == 21  # 12 + "Hello " (6) + emoji (2) + space (1)
    assert m.section_path(m.outline(body), at) == ["Plan", "Intro"]
    assert m.section_path(m.outline(body), m.locate_quote(body, "few")) == ["Plan", "Intro", "Risks"]


def test_locate_quote_tolerates_whitespace_and_searches_tables():
    body = make_body(("NORMAL_TEXT", "one"), ("NORMAL_TEXT", "two"), ("table", [["cell text"]]))
    assert m.locate_quote(body, "one  two") == 1
    assert m.locate_quote(body, "cell text") is not None
    assert m.locate_quote(body, "absent") is None


# ─── chips (docs_insert_chip's @-mentions, dates, rich links) ────────────


def _chip_body(el: dict) -> dict:
    """A one-paragraph body whose only element is `el` (plus its newline)."""
    return {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        {"startIndex": 1, "endIndex": 3, "paragraph": {"elements": [
            el, {"startIndex": 2, "textRun": {"content": "\n"}},
        ]}},
    ]}


def test_chip_text_renders_person_richlink_and_date():
    person = {"person": {"personProperties": {"name": "Ada Lovelace", "email": "ada@x.com"}}}
    assert m.chip_text(person) == "@Ada Lovelace <ada@x.com>"
    assert m.chip_text({"person": {"personProperties": {"email": "ada@x.com"}}}) == "ada@x.com"
    link = {"richLink": {"richLinkProperties": {"title": "Q3 Plan", "uri": "https://docs.google.com/x"}}}
    assert m.chip_text(link) == "[Q3 Plan](https://docs.google.com/x)"
    assert m.chip_text({"richLink": {"richLinkProperties": {"uri": "https://docs.google.com/x"}}}) == \
        "[https://docs.google.com/x](https://docs.google.com/x)"
    date = {"dateElement": {"dateElementProperties": {"displayText": "Sep 25, 2026", "timestamp": "2026-09-25T00:00:00Z"}}}
    assert m.chip_text(date) == "Sep 25, 2026"
    assert m.chip_text({"textRun": {"content": "plain"}}) is None


def test_plain_text_and_para_text_render_chips_instead_of_dropping_them():
    body = _chip_body({"startIndex": 1, "person": {"personProperties": {"name": "Ada", "email": "ada@x.com"}}})
    assert m.plain_text(body) == "@Ada <ada@x.com>\n"
    assert m.paragraphs(body)[0]["text"] == "@Ada <ada@x.com>"


def test_locate_quote_sees_chip_text():
    # chip_text renders as "[Budget](https://docs.google.com/y)"; the '['
    # sits at the chip's own document index, "Budget" one char after it.
    body = _chip_body({"startIndex": 1, "richLink": {"richLinkProperties": {
        "title": "Budget", "uri": "https://docs.google.com/y"}}})
    assert m.locate_quote(body, "Budget") == 2


def test_find_inline_object_locates_element_range_in_body_and_table():
    body = make_body(("image", "cap"))
    got = m.find_inline_object(body["content"], "img")
    assert got == (1, 2)
    assert m.find_inline_object(body["content"], "nope") is None

    table_body = make_body(("table", [["x"]]))
    table_body["content"][1]["table"]["tableRows"][0]["tableCells"][0]["content"][0]["paragraph"]["elements"].append(
        {"startIndex": 99, "endIndex": 100, "inlineObjectElement": {"inlineObjectId": "cell-img"}}
    )
    assert m.find_inline_object(table_body["content"], "cell-img") == (99, 100)


def test_insertion_mode():
    body = make_body(("NORMAL_TEXT", "ab"), ("NORMAL_TEXT", "cd"))  # 1..4, 4..7, end 7
    assert m.insertion_mode(body, 4) == "paragraph"
    assert m.insertion_mode(body, 2) == "inline"
    assert m.insertion_mode(body, 6) == "end"
    assert m.insertion_mode(make_body(("NORMAL_TEXT", "ab"), ("NORMAL_TEXT", "")), 4) == "end_empty"


def test_find_all_returns_utf16_ranges():
    body = make_body(("NORMAL_TEXT", "👋 ab AB ab"))
    assert m.find_all(body, "ab") == [(4, 6), (10, 12)]
    assert len(m.find_all(body, "ab", match_case=False)) == 3
    assert m.find_all(body, "") == []


def test_flatten_tabs_includes_headers_footers_footnotes():
    doc = {"tabs": [{
        "tabProperties": {"tabId": "a", "title": "A"},
        "documentTab": {
            "body": {"content": []},
            "headers": {"h1": {"content": []}},
            "footers": {"f1": {"content": []}},
            "footnotes": {"fn1": {"content": []}},
        },
    }]}
    t = m.flatten_tabs(doc)[0]
    assert t["headers"] == {"h1": {"content": []}}
    assert t["footers"] == {"f1": {"content": []}}
    assert t["footnotes"] == {"fn1": {"content": []}}

    untabbed = m.flatten_tabs({
        "body": {"content": []}, "headers": {"h2": {"content": []}}, "footers": {}, "footnotes": {},
    })[0]
    assert untabbed["headers"] == {"h2": {"content": []}}


_FOOTNOTE_ELEMENTS = [
    {"textRun": {"content": "Hello "}},
    {"footnoteReference": {"footnoteId": "fn1", "footnoteNumber": "1"}},
    {"textRun": {"content": " world\n"}},
]


def test_footnote_references_in_reading_order():
    content = [{"paragraph": {"elements": _FOOTNOTE_ELEMENTS}}]
    assert m.footnote_references(content) == [("fn1", "1")]


def test_footnote_references_searches_tables():
    content = [{"table": {"tableRows": [{"tableCells": [
        {"content": [{"paragraph": {"elements": _FOOTNOTE_ELEMENTS}}]},
    ]}]}}]
    assert m.footnote_references(content) == [("fn1", "1")]


def test_plain_text_with_footnotes_renders_bracket_markers():
    content = [{"paragraph": {"elements": _FOOTNOTE_ELEMENTS}}]
    assert m.plain_text_with_footnotes(content) == "Hello [1] world\n"


def test_has_pending_suggestions_finds_nested_keys():
    assert m.has_pending_suggestions({"a": {"b": [{"suggestedInsertionIds": ["s1"]}]}})
    assert m.has_pending_suggestions({"a": [{"suggestedDeletionIds": ["s1"]}]})
    assert not m.has_pending_suggestions({"a": {"b": [1, "text", {"c": True}]}})
