"""Read a Google Docs API document into the shapes the docs_* tools need.

Pure functions over the JSON `documents.get` returns — no API calls — so the
index math can be tested without Google.

Two facts drive everything here:
- Docs indices count UTF-16 code units, not Python characters. An emoji is
  one Python character but two index units; every offset derived from a
  Python string goes through `utf16_len`.
- Indices are per tab. A document fetched with `includeTabsContent=True`
  keeps each tab's body under `tabs[].documentTab.body`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Elements a section rewrite would delete along with the text. Refused rather
# than silently dropped; the caller can remove them deliberately by range.
_STRUCTURAL = ("table", "tableOfContents", "sectionBreak")


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def norm(s: str) -> str:
    """Heading comparison key: leading '#'s, case and whitespace don't matter."""
    return " ".join(s.lstrip("#").split()).casefold()


# ─── tabs ───────────────────────────────────────────────────────────────


def flatten_tabs(doc: dict) -> list[dict]:
    """Every tab, child tabs included, in reading order."""
    out: list[dict] = []

    def walk(tabs: list[dict], depth: int) -> None:
        for t in tabs:
            props = t.get("tabProperties", {})
            doc_tab = t.get("documentTab", {})
            out.append({
                "tab_id": props.get("tabId"),
                "title": props.get("title"),
                "depth": depth,
                "body": doc_tab.get("body", {}),
                "lists": doc_tab.get("lists", {}),
            })
            walk(t.get("childTabs", []), depth + 1)

    walk(doc.get("tabs", []), 0)
    if not out:  # fetched without includeTabsContent: one implicit tab
        out.append({
            "tab_id": None, "title": None, "depth": 0,
            "body": doc.get("body", {}), "lists": doc.get("lists", {}),
        })
    return out


def resolve_tab(doc: dict, tab_id: str | None) -> dict:
    """The requested tab, or the first one (Google's own default)."""
    tabs = flatten_tabs(doc)
    if tab_id is None:
        return tabs[0]
    for t in tabs:
        if t["tab_id"] == tab_id:
            return t
    known = ", ".join(f"{t['tab_id']} ({t['title']})" for t in tabs)
    raise ValueError(f"No tab {tab_id!r} in this document. Tabs: {known}")


# ─── paragraphs & text ──────────────────────────────────────────────────


def _para_text(p: dict) -> str:
    return "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", []))


def body_end(body: dict) -> int:
    content = body.get("content", [])
    return content[-1]["endIndex"] if content else 1


def paragraphs(body: dict) -> list[dict]:
    """Top-level paragraphs (not inside tables) with their index ranges."""
    out = []
    for el in body.get("content", []):
        p = el.get("paragraph")
        if p is None:
            continue
        out.append({
            "start_index": el.get("startIndex", 0),
            "end_index": el["endIndex"],
            "style": p.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT"),
            "bullet": "bullet" in p,
            "text": _para_text(p).rstrip("\n"),
        })
    return out


def text_runs(content: list[dict]) -> list[tuple[int, str]]:
    """(start index, text) for every text run, table cells included."""
    runs: list[tuple[int, str]] = []
    for el in content:
        if "paragraph" in el:
            for e in el["paragraph"].get("elements", []):
                if "textRun" in e:
                    runs.append((e.get("startIndex", 0), e["textRun"].get("content", "")))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    runs.extend(text_runs(cell.get("content", [])))
    return runs


def plain_text(body: dict) -> str:
    return "".join(t for _, t in text_runs(body.get("content", [])))


def _char_map(body: dict) -> tuple[list[str], list[int]]:
    """Every character of the body's text runs, and each one's document index."""
    chars: list[str] = []
    where: list[int] = []
    for start, text in text_runs(body.get("content", [])):
        idx = start
        for ch in text:
            chars.append(ch)
            where.append(idx)
            idx += utf16_len(ch)
    return chars, where


def locate_quote(body: dict, quote: str) -> int | None:
    """Document index where `quote` first appears, or None.

    Matching is whitespace-tolerant: a comment's quoted text may span a
    paragraph break that Drive reports as a space, or vice versa.
    """
    words = quote.split()
    if not words:
        return None
    chars, where = _char_map(body)
    # Collapse whitespace, keeping the index map aligned.
    squashed: list[str] = []
    squashed_at: list[int] = []
    for ch, at in zip(chars, where):
        if ch.isspace():
            if squashed and squashed[-1] == " ":
                continue
            ch = " "
        squashed.append(ch)
        squashed_at.append(at)
    hay = "".join(squashed).casefold()
    pos = hay.find(" ".join(words).casefold())
    return None if pos < 0 else squashed_at[pos]


