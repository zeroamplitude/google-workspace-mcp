"""Turn a small Markdown subset into Docs API batchUpdate requests.

Supported: `#`–`######` headings, paragraphs (consecutive lines join),
**bold** / __bold__, *italic* / _italic_, `code`, [text](url), `-`/`*`/`+`
bullets, `1.` numbered lists (nested by two-space indent), `- [ ]` / `- [x]`
checklists (also `*`/`+`), and ``` fenced code blocks (monospace), and `>`
quotes (an indented paragraph). Anything else is inserted literally.

The text goes in with a single insertText; every other request only styles
it, so all ranges are computed against the post-insert document. The one
exception is createParagraphBullets, which strips the leading tabs that set
nesting levels and so shifts later indices — those requests go last, in
reverse document order, where no later range depends on them.

GitHub-style pipe tables aren't part of that single insert: `insertTable`
is its own API call that has to be re-fetched to find the table it made
before cells can be filled, so `split_markdown_tables` below pulls table
blocks out of the Markdown for server.py to insert separately, in sequence
with the surrounding text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .docs_model import utf16_len

MONOSPACE = "Roboto Mono"
_BULLET_PRESET = {
    "bullet": "BULLET_DISC_CIRCLE_SQUARE",
    "number": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "check": "BULLET_CHECKBOX",
}
# Text-style fields cleared on everything inserted, so new text doesn't
# inherit the bold/link/font of whatever it was typed next to.
_RESET_TEXT_FIELDS = "bold,italic,underline,strikethrough,link,weightedFontFamily"


@dataclass
class Para:
    runs: list[tuple[str, dict]] = field(default_factory=list)  # (text, textStyle)
    style: str = "NORMAL_TEXT"
    list_kind: str | None = None  # "bullet" | "number"
    level: int = 0
    indent: bool = False  # from a > quote

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.runs)


# ─── inline ─────────────────────────────────────────────────────────────

_INLINE = [
    ("code", re.compile(r"`([^`]+)`")),
    ("link", re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")),
    ("bold", re.compile(r"\*\*(.+?)\*\*|__(.+?)__")),
    ("italic", re.compile(r"\*(?!\s)(.+?)(?<!\s)\*|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")),
]


def parse_inline(s: str, style: dict | None = None) -> list[tuple[str, dict]]:
    """Split one line into (text, textStyle) runs; nested styles combine."""
    style = style or {}
    runs: list[tuple[str, dict]] = []
    i = 0
    buf = ""
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "\\`*_[]()#":
            buf += s[i + 1]
            i += 2
            continue
        for kind, rx in _INLINE:
            m = rx.match(s, i)
            if not m:
                continue
            if buf:
                runs.append((buf, style))
                buf = ""
            if kind == "code":
                runs.append((m.group(1), {**style, "weightedFontFamily": {"fontFamily": MONOSPACE}}))
            elif kind == "link":
                runs.extend(parse_inline(m.group(1), {**style, "link": {"url": m.group(2)}}))
            else:
                inner = m.group(1) if m.group(1) is not None else m.group(2)
                runs.extend(parse_inline(inner, {**style, kind: True}))
            i = m.end()
            break
        else:
            buf += s[i]
            i += 1
    if buf:
        runs.append((buf, style))
    return runs


# ─── blocks ─────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_CHECK = re.compile(r"^( *)[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^( *)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^( *)\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*```")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_INDENT = {"magnitude": 36, "unit": "PT"}  # half an inch, the Docs "Increase indent" step


def parse_blocks(md: str) -> list[Para]:
    paras: list[Para] = []
    pending: list[str] = []  # lines of the paragraph being gathered
    quoted = False  # whether `pending` came from > lines

    def flush() -> None:
        if pending:
            paras.append(Para(runs=parse_inline(" ".join(pending)), indent=quoted))
            pending.clear()

    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].expandtabs(4)
        if _FENCE.match(line):
            flush()
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                code = lines[i]
                paras.append(Para(runs=[(code or " ", {"weightedFontFamily": {"fontFamily": MONOSPACE}})]))
                i += 1
            i += 1  # closing fence
            continue
        q = _QUOTE.match(line)
        if pending and bool(q) != quoted:
            flush()  # moving into or out of a quote ends the paragraph
        quoted = bool(q)
        if q:
            line = q.group(1)
        if not line.strip():
            flush()
        elif m := _HEADING.match(line):
            flush()
            if m.group(2):
                paras.append(Para(runs=parse_inline(m.group(2)), style=f"HEADING_{len(m.group(1))}"))
        elif m := _CHECK.match(line):
            flush()
            if m.group(3).strip():
                # The Docs API has no way to set a checkbox's checked state (createParagraphBullets
                # only picks the BULLET_CHECKBOX glyph); strikethrough is the visible "done" marker.
                checked_style = {"strikethrough": True} if m.group(2) in "xX" else None
                paras.append(
                    Para(runs=parse_inline(m.group(3), checked_style), list_kind="check", level=len(m.group(1)) // 2)
                )
        elif (m := _BULLET.match(line)) or (m := _NUMBER.match(line)):
            flush()
            kind = "bullet" if m.re is _BULLET else "number"
            if m.group(2).strip():
                paras.append(Para(runs=parse_inline(m.group(2)), list_kind=kind, level=len(m.group(1)) // 2))
        else:
            pending.append(line.strip())
        i += 1
    flush()
    return paras


# ─── tables ─────────────────────────────────────────────────────────────

_ROW = re.compile(r"^[ \t]*\|(.*)\|[ \t]*$")
_SEP_CELL = re.compile(r"^:?-+:?$")


def _split_row(line: str) -> list[str]:
    """A `| a | b |` line's cells, `\\|` treated as a literal pipe."""
    inner = _ROW.match(line).group(1)
    cells: list[str] = []
    buf = ""
    i = 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] == "|":
            buf += "|"
            i += 2
        elif inner[i] == "|":
            cells.append(buf.strip())
            buf = ""
            i += 1
        else:
            buf += inner[i]
            i += 1
    cells.append(buf.strip())
    return cells


