"""Build Docs API `documents.get` JSON with real index arithmetic, for tests."""

from __future__ import annotations

from google_workspace_mcp.docs_model import utf16_len


def make_body(*paras) -> dict:
    """Each item: ("HEADING_1" | "NORMAL_TEXT" | ..., "text"), or ("table", [["cell", ...], ...]),
    or ("image", "caption"). Every paragraph gets its trailing newline.

    `value` may also be a list of (text, textStyle) runs instead of a plain
    string, for multi-styled paragraphs. A third, optional item is an
    `extra` dict: {"bullet": {"listId": ..., "nestingLevel": ...}} adds a
    list bullet, {"indent": True} sets a nonzero indentStart (a `> ` quote)."""
    content = [{"endIndex": 1, "sectionBreak": {}}]
    idx = 1
    for item in paras:
        kind, value, *rest = item
        extra = rest[0] if rest else {}
        if kind == "table":
            start = idx
            idx += 1  # table start
            rows = []
            for row in value:
                idx += 1  # row start
                cells = []
                for cell in row:
                    idx += 1  # cell start
                    text = cell + "\n"
                    cells.append({"content": [{
                        "startIndex": idx,
                        "endIndex": idx + utf16_len(text),
                        "paragraph": {"elements": [{"startIndex": idx, "textRun": {"content": text}}]},
                    }]})
                    idx += utf16_len(text)
                rows.append({"tableCells": cells})
            idx += 1  # table end
            content.append({"startIndex": start, "endIndex": idx, "table": {"tableRows": rows}})
            continue
        start = idx
        elements = []
        if kind == "image":
            elements.append({"startIndex": idx, "inlineObjectElement": {"inlineObjectId": "img"}})
            idx += 1
            kind = "NORMAL_TEXT"
        runs = list(value) if isinstance(value, list) else [(value, {})]
        text, style = runs[-1]
        runs[-1] = (text + "\n", style)
        for text, style in runs:
            el = {"startIndex": idx, "textRun": {"content": text}}
            if style:
                el["textRun"]["textStyle"] = style
            elements.append(el)
            idx += utf16_len(text)
        para_style = {"namedStyleType": kind}
        if extra.get("indent"):
            para_style["indentStart"] = {"magnitude": 36, "unit": "PT"}
        paragraph = {"elements": elements, "paragraphStyle": para_style}
        if "bullet" in extra:
            paragraph["bullet"] = extra["bullet"]
        content.append({"startIndex": start, "endIndex": idx, "paragraph": paragraph})
    return {"content": content}


def make_doc(*paras, tabs: dict | None = None, revision: str = "rev-1", lists: dict | None = None) -> dict:
    """A tabbed document. `tabs` maps tab_id -> body; default is one tab 't.0'.
    `lists` (a listId -> list dict, as in `tab.documentTab.lists`) is attached
    to every tab, for tests that read bulleted/numbered paragraphs back."""
    tabs = tabs or {"t.0": make_body(*paras)}
    return {
        "documentId": "doc-1",
        "title": "Doc",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tid, "title": tid.upper()},
                "documentTab": {"body": body, **({"lists": lists} if lists else {})},
            }
            for tid, body in tabs.items()
        ],
    }


SAMPLE = (
    ("TITLE", "Plan"),
    ("HEADING_1", "Intro"),
    ("NORMAL_TEXT", "Hello 👋 world"),
    ("HEADING_2", "Scope"),
    ("NORMAL_TEXT", "In scope."),
    ("HEADING_2", "Risks"),
    ("NORMAL_TEXT", "Few."),
    ("HEADING_1", "Budget"),
    ("NORMAL_TEXT", "Cheap."),
)