def find_all(body: dict, needle: str, match_case: bool = True) -> list[tuple[int, int]]:
    """[start, end) document ranges of every occurrence of `needle`."""
    if not needle:
        return []
    chars, where = _char_map(body)
    if match_case:
        hay = "".join(chars)
    else:  # lower per character, so positions in `hay` stay aligned with `chars`
        hay = "".join(c.lower() if len(c.lower()) == 1 else c for c in chars)
        needle = needle.lower()
    out = []
    pos = hay.find(needle)
    while pos >= 0:
        last = pos + len(needle) - 1
        out.append((where[pos], where[last] + utf16_len(chars[last])))
        pos = hay.find(needle, pos + len(needle))
    return out


# ─── outline ────────────────────────────────────────────────────────────


@dataclass
class Section:
    level: int  # 0 = TITLE, 1-6 = HEADING_n
    heading: str
    heading_start: int  # the heading paragraph itself
    body_start: int  # first index after the heading paragraph
    end: int  # exclusive end of the section's deletable content
    contains_end: int  # exclusive end for "is this index in the section"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "heading": self.heading,
            "start_index": self.heading_start,
            "body_start_index": self.body_start,
            "end_index": self.end,
        }


def _level(style: str) -> int | None:
    if style == "TITLE":
        return 0
    if style.startswith("HEADING_"):
        return int(style.removeprefix("HEADING_"))
    return None


def outline(body: dict) -> list[Section]:
    """Every heading, with the span of its section.

    A section runs to the next heading of the same or a higher level (so an
    H1's section includes its H2s). The last section stops one short of the
    body's end: a body's final newline can never be deleted.
    """
    end = body_end(body)
    heads = [
        (lvl, p)
        for p in paragraphs(body)
        if (lvl := _level(p["style"])) is not None and p["text"].strip()
    ]
    out = []
    for i, (lvl, p) in enumerate(heads):
        stop = next((q["start_index"] for l2, q in heads[i + 1:] if l2 <= lvl), None)
        out.append(Section(
            level=lvl,
            heading=p["text"].strip(),
            heading_start=p["start_index"],
            body_start=p["end_index"],
            end=stop if stop is not None else end - 1,
            contains_end=stop if stop is not None else end,
        ))
    return out


def find_section(sections: list[Section], heading: str) -> Section:
    """The one section whose heading matches. Never guesses between several."""
    hits = [s for s in sections if norm(s.heading) == norm(heading)]
    if len(hits) == 1:
        return hits[0]
    listing = "; ".join(f"{'#' * max(s.level, 1)} {s.heading} (@{s.heading_start})" for s in sections)
    if not hits:
        raise ValueError(f"No heading matches {heading!r}. Headings: {listing or '(none)'}")
    raise ValueError(
        f"{len(hits)} headings match {heading!r}; pass an explicit index instead. Headings: {listing}"
    )


def section_path(sections: list[Section], index: int) -> list[str]:
    """Headings (outermost first) whose sections contain `index`."""
    return [s.heading for s in sections if s.heading_start <= index < s.contains_end]


def structural_in_range(body: dict, start: int, end: int) -> str | None:
    """Name of the first table / TOC / section break / image in [start, end)."""
    for el in body.get("content", []):
        s, e = el.get("startIndex", 0), el["endIndex"]
        if e <= start or s >= end:
            continue
        for kind in _STRUCTURAL:
            if kind in el:
                return kind
        for pe in el.get("paragraph", {}).get("elements", []):
            if "inlineObjectElement" in pe:
                return "image"
    return None


def insertion_mode(body: dict, index: int) -> str:
    """How text inserted at `index` joins the paragraphs around it.

    - "paragraph": index is the start of a paragraph; new text ends in a
      newline so it becomes whole paragraphs before that one.
    - "end": the body's last position (before the undeletable final
      newline); new text needs a leading newline if that paragraph has text.
    - "end_empty": as "end", but the last paragraph is empty, so the new
      text fills it directly.
    - "inline": mid-paragraph; text is spliced in without paragraph styling.
    """
    end = body_end(body)
    paras = paragraphs(body)
    if index == end - 1:
        last = paras[-1] if paras else None
        return "end_empty" if last is None or not last["text"] else "end"
    if any(p["start_index"] == index for p in paras):
        return "paragraph"
    return "inline"
