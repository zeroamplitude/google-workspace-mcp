"""Build Docs API `documents.get` JSON with real index arithmetic, for tests."""

from __future__ import annotations

from google_workspace_mcp.docs_model import utf16_len


def make_body(*paras) -> dict:
    """Each item: ("HEADING_1" | "NORMAL_TEXT" | ..., "text"), or ("table", [["cell", ...], ...]),
    or ("image", "caption"). Every paragraph gets its trailing newline."""
    content = [{"endIndex": 1, "sectionBreak": {}}]
    idx = 1
    for kind, value in paras:
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
        text = value + "\n"
        elements.append({"startIndex": idx, "textRun": {"content": text}})
        idx += utf16_len(text)
        content.append({
            "startIndex": start,
            "endIndex": idx,
            "paragraph": {"elements": elements, "paragraphStyle": {"namedStyleType": kind}},
        })
    return {"content": content}


def make_doc(*paras, tabs: dict | None = None, revision: str = "rev-1") -> dict:
    """A tabbed document. `tabs` maps tab_id -> body; default is one tab 't.0'."""
    tabs = tabs or {"t.0": make_body(*paras)}
    return {
        "documentId": "doc-1",
        "title": "Doc",
        "revisionId": revision,
        "tabs": [
            {"tabProperties": {"tabId": tid, "title": tid.upper()}, "documentTab": {"body": body}}
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