def split_markdown_tables(md: str) -> list[tuple[str, object]]:
    """Split `md` into ("text", str) and ("table", rows) segments, in order.

    A table is a GitHub-style pipe table: a `| a | b |` header row, a
    `|---|---|` separator row of the same width (each cell just dashes,
    optionally `:`-anchored), then zero or more further rows. `rows[0]` is
    the header; data rows are padded/truncated to the header's width.
    """
    lines = md.replace("\r\n", "\n").split("\n")
    segments: list[tuple[str, object]] = []
    text_buf: list[str] = []

    def flush() -> None:
        if text_buf:
            segments.append(("text", "\n".join(text_buf)))
            text_buf.clear()

    i, n = 0, len(lines)
    while i < n:
        header = _ROW.match(lines[i])
        sep = _ROW.match(lines[i + 1]) if i + 1 < n else None
        if header and sep:
            head_cells = _split_row(lines[i])
            sep_cells = _split_row(lines[i + 1])
            if len(sep_cells) == len(head_cells) and all(_SEP_CELL.match(c) for c in sep_cells):
                flush()
                width = len(head_cells)
                rows = [head_cells]
                j = i + 2
                while j < n and _ROW.match(lines[j]):
                    cells = _split_row(lines[j])
                    cells = (cells + [""] * width)[:width]
                    rows.append(cells)
                    j += 1
                segments.append(("table", rows))
                i = j
                continue
        text_buf.append(lines[i])
        i += 1
    flush()
    return segments


# ─── requests ───────────────────────────────────────────────────────────


def _range(start: int, end: int, tab_id: str | None) -> dict:
    r = {"startIndex": start, "endIndex": end}
    if tab_id:
        r["tabId"] = tab_id
    return r


def _fields(style: dict) -> str:
    return ",".join(style)


