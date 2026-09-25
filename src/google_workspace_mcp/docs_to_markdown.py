"""Turn a Docs API tab body back into the Markdown subset `docs_markdown` writes.

The point is a stable read -> edit -> write round trip: `docs_get(format=
"markdown")` should hand back text that `docs_insert` / `docs_replace_section`
can re-ingest without losing structure. So this sticks to exactly the subset
`docs_markdown` supports (see its module docstring) wherever the two need to
agree — headings, bold/italic/code/links, bulleted/numbered lists (nested by
two-space indent), and indented paragraphs as `> ` quotes. Tables, images and
strikethrough don't round-trip through `docs_markdown` (it has no writer for
them); they're still rendered, as a GitHub pipe table, `![image](id)`, and
`~~text~~` respectively, for a human or Claude to read.

Every Doc paragraph becomes its own block, blank-line separated, except runs
of consecutive list items (kept one-per-line) since `docs_markdown` matches
list markers per line regardless of blank lines, while any other paragraph
type would merge with its neighbour without one.
"""

from __future__ import annotations

import re

_MONO_FONTS = {"Roboto Mono", "Courier New", "Consolas", "Source Code Pro"}
_ESCAPE_RE = re.compile(r"([\\*_`])")


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


# ─── inline ─────────────────────────────────────────────────────────────


def _style_key(style: dict) -> tuple:
    """(bold, italic, strikethrough, code, link url) — the merge/render key."""
    font = (style.get("weightedFontFamily") or {}).get("fontFamily")
    return (
        bool(style.get("bold")),
        bool(style.get("italic")),
        bool(style.get("strikethrough")),
        font in _MONO_FONTS,
        (style.get("link") or {}).get("url"),
    )


def _render_run(text: str, key: tuple) -> str:
    if not text or text.isspace():
        return text  # never wrap whitespace-only runs in markers
    bold, italic, strike, code, link = key
    # Leading/trailing whitespace stays outside the markers too, so a run
    # like "bold " (bold=True) doesn't render as the ambiguous "**bold **".
    lead = text[: len(text) - len(text.lstrip())]
    core = text.strip()
    trail = text[len(lead) + len(core):]
    if code:
        inner = f"`{core}`"  # backtick content isn't re-parsed; leave it unescaped
    else:
        inner = _escape(core)
        if strike:
            inner = f"~~{inner}~~"
        if bold and italic:
            inner = f"**_{inner}_**"
        elif bold:
            inner = f"**{inner}**"
        elif italic:
            inner = f"*{inner}*"
    if link:
        inner = f"[{inner}]({link})"
    return lead + inner + trail


def _raw_runs(elements: list[dict]) -> list[list]:
    """[text, style, is_image] per element, with the paragraph's trailing
    newline stripped off the last text run."""
    out: list[list] = []
    for e in elements:
        if "textRun" in e:
            out.append([e["textRun"].get("content", ""), e["textRun"].get("textStyle", {}) or {}, False])
        elif "inlineObjectElement" in e:
            oid = e["inlineObjectElement"].get("inlineObjectId", "")
            out.append([f"![image]({oid})", {}, True])
        # horizontalRule, footnoteReference, columnBreak, ... : skipped
    if out and not out[-1][2] and out[-1][0].endswith("\n"):
        out[-1][0] = out[-1][0][:-1]
    return out


def _para_inline_md(elements: list[dict]) -> str:
    """Runs merged by identical style, each rendered and concatenated."""
    segments: list[str] = []
    buf_text, buf_key = "", None

    def flush() -> None:
        nonlocal buf_text, buf_key
        if buf_text:
            segments.append(_render_run(buf_text, buf_key))
        buf_text, buf_key = "", None

    for text, style, is_image in _raw_runs(elements):
        if is_image:
            flush()
            segments.append(text)
            continue
        key = _style_key(style)
        if buf_key is not None and key != buf_key:
            flush()
        buf_text += text
        buf_key = key
    flush()
    return "".join(segments)


# ─── lists ──────────────────────────────────────────────────────────────


def _list_kind(lists: dict, list_id: str | None, level: int) -> str:
    """"bullet" or "number", from lists[list_id].listProperties.nestingLevels[level]."""
    levels = ((lists or {}).get(list_id or "") or {}).get("listProperties", {}).get("nestingLevels", [])
    if not levels:
        return "bullet"
    props = levels[level] if 0 <= level < len(levels) else levels[-1]
    glyph_type = props.get("glyphType")
    if glyph_type and glyph_type not in ("GLYPH_TYPE_UNSPECIFIED", "NONE"):
        return "number"
    return "bullet"


# ─── blocks ─────────────────────────────────────────────────────────────


def _paragraph_block(p: dict, lists: dict) -> tuple[str | None, bool]:
    """(markdown line, is_list_item) for one Docs paragraph, or (None, False) to skip it."""
    text = _para_inline_md(p.get("elements", []))
    bullet = p.get("bullet")
    if bullet:
        level = bullet.get("nestingLevel", 0)
        marker = "1. " if _list_kind(lists, bullet.get("listId"), level) == "number" else "- "
        return "  " * level + marker + text, True
    if not text:
        return None, False
    style = p.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
    if style == "TITLE":
        return "# " + text, False
    if style.startswith("HEADING_"):
        return "#" * int(style.removeprefix("HEADING_")) + " " + text, False
    indent = (p.get("paragraphStyle", {}).get("indentStart") or {}).get("magnitude", 0)
    if indent > 0:
        return "> " + text, False
    return text, False


def _table_md(table: dict) -> str:
    """A GitHub pipe table; the first row doubles as the header row."""
    grid = []
    for row in table.get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            parts = [
                _para_inline_md(c["paragraph"]["elements"])
                for c in cell.get("content", [])
                if "paragraph" in c
            ]
            cells.append(" ".join(t for t in parts if t).replace("|", "\\|"))
        grid.append(cells)
    if not grid:
        return ""
    ncols = max(len(r) for r in grid)

    def line(row: list[str]) -> str:
        return "| " + " | ".join(row + [""] * (ncols - len(row))) + " |"

    return "\n".join([line(grid[0]), "| " + " | ".join(["---"] * ncols) + " |", *(line(r) for r in grid[1:])])


def body_to_markdown(body: dict, lists: dict) -> str:
    """A tab body -> Markdown. `lists` is `tab.documentTab.lists` (or
    `doc.lists` for an untabbed document) — see `docs_model.flatten_tabs`."""
    blocks: list[tuple[str, bool]] = []
    for el in body.get("content", []):
        if "paragraph" in el:
            line, is_list = _paragraph_block(el["paragraph"], lists or {})
            if line is not None:
                blocks.append((line, is_list))
        elif "table" in el:
            t = _table_md(el["table"])
            if t:
                blocks.append((t, False))
        # sectionBreak / tableOfContents / horizontal rule at top level: skipped

    out: list[str] = []
    prev_list = False
    for i, (text, is_list) in enumerate(blocks):
        if i == 0:
            out.append(text)
        elif is_list and prev_list:
            out.append("\n" + text)  # consecutive list items: no blank line needed
        else:
            out.append("\n\n" + text)
        prev_list = is_list
    return "".join(out)