def markdown_to_requests(md: str, index: int, mode: str = "paragraph", tab_id: str | None = None) -> list[dict]:
    """Requests that insert `md` at `index`.

    `mode` comes from docs_model.insertion_mode: "paragraph" (index starts a
    paragraph), "end" / "end_empty" (the body's final position), or
    "inline" (mid-paragraph: text styles only, no paragraph restyling).
    """
    paras = parse_blocks(md)
    if not paras:
        raise ValueError("Nothing to insert: the markdown is empty.")
    inline = mode == "inline"
    if inline and len(paras) == 1 and paras[0].list_kind is None and paras[0].style == "NORMAL_TEXT":
        # Splicing into a sentence: the spaces around the text are part of it.
        lead, trail = md[: len(md) - len(md.lstrip(" "))], md[len(md.rstrip(" ")):]
        paras[0].runs = ([(lead, {})] if lead else []) + paras[0].runs + ([(trail, {})] if trail else [])

    lines = [("\t" * p.level if p.list_kind and not inline else "") + p.text for p in paras]
    prefix = "\n" if mode == "end" else ""
    text = prefix + "\n".join(lines) + ("\n" if mode == "paragraph" else "")

    loc = {"index": index}
    if tab_id:
        loc["tabId"] = tab_id
    reqs: list[dict] = [{"insertText": {"text": text, "location": loc}}]

    first = index + utf16_len(prefix)
    last_end = index + utf16_len(text)

    # Paragraph index ranges ([start, end-including-newline)) and styled spans.
    para_ranges: list[tuple[int, int]] = []
    spans: list[tuple[int, int, dict]] = []
    pos = first
    for p, line in zip(paras, lines):
        start = pos
        pos += utf16_len(line) - utf16_len(p.text)  # leading tabs
        for t, style in p.runs:
            n = utf16_len(t)
            if style:
                spans.append((pos, pos + n, style))
            pos += n
        para_ranges.append((start, pos + 1))
        pos += 1

    if not inline:
        whole = _range(first, para_ranges[-1][1], tab_id)
        reqs.append({"deleteParagraphBullets": {"range": whole}})
        reqs.append({
            "updateParagraphStyle": {
                "range": whole,
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType,indentStart,indentFirstLine",
            }
        })
    if last_end > first:
        reqs.append({
            "updateTextStyle": {"range": _range(first, last_end, tab_id), "textStyle": {}, "fields": _RESET_TEXT_FIELDS}
        })

    if not inline:
        for p, (s, e) in zip(paras, para_ranges):
            if p.indent:
                reqs.append({
                    "updateParagraphStyle": {
                        "range": _range(s, e, tab_id),
                        "paragraphStyle": {"indentStart": _INDENT, "indentFirstLine": _INDENT},
                        "fields": "indentStart,indentFirstLine",
                    }
                })
            if p.style != "NORMAL_TEXT":
                reqs.append({
                    "updateParagraphStyle": {
                        "range": _range(s, e, tab_id),
                        "paragraphStyle": {"namedStyleType": p.style},
                        "fields": "namedStyleType",
                    }
                })
    for s, e, style in spans:
        reqs.append({"updateTextStyle": {"range": _range(s, e, tab_id), "textStyle": style, "fields": _fields(style)}})

    if not inline:
        # Group consecutive list items; the first item's kind sets the preset.
        groups: list[tuple[str, int, int]] = []
        for p, (s, e) in zip(paras, para_ranges):
            if p.list_kind is None:
                continue
            # A new top-level item of another kind starts a new list.
            same = groups and groups[-1][2] == s and (p.level > 0 or groups[-1][0] == p.list_kind)
            if same:
                kind, gs, _ = groups[-1]
                groups[-1] = (kind, gs, e)
            else:
                groups.append((p.list_kind, s, e))
        for kind, s, e in reversed(groups):
            reqs.append({"createParagraphBullets": {"range": _range(s, e, tab_id), "bulletPreset": _BULLET_PRESET[kind]}})
    return reqs
