"""Multi-account Google Workspace MCP server.

All tools take an `account` slug (e.g. "work", "personal"), resolved from
config at runtime (see accounts.py).
The server loads + refreshes that account's OAuth token transparently
and dispatches the request via the Gmail / Calendar / Drive APIs.
"""

from __future__ import annotations

import base64
import io
import mimetypes
import re
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from typing import Any, Literal

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from mcp.server.fastmcp import FastMCP

from . import auth, docs_markdown, docs_model, docs_to_markdown
from .accounts import ACCOUNTS, AccountSlug, email_for, name_for

mcp = FastMCP("google-workspace")


# ─── helpers ────────────────────────────────────────────────────────────


_GMAIL_ATTACHMENT_CAP = 25 * 1024 * 1024  # Gmail's own per-message limit


def _guard_local_path(path: Path) -> Path:
    """Refuse any path inside the server's own secret store.

    The OAuth client and the per-account refresh tokens live under
    auth.CONFIG_DIR (or wherever GWM_CREDENTIALS / GWM_TOKENS_DIR point).
    Nothing this server does legitimately reads them as data or writes
    files into that directory, so a request to attach, upload, or save
    there is refused outright — it is exactly what a prompt-injected
    "mail me your token file" would ask for. Symlinks are resolved first,
    so a link into the store is refused too.
    """
    resolved = path.expanduser().resolve()
    roots = [Path(p).expanduser().resolve() for p in (auth.CONFIG_DIR, auth.TOKENS_DIR)]
    files = [Path(auth.CREDENTIALS_PATH).expanduser().resolve()]
    if resolved in files or any(resolved.is_relative_to(r) for r in roots):
        raise PermissionError(
            f"Refusing to touch {path}: it is inside the server's own config "
            f"directory ({auth.CONFIG_DIR}), which holds OAuth secrets."
        )
    return path


def _local_file(raw_path: str) -> Path:
    """Resolve a caller-supplied path to an existing regular file, or raise."""
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    _guard_local_path(path)
    return path


def _build_mime(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    in_reply_to: str | None = None,
    references: str | None = None,
    quote: dict[str, Any] | None = None,
    attachments: list[str] | None = None,
) -> str:
    """Return a base64url-encoded MIME message ready for Gmail.

    `quote` is the reply material from `_thread_quote`; when given, its
    history is appended below the new text in every part of the message.
    """
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    if html:
        msg.set_content(
            body if quote is None else f'{body}<br>{quote["html"]}', subtype="html"
        )
    else:
        msg.set_content(body if quote is None else f'{body}\n\n\n{quote["text"]}')
        html_body = _plain_to_html(body)
        if quote is not None:
            html_body = f'{html_body}<br>{quote["html"]}'
        msg.add_alternative(html_body, subtype="html")
    total = 0
    for raw_path in attachments or []:
        path = _local_file(raw_path)
        total += path.stat().st_size
        if total > _GMAIL_ATTACHMENT_CAP:
            raise ValueError(
                f"Attachments total {total / 1_048_576:.1f} MB; Gmail caps a "
                "message at 25 MB. Upload the file to Drive and share a link "
                "instead (drive_file_upload + drive_file_link_access)."
            )
        ctype, encoding = mimetypes.guess_type(path.name)
        # No type, a compressed type (.gz), or a container type (message/*,
        # multipart/*) all go as opaque bytes: RFC 2046 forbids base64 on
        # message/rfc822, so a base64'd .eml is unreadable to the recipient.
        container = ctype is not None and ctype.split("/")[0] in ("message", "multipart")
        if ctype is None or encoding is not None or container:
            ctype = "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


_BULLET_RE = re.compile(r"^[-*\u2022]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^([0-9\u06f0-\u06f9\u0660-\u0669]{1,3})[.)]\s+(.*)$")
_DIGITS = str.maketrans("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
                        "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
                        "01234567890123456789")


def _plain_to_html(body: str) -> str:
    """Render a plain-text body the way Gmail's own composer does.

    Regular lines become one <div> per line (blank lines <div><br></div>);
    runs of "- " / "* " / "\u2022 " lines become a real <ul>, and runs of
    "1. " / "1) " lines (ASCII, Persian, or Arabic-Indic digits) a real <ol>
    with the list marker Gmail itself would draw. dir="auto" throughout so
    LTR and RTL lines each lay out correctly, markers included.

    Attached as the text/html alternative of every plain-text message so a
    draft opened in the Gmail web UI keeps its rich-text shape. Without it
    Gmail treats the draft as plain-text-only and, on Send, rewrites the body
    with hard line breaks at ~70 columns — visibly broken paragraphs for the
    recipient. Gmail-composed mail never shows this because it is always
    multipart/alternative and clients display the HTML part.
    """
    out: list[str] = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if _BULLET_RE.match(lines[i]):
            items = []
            while i < len(lines) and (m := _BULLET_RE.match(lines[i])):
                items.append(f'<li dir="auto">{escape(m.group(1))}</li>')
                i += 1
            out.append(f'<ul dir="auto">{"".join(items)}</ul>')
        elif m := _NUMBERED_RE.match(lines[i]):
            start = int(m.group(1).translate(_DIGITS))
            items = []
            while i < len(lines) and (m := _NUMBERED_RE.match(lines[i])):
                items.append(f'<li dir="auto">{escape(m.group(2))}</li>')
                i += 1
            attr = f' start="{start}"' if start != 1 else ""
            out.append(f'<ol dir="auto"{attr}>{"".join(items)}</ol>')
        else:
            line = lines[i]
            out.append(
                f'<div dir="auto">{escape(line)}</div>' if line.strip() else '<div dir="auto"><br></div>'
            )
            i += 1
    return "".join(out)


_QUOTE_BQ_STYLE = (
    "margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex"
)


def _extract_html_body(part: dict) -> str | None:
    """Return the first text/html body in a Gmail payload tree, decoded."""
    if part.get("mimeType") == "text/html":
        data = (part.get("body") or {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode(
                "utf-8", errors="replace"
            )
    for sub in part.get("parts", []) or []:
        html_body = _extract_html_body(sub)
        if html_body:
            return html_body
    return None


def _quote_when(date_header: str) -> str:
    """Format a Date header the way Gmail writes it in an attribution line.

    "Mon, Aug 24, 2026 at 12:24 PM", with the narrow no-break space (U+202F)
    Gmail puts before AM/PM. Rendered in the message's own UTC offset, which
    is what the header carries; falls back to the raw header if unparseable.
    """
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return date_header
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%a}, {dt:%b} {dt.day}, {dt.year} at {hour}:{dt.minute:02d}\u202f{ampm}"


def _quote_prefix(text: str) -> str:
    """Prefix each line with Gmail's "> ", deepening already-quoted lines."""
    return "\n".join(
        (">" + line) if line.startswith(">") else f"> {line}"
        for line in text.split("\n")
    )


def _thread_quote(account: str, thread_id: str) -> dict[str, Any] | None:
    """Build reply material from the newest sent or received message of a thread.

    Returns the RFC822 Message-Id and References chain needed for correct
    threading, plus that message quoted in both flavours: "> "-prefixed text
    and a Gmail-style nested <blockquote class="gmail_quote">.

    Quoting only the newest message is enough to reproduce the whole thread:
    it already carries every earlier message nested inside it, exactly as
    Gmail's web Reply builds it. Drafts are skipped: they are not part of the
    conversation, and a reply draft is itself the newest message of its thread.
    Returns None when the thread cannot be read (bad id, deleted, missing
    scope) or holds nothing but drafts, so a send degrades to an unquoted reply
    instead of failing outright.
    """
    try:
        thread = (
            auth.gmail(account)
            .users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except HttpError:
        return None
    # Quoting a draft sent the recipient an unsent text and pointed In-Reply-To
    # at a message they never received: gmail_draft_update with thread_id
    # re-quoted the draft's own earlier version under the new body.
    messages = [
        m
        for m in thread.get("messages") or []
        if "DRAFT" not in (m.get("labelIds") or [])
    ]
    if not messages:
        return None
    payload = messages[-1].get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    when = _quote_when(headers.get("date", ""))
    who = f"{name} " if name else ""
    prev_text = _extract_plain_body(payload) or ""
    prev_html = _extract_html_body(payload) or _plain_to_html(prev_text)
    message_id = headers.get("message-id")
    references = " ".join(
        ref for ref in (headers.get("references"), message_id) if ref
    )
    attr_text = f"On {when} {who}<{addr}> wrote:"
    attr_html = (
        f'<div dir="ltr" class="gmail_attr">On {escape(when)} {escape(who)}'
        f'&lt;<a href="mailto:{escape(addr, quote=True)}">{escape(addr)}</a>&gt;'
        " wrote:<br></div>"
    )
    return {
        "message_id": message_id,
        "references": references or None,
        "text": f"{attr_text}\n{_quote_prefix(prev_text)}" if prev_text else attr_text,
        "html": (
            '<div class="gmail_quote gmail_quote_container">'
            f'{attr_html}<blockquote class="gmail_quote" style="{_QUOTE_BQ_STYLE}">'
            f"{prev_html}</blockquote></div>"
        ),
    }


def _from_header(slug: str) -> str:
    """Build an RFC-5322 From header, e.g. 'Your Name <you@example.com>'.

    Falls back to the bare address if the account has no display name.
    """
    name = name_for(slug)
    email = email_for(slug)
    return formataddr((name, email)) if name else email


def _msg_summary(m: dict) -> dict:
    """Trim a Gmail message to what callers actually need."""
    headers = {
        h["name"].lower(): h["value"]
        for h in (m.get("payload") or {}).get("headers", [])
    }
    return {
        "id": m.get("id"),
        "threadId": m.get("threadId"),
        "labelIds": m.get("labelIds", []),
        "snippet": m.get("snippet"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
    }


# ─── meta ───────────────────────────────────────────────────────────────


@mcp.tool()
def accounts_list() -> dict:
    """List the configured Google accounts and whether each token still works.

    `authorized` is proven by refreshing each token against Google (null
    if Google could not be reached), not by the token file existing; a
    dead account carries a `status` and a `detail` naming the fix.
    """
    out: dict[str, dict] = {}
    for slug, info in ACCOUNTS.items():
        out[slug] = {
            "email": info["email"],
            "name": info.get("name", ""),
            **auth.token_status(slug),
        }
    if not out:
        return {
            "accounts": {},
            "hint": (
                "No accounts configured. Set GWM_ACCOUNTS (a JSON map or a "
                "comma-list of slugs from your registry) or create "
                f"{auth.CONFIG_DIR / 'accounts.json'}, then run "
                "google-workspace-authorize <slug> <email>. See the README."
            ),
        }
    return {"accounts": out}


# ─── Gmail ──────────────────────────────────────────────────────────────


@mcp.tool()
def gmail_send(
    account: AccountSlug,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    thread_id: str | None = None,
    in_reply_to_message_id: str | None = None,
    quote_history: bool = True,
    attachments: list[str] | None = None,
) -> dict:
    """Send an email immediately from the given account.

    Replies: pass thread_id and nothing else. The server reads the thread and
    quotes its history below your text exactly as Gmail's web Reply does, and
    derives the In-Reply-To / References headers itself. So write `body` as
    ONLY the new message — never paste earlier messages into it by hand, or
    the recipient gets the history twice. (`in_reply_to_message_id` overrides
    the derived header when you already hold the RFC822 Message-Id;
    `quote_history=false` sends into the thread with no quote.)

    Body format: write `body` as plain text — blank-line paragraphs,
    "- " bullets, "1." / "1)" numbered lines (ASCII or Persian digits).
    It goes out as multipart/alternative with a Gmail-composer-style HTML
    part, so lists arrive as Gmail's real bullets/numbering and the draft
    can be opened and sent from the Gmail web UI safely. Never hard-wrap
    lines yourself. Set html=true only for a body that is already HTML.

    `attachments` are paths on the machine running this server. Each is
    attached under its own file name, with the MIME type guessed from that
    name and `application/octet-stream` as the fallback.
    """
    ctx = _thread_quote(account, thread_id) if thread_id else None
    raw = _build_mime(
        sender=_from_header(account),
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        in_reply_to=in_reply_to_message_id or (ctx or {}).get("message_id"),
        references=(ctx or {}).get("references"),
        quote=ctx if quote_history else None,
        attachments=attachments,
    )
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = (
        auth.gmail(account)
        .users()
        .messages()
        .send(userId="me", body=payload)
        .execute()
    )
    return _msg_summary(sent)


@mcp.tool()
def gmail_draft_create(
    account: AccountSlug,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    thread_id: str | None = None,
    quote_history: bool = True,
    attachments: list[str] | None = None,
) -> dict:
    """Create a Gmail draft. Returns {id, message: {...}}.

    Replies: pass thread_id and nothing else. The server reads the thread and
    quotes its history below your text exactly as Gmail's web Reply does, and
    derives the In-Reply-To / References headers itself. So write `body` as
    ONLY the new message — never paste earlier messages into it by hand, or
    the recipient gets the history twice. (`in_reply_to_message_id` overrides
    the derived header when you already hold the RFC822 Message-Id;
    `quote_history=false` sends into the thread with no quote.)

    Body format: write `body` as plain text — blank-line paragraphs,
    "- " bullets, "1." / "1)" numbered lines (ASCII or Persian digits).
    It goes out as multipart/alternative with a Gmail-composer-style HTML
    part, so lists arrive as Gmail's real bullets/numbering and the draft
    can be opened and sent from the Gmail web UI safely. Never hard-wrap
    lines yourself. Set html=true only for a body that is already HTML.

    `attachments` are paths on the machine running this server. Each is
    attached under its own file name, with the MIME type guessed from that
    name and `application/octet-stream` as the fallback.
    """
    ctx = _thread_quote(account, thread_id) if thread_id else None
    raw = _build_mime(
        sender=_from_header(account),
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        in_reply_to=(ctx or {}).get("message_id"),
        references=(ctx or {}).get("references"),
        quote=ctx if quote_history else None,
        attachments=attachments,
    )
    msg: dict[str, Any] = {"raw": raw}
    if thread_id:
        msg["threadId"] = thread_id
    draft = (
        auth.gmail(account)
        .users()
        .drafts()
        .create(userId="me", body={"message": msg})
        .execute()
    )
    return {"id": draft.get("id"), "message": _msg_summary(draft.get("message", {}))}


@mcp.tool()
def gmail_draft_update(
    account: AccountSlug,
    draft_id: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    thread_id: str | None = None,
    quote_history: bool = True,
    attachments: list[str] | None = None,
) -> dict:
    """Overwrite an existing draft's contents.

    Pass thread_id when the draft is a reply — it keeps the draft attached to
    that thread (an update without it detaches the draft) and re-quotes the
    thread's history, so `body` stays just the new text.

    Body format: write `body` as plain text — blank-line paragraphs,
    "- " bullets, "1." / "1)" numbered lines (ASCII or Persian digits).
    It goes out as multipart/alternative with a Gmail-composer-style HTML
    part, so lists arrive as Gmail's real bullets/numbering and the draft
    can be opened and sent from the Gmail web UI safely. Never hard-wrap
    lines yourself. Set html=true only for a body that is already HTML.

    `attachments` are paths on the machine running this server. Each is
    attached under its own file name, with the MIME type guessed from that
    name and `application/octet-stream` as the fallback.
    """
    ctx = _thread_quote(account, thread_id) if thread_id else None
    raw = _build_mime(
        sender=_from_header(account),
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        in_reply_to=(ctx or {}).get("message_id"),
        references=(ctx or {}).get("references"),
        quote=ctx if quote_history else None,
        attachments=attachments,
    )
    msg: dict[str, Any] = {"raw": raw}
    if thread_id:
        msg["threadId"] = thread_id
    draft = (
        auth.gmail(account)
        .users()
        .drafts()
        .update(userId="me", id=draft_id, body={"message": msg})
        .execute()
    )
    return {"id": draft.get("id"), "message": _msg_summary(draft.get("message", {}))}


@mcp.tool()
def gmail_draft_send(account: AccountSlug, draft_id: str) -> dict:
    """Send an existing draft."""
    sent = (
        auth.gmail(account)
        .users()
        .drafts()
        .send(userId="me", body={"id": draft_id})
        .execute()
    )
    return _msg_summary(sent)


@mcp.tool()
def gmail_draft_delete(account: AccountSlug, draft_id: str) -> dict:
    """Permanently delete a draft (the thing the default connector can't do)."""
    auth.gmail(account).users().drafts().delete(userId="me", id=draft_id).execute()
    return {"deleted": draft_id}


@mcp.tool()
def gmail_drafts_list(
    account: AccountSlug,
    max_results: int = 20,
    query: str | None = None,
) -> dict:
    """List drafts. `query` uses standard Gmail search syntax."""
    resp = (
        auth.gmail(account)
        .users()
        .drafts()
        .list(userId="me", maxResults=max_results, q=query)
        .execute()
    )
    drafts = resp.get("drafts", []) or []
    return {"drafts": drafts, "next_page_token": resp.get("nextPageToken")}


@mcp.tool()
def gmail_search(
    account: AccountSlug,
    query: str,
    max_results: int = 20,
    label_ids: list[str] | None = None,
    include_spam_trash: bool = False,
) -> dict:
    """Search messages with Gmail's query syntax (e.g. 'from:foo subject:bar')."""
    svc = auth.gmail(account).users().messages()
    resp = svc.list(
        userId="me",
        q=query,
        maxResults=max_results,
        labelIds=label_ids,
        includeSpamTrash=include_spam_trash,
    ).execute()
    ids = [m["id"] for m in (resp.get("messages") or [])]
    # Bulk-fetch metadata so the caller can see who/what/when without
    # a second round trip per message.
    out = []
    for mid in ids:
        m = svc.get(userId="me", id=mid, format="metadata").execute()
        out.append(_msg_summary(m))
    return {"messages": out, "next_page_token": resp.get("nextPageToken")}


@mcp.tool()
def gmail_message_get(
    account: AccountSlug,
    message_id: str,
    format: Literal["full", "metadata", "minimal", "raw"] = "full",
) -> dict:
    """Fetch one message. `format=full` includes the body."""
    m = (
        auth.gmail(account)
        .users()
        .messages()
        .get(userId="me", id=message_id, format=format)
        .execute()
    )
    if format == "full":
        # Decode the plain-text body if present, for convenience.
        body_text = _extract_plain_body(m.get("payload") or {})
        return {**_msg_summary(m), "body_text": body_text, "raw": m}
    return {**_msg_summary(m), "raw": m}


def _extract_plain_body(part: dict) -> str | None:
    if part.get("mimeType") == "text/plain":
        data = (part.get("body") or {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode(
                "utf-8", errors="replace"
            )
    for sub in part.get("parts", []) or []:
        text = _extract_plain_body(sub)
        if text:
            return text
    return None


@mcp.tool()
def gmail_message_trash(account: AccountSlug, message_id: str) -> dict:
    """Move a message to Trash (reversible for 30 days)."""
    m = (
        auth.gmail(account)
        .users()
        .messages()
        .trash(userId="me", id=message_id)
        .execute()
    )
    return _msg_summary(m)


@mcp.tool()
def gmail_message_modify(
    account: AccountSlug,
    message_id: str,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> dict:
    """Add/remove labels on a message (e.g. mark read by removing UNREAD)."""
    body = {
        "addLabelIds": add_label_ids or [],
        "removeLabelIds": remove_label_ids or [],
    }
    m = (
        auth.gmail(account)
        .users()
        .messages()
        .modify(userId="me", id=message_id, body=body)
        .execute()
    )
    return _msg_summary(m)


@mcp.tool()
def gmail_labels_list(account: AccountSlug) -> dict:
    """List all labels for the account."""
    resp = auth.gmail(account).users().labels().list(userId="me").execute()
    return {"labels": resp.get("labels", [])}


@mcp.tool()
def gmail_label_create(
    account: AccountSlug,
    name: str,
    label_list_visibility: Literal[
        "labelShow", "labelShowIfUnread", "labelHide"
    ] = "labelShow",
    message_list_visibility: Literal["show", "hide"] = "show",
) -> dict:
    """Create a label. Nested labels use 'Parent/Child' names; create each
    parent level first. Idempotent: an existing label is returned as-is."""
    svc = auth.gmail(account).users().labels()
    body = {
        "name": name,
        "labelListVisibility": label_list_visibility,
        "messageListVisibility": message_list_visibility,
    }
    try:
        label = svc.create(userId="me", body=body).execute()
    except HttpError as e:
        if e.resp.status != 409:
            raise
        existing = svc.list(userId="me").execute().get("labels", [])
        label = next(lb for lb in existing if lb.get("name") == name)
        return {**label, "already_existed": True}
    return label


@mcp.tool()
def gmail_thread_get(account: AccountSlug, thread_id: str) -> dict:
    """Fetch a whole thread (all messages)."""
    t = (
        auth.gmail(account)
        .users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    return {
        "id": t.get("id"),
        "messages": [_msg_summary(m) for m in t.get("messages", [])],
    }


@mcp.tool()
def gmail_attachment_download(
    account: AccountSlug,
    message_id: str,
    attachment_id: str,
    save_to: str,
) -> dict:
    """Download one attachment to a local path. Get `attachment_id` from
    the message payload parts (gmail_message_get with format=full)."""
    path = _guard_local_path(Path(save_to).expanduser())
    att = (
        auth.gmail(account)
        .users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = base64.urlsafe_b64decode(att["data"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"saved_to": str(path), "size_bytes": len(data)}


# ─── Calendar ───────────────────────────────────────────────────────────


@mcp.tool()
def calendar_list(account: AccountSlug) -> dict:
    """List all calendars the account can access."""
    resp = auth.calendar(account).calendarList().list().execute()
    cals = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "primary": c.get("primary", False),
            "accessRole": c.get("accessRole"),
            "timeZone": c.get("timeZone"),
        }
        for c in resp.get("items", [])
    ]
    return {"calendars": cals}


@mcp.tool()
def calendar_events_list(
    account: AccountSlug,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = 25,
    single_events: bool = True,
) -> dict:
    """List events. `time_min`/`time_max` are RFC3339 (e.g. '2026-06-05T00:00:00-07:00')."""
    resp = (
        auth.calendar(account)
        .events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            q=query,
            maxResults=max_results,
            singleEvents=single_events,
            orderBy="startTime" if single_events else None,
        )
        .execute()
    )
    return {
        "events": resp.get("items", []),
        "next_page_token": resp.get("nextPageToken"),
    }


@mcp.tool()
def calendar_event_get(
    account: AccountSlug,
    event_id: str,
    calendar_id: str = "primary",
) -> dict:
    """Fetch one event."""
    return (
        auth.calendar(account)
        .events()
        .get(calendarId=calendar_id, eventId=event_id)
        .execute()
    )


@mcp.tool()
def calendar_event_create(
    account: AccountSlug,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    send_updates: Literal["all", "externalOnly", "none"] = "none",
    add_google_meet: bool = False,
) -> dict:
    """Create an event. `start`/`end` are RFC3339 datetimes; date-only ('2026-06-10') makes an all-day event."""
    def time_obj(s: str) -> dict:
        if "T" in s:
            obj = {"dateTime": s}
            if timezone:
                obj["timeZone"] = timezone
            return obj
        return {"date": s}

    body: dict[str, Any] = {
        "summary": summary,
        "start": time_obj(start),
        "end": time_obj(end),
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]

    kwargs: dict[str, Any] = {
        "calendarId": calendar_id,
        "body": body,
        "sendUpdates": send_updates,
    }
    if add_google_meet:
        import uuid

        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        kwargs["conferenceDataVersion"] = 1

    return auth.calendar(account).events().insert(**kwargs).execute()


@mcp.tool()
def calendar_event_update(
    account: AccountSlug,
    event_id: str,
    calendar_id: str = "primary",
    summary: str | None = None,
    description: str | None = None,
    location: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    attendees: list[str] | None = None,
    send_updates: Literal["all", "externalOnly", "none"] = "none",
) -> dict:
    """Patch an existing event — only the fields you pass are changed."""
    patch: dict[str, Any] = {}
    if summary is not None:
        patch["summary"] = summary
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start is not None:
        patch["start"] = (
            {"dateTime": start, **({"timeZone": timezone} if timezone else {})}
            if "T" in start
            else {"date": start}
        )
    if end is not None:
        patch["end"] = (
            {"dateTime": end, **({"timeZone": timezone} if timezone else {})}
            if "T" in end
            else {"date": end}
        )
    if attendees is not None:
        patch["attendees"] = [{"email": a} for a in attendees]
    return (
        auth.calendar(account)
        .events()
        .patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch,
            sendUpdates=send_updates,
        )
        .execute()
    )


@mcp.tool()
def calendar_event_delete(
    account: AccountSlug,
    event_id: str,
    calendar_id: str = "primary",
    send_updates: Literal["all", "externalOnly", "none"] = "none",
) -> dict:
    """Delete an event."""
    auth.calendar(account).events().delete(
        calendarId=calendar_id, eventId=event_id, sendUpdates=send_updates
    ).execute()
    return {"deleted": event_id}


@mcp.tool()
def calendar_create(
    account: AccountSlug,
    summary: str,
    description: str | None = None,
    timezone: str | None = None,
) -> dict:
    """Create a new secondary calendar owned by the account (a school, project, or family calendar).

    Returns the new calendar's id — pass it as `calendar_id` to the event tools and to `calendar_share`.
    """
    body: dict[str, Any] = {"summary": summary}
    if description:
        body["description"] = description
    if timezone:
        body["timeZone"] = timezone
    cal = auth.calendar(account).calendars().insert(body=body).execute()
    return {
        "id": cal.get("id"),
        "summary": cal.get("summary"),
        "description": cal.get("description"),
        "timeZone": cal.get("timeZone"),
    }


_SHARE_ROLES = ("freeBusyReader", "reader", "writer")


def _acl_rule_id(email: str) -> str:
    """Google names a per-user sharing rule `user:<email>`."""
    return f"user:{email.strip().lower()}"


@mcp.tool()
def calendar_acl_list(account: AccountSlug, calendar_id: str) -> dict:
    """Who can see a calendar — one row per sharing rule (a user, a group, a domain, or the public)."""
    resp = auth.calendar(account).acl().list(calendarId=calendar_id).execute()
    rules = [
        {
            "id": r.get("id"),
            "role": r.get("role"),
            "scope_type": r.get("scope", {}).get("type"),
            "scope_value": r.get("scope", {}).get("value"),
        }
        for r in resp.get("items", [])
    ]
    return {"calendar_id": calendar_id, "rules": rules}


@mcp.tool()
def calendar_share(
    account: AccountSlug,
    calendar_id: str,
    email: str,
    role: Literal["freeBusyReader", "reader", "writer"] = "reader",
    send_notifications: bool = True,
) -> dict:
    """Share a calendar with one person (or Google group) by email.

    Roles: `freeBusyReader` (busy/free only), `reader` (see all event details),
    `writer` (also add and edit events). `owner` is deliberately not offered —
    hand ownership over in the Calendar UI. Sharing an address that already has
    access updates its role. By default Google emails the person an invitation.
    """
    if role not in _SHARE_ROLES:
        raise ValueError(f"role must be one of {_SHARE_ROLES}, got {role!r}")
    rule = (
        auth.calendar(account)
        .acl()
        .insert(
            calendarId=calendar_id,
            body={"role": role, "scope": {"type": "user", "value": email.strip()}},
            sendNotifications=send_notifications,
        )
        .execute()
    )
    return {
        "calendar_id": calendar_id,
        "rule_id": rule.get("id"),
        "email": rule.get("scope", {}).get("value", email.strip()),
        "role": rule.get("role", role),
    }


@mcp.tool()
def calendar_unshare(account: AccountSlug, calendar_id: str, email: str) -> dict:
    """Remove one person's access to a calendar.

    Looks the address up in the calendar's sharing rules (case-insensitively) and
    deletes that rule; falls back to the conventional `user:<email>` rule id.
    """
    svc = auth.calendar(account)
    want = email.strip().lower()
    rules = svc.acl().list(calendarId=calendar_id).execute().get("items", [])
    match = next(
        (
            r
            for r in rules
            if r.get("scope", {}).get("type") == "user"
            and (r.get("scope", {}).get("value") or "").lower() == want
        ),
        None,
    )
    rule_id = match["id"] if match else _acl_rule_id(email)
    svc.acl().delete(calendarId=calendar_id, ruleId=rule_id).execute()
    return {"calendar_id": calendar_id, "removed": rule_id, "found": match is not None}


# ─── Drive ──────────────────────────────────────────────────────────────


_DRIVE_FIELDS = "id, name, mimeType, parents, webViewLink, webContentLink, modifiedTime, size, owners(emailAddress, displayName)"

# Office formats Drive can convert into its own editable types. Used when
# creating a native Doc/Sheet/Slides from a local file, and when revising one.
_GOOGLE_NATIVE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "application/vnd.google-apps.presentation",
}


@mcp.tool()
def drive_search(
    account: AccountSlug,
    query: str | None = None,
    max_results: int = 20,
    order_by: str | None = "modifiedTime desc",
    include_trashed: bool = False,
) -> dict:
    """List/search files. `query` uses Drive query language; see
    https://developers.google.com/drive/api/guides/search-files
    Examples:
      "name contains 'budget'"
      "mimeType='application/vnd.google-apps.folder'"
      "'<parent-id>' in parents"
    """
    q_parts = []
    if query:
        q_parts.append(f"({query})")
    if not include_trashed:
        q_parts.append("trashed = false")
    q = " and ".join(q_parts) if q_parts else None

    resp = (
        auth.drive(account)
        .files()
        .list(
            q=q,
            pageSize=max_results,
            orderBy=order_by,
            fields=f"nextPageToken, files({_DRIVE_FIELDS})",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
    )
    return {"files": resp.get("files", []), "next_page_token": resp.get("nextPageToken")}


@mcp.tool()
def drive_file_get(account: AccountSlug, file_id: str) -> dict:
    """Get a file's full metadata."""
    return (
        auth.drive(account)
        .files()
        .get(fileId=file_id, fields=_DRIVE_FIELDS, supportsAllDrives=True)
        .execute()
    )


@mcp.tool()
def drive_file_download(
    account: AccountSlug,
    file_id: str,
    save_to: str,
    export_mime_type: str | None = None,
) -> dict:
    """Download a file. For Google Docs/Sheets/Slides, pass `export_mime_type`
    (e.g. 'application/pdf', 'text/plain', 'text/csv')."""
    out_path = _guard_local_path(Path(save_to).expanduser())
    svc = auth.drive(account).files()
    if export_mime_type:
        req = svc.export_media(fileId=file_id, mimeType=export_mime_type)
    else:
        req = svc.get_media(fileId=file_id, supportsAllDrives=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with io.FileIO(out_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return {"saved_to": str(out_path), "bytes": out_path.stat().st_size}


@mcp.tool()
def drive_file_upload(
    account: AccountSlug,
    local_path: str,
    name: str | None = None,
    parent_folder_id: str | None = None,
    mime_type: str | None = None,
    convert_to_google_doc: bool = False,
) -> dict:
    """Upload a local file to Drive. `convert_to_google_doc=True` converts
    .docx/.xlsx/.pptx to native Google Docs/Sheets/Slides."""
    path = _local_file(local_path)

    metadata: dict[str, Any] = {"name": name or path.name}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    if mime_type is None:
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

    if convert_to_google_doc:
        # Drive treats the destination mimeType in metadata as the
        # target format; the source mimeType comes from the media body.
        target = _GOOGLE_NATIVE.get(mime_type)
        if target:
            metadata["mimeType"] = target

    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    file = (
        auth.drive(account)
        .files()
        .create(
            body=metadata,
            media_body=media,
            fields=_DRIVE_FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )
    return file


@mcp.tool()
def drive_file_update_content(
    account: AccountSlug,
    file_id: str,
    local_path: str,
    name: str | None = None,
    mime_type: str | None = None,
    convert_to_google_doc: bool = False,
    keep_revision_forever: bool = False,
) -> dict:
    """Replace an existing file's contents in place. The file keeps its ID,
    link and sharing, and the previous contents stay in Drive's revision
    history. Use this rather than `drive_file_upload` to revise something
    already in Drive: uploading again under the same name creates a second
    file, it does not version the first. `convert_to_google_doc=True` revises
    a native Doc/Sheet/Slides from a local .docx/.xlsx/.pptx. Google
    Docs/Sheets/Slides keep full version history; for binary files Drive
    drops old revisions after 30 days or 100 revisions unless
    `keep_revision_forever=True` (at most 200 pinned per file)."""
    path = _local_file(local_path)

    if mime_type is None:
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

    metadata: dict[str, Any] = {}
    if name:
        metadata["name"] = name
    if convert_to_google_doc:
        target = _GOOGLE_NATIVE.get(mime_type)
        if target:
            metadata["mimeType"] = target

    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    file = (
        auth.drive(account)
        .files()
        .update(
            fileId=file_id,
            body=metadata,
            media_body=media,
            fields=_DRIVE_FIELDS,
            supportsAllDrives=True,
            keepRevisionForever=keep_revision_forever,
        )
        .execute()
    )
    return file


@mcp.tool()
def drive_file_move(
    account: AccountSlug,
    file_id: str,
    new_parent_id: str,
    remove_old_parents: bool = True,
) -> dict:
    """Move a file to a new folder."""
    svc = auth.drive(account).files()
    file = svc.get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    prev_parents = ",".join(file.get("parents", [])) if remove_old_parents else None
    updated = svc.update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=prev_parents,
        fields=_DRIVE_FIELDS,
        supportsAllDrives=True,
    ).execute()
    return updated


@mcp.tool()
def drive_file_rename(account: AccountSlug, file_id: str, new_name: str) -> dict:
    """Rename a file."""
    return (
        auth.drive(account)
        .files()
        .update(
            fileId=file_id,
            body={"name": new_name},
            fields=_DRIVE_FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )


@mcp.tool()
def drive_file_trash(account: AccountSlug, file_id: str) -> dict:
    """Move a file to trash (reversible)."""
    return (
        auth.drive(account)
        .files()
        .update(
            fileId=file_id,
            body={"trashed": True},
            fields=_DRIVE_FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )


@mcp.tool()
def drive_folder_create(
    account: AccountSlug,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """Create a new folder."""
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]
    return (
        auth.drive(account)
        .files()
        .create(body=metadata, fields=_DRIVE_FIELDS, supportsAllDrives=True)
        .execute()
    )


@mcp.tool()
def drive_file_share(
    account: AccountSlug,
    file_id: str,
    email: str,
    role: Literal["reader", "commenter", "writer", "fileOrganizer", "organizer"] = "reader",
    send_notification: bool = False,
    message: str | None = None,
) -> dict:
    """Share a file with someone by email."""
    permission = {
        "type": "user",
        "role": role,
        "emailAddress": email,
    }
    kwargs: dict[str, Any] = {
        "fileId": file_id,
        "body": permission,
        "sendNotificationEmail": send_notification,
        "supportsAllDrives": True,
    }
    if send_notification and message:
        kwargs["emailMessage"] = message
    return auth.drive(account).permissions().create(**kwargs).execute()


@mcp.tool()
def drive_file_link_access(
    account: AccountSlug,
    file_id: str,
    enabled: bool,
    role: Literal["reader", "commenter"] = "reader",
) -> dict:
    """Toggle "anyone with the link" access on a file the account owns.

    Built for zero-bandwidth server-side fetches: some APIs (e.g. a
    transcription service's source_url parameter) can download a Drive file
    themselves — but only while it is link-accessible. Flow: enable, hand the
    returned direct_download_url to the fetching service, then IMMEDIATELY
    call again with enabled=false to revoke. Never leave link access on.
    """
    service = auth.drive(account)
    if enabled:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": role},
            supportsAllDrives=True,
        ).execute()
        return {
            "file_id": file_id,
            "link_access": role,
            "direct_download_url": (
                "https://drive.usercontent.google.com/download"
                f"?id={file_id}&export=download&confirm=t"
            ),
            "reminder": "Revoke when done: call again with enabled=false.",
        }
    service.permissions().delete(
        fileId=file_id,
        permissionId="anyoneWithLink",
        supportsAllDrives=True,
    ).execute()
    return {"file_id": file_id, "link_access": "off"}


# Comments live on the Drive file, so these work on any file type. For a
# Google Doc, each comment is also tagged with the section (heading path) its
# quoted text falls in, found by locating the quote in the document.

_COMMENT_FIELDS = (
    "id, content, author(displayName, emailAddress), createdTime, modifiedTime, "
    "resolved, deleted, quotedFileContent, "
    "replies(id, content, author(displayName, emailAddress), createdTime, action, deleted)"
)
_REPLY_FIELDS = "id, content, author(displayName, emailAddress), createdTime, action"
_GOOGLE_DOC = "application/vnd.google-apps.document"


def _person(a: dict | None) -> str | None:
    if not a:
        return None
    name, email = a.get("displayName"), a.get("emailAddress")
    return f"{name} <{email}>" if name and email else name or email


def _comment_row(c: dict) -> dict:
    quoted = (c.get("quotedFileContent") or {}).get("value")
    return {
        "id": c["id"],
        "author": _person(c.get("author")),
        "content": c.get("content"),
        "created_time": c.get("createdTime"),
        "resolved": bool(c.get("resolved")),
        "quoted_text": unescape(quoted) if quoted else None,
        "replies": [
            {
                "id": r["id"],
                "author": _person(r.get("author")),
                "content": r.get("content"),
                "created_time": r.get("createdTime"),
                **({"action": r["action"]} if r.get("action") else {}),
            }
            for r in c.get("replies", [])
            if not r.get("deleted")
        ],
    }


def _tag_sections(account: str, file_id: str, rows: list[dict]) -> None:
    """Add `section` (innermost heading), `section_path` and `tab_id` to each row."""
    doc = auth.docs(account).documents().get(documentId=file_id, includeTabsContent=True).execute()
    tabs = [(t, docs_model.outline(t["body"])) for t in docs_model.flatten_tabs(doc)]
    for row in rows:
        row.update(section=None, section_path=[], tab_id=None)
        if not row["quoted_text"]:
            continue
        for tab, sections in tabs:
            at = docs_model.locate_quote(tab["body"], row["quoted_text"])
            if at is not None:
                path = docs_model.section_path(sections, at)
                row.update(section=path[-1] if path else None, section_path=path, tab_id=tab["tab_id"])
                break


@mcp.tool()
def drive_comment_list(
    account: AccountSlug,
    file_id: str,
    include_resolved: bool = False,
    heading: str | None = None,
) -> dict:
    """List the comments on a file, with their replies and the text each quotes.

    Open comments only unless `include_resolved`. For a Google Doc each
    comment also carries `section` (the heading it sits under) and
    `section_path` (outermost heading first); pass `heading` to keep only
    comments inside that heading's section, subsections included. A comment
    whose quoted text was since edited away has `section: null`.
    """
    svc = auth.drive(account).comments()
    comments: list[dict] = []
    token = None
    while True:
        resp = svc.list(
            fileId=file_id,
            includeDeleted=False,
            pageSize=100,
            pageToken=token,
            fields=f"nextPageToken, comments({_COMMENT_FIELDS})",
        ).execute()
        comments.extend(resp.get("comments", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    rows = [
        _comment_row(c)
        for c in comments
        if not c.get("deleted") and (include_resolved or not c.get("resolved"))
    ]

    mime = (
        auth.drive(account).files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()
    ).get("mimeType")
    if mime == _GOOGLE_DOC:
        _tag_sections(account, file_id, rows)
        if heading is not None:
            key = docs_model.norm(heading)
            rows = [r for r in rows if any(docs_model.norm(h) == key for h in r["section_path"])]
    elif heading is not None:
        raise ValueError("`heading` filtering needs a Google Doc; this file is " + str(mime))
    return {"file_id": file_id, "comments": rows}


@mcp.tool()
def drive_comment_create(
    account: AccountSlug,
    file_id: str,
    content: str,
    quoted_text: str | None = None,
) -> dict:
    """Post a new comment on a file.

    Note the Drive API's own limit here: even with `quoted_text` set, it
    cannot anchor a new comment to a highlighted range in a Google Doc the
    way the Docs UI does — the comment will show the quote as context, but
    nothing in the document body itself gets highlighted. For a Google Doc,
    when `quoted_text` is given it is first located in the document (as
    drive_comment_list does) so the comment can carry `section` /
    `section_path`; if the quote can't be found there, this raises instead
    of creating a comment with a bogus anchor. Returns the new comment in
    the same shape as drive_comment_list's rows.
    """
    mime = (
        auth.drive(account).files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True).execute()
    ).get("mimeType")

    section = section_path = tab_id = None
    if mime == _GOOGLE_DOC:
        section_path = []
        if quoted_text:
            doc = auth.docs(account).documents().get(documentId=file_id, includeTabsContent=True).execute()
            for tab in docs_model.flatten_tabs(doc):
                at = docs_model.locate_quote(tab["body"], quoted_text)
                if at is not None:
                    path = docs_model.section_path(docs_model.outline(tab["body"]), at)
                    section, section_path, tab_id = (path[-1] if path else None), path, tab["tab_id"]
                    break
            else:
                raise ValueError(f"Quote not found in the document: {quoted_text!r}")

    body: dict[str, Any] = {"content": content}
    if quoted_text:
        body["quotedFileContent"] = {"mimeType": "text/html", "value": quoted_text}
    comment = (
        auth.drive(account).comments().create(fileId=file_id, body=body, fields=_COMMENT_FIELDS).execute()
    )
    row = _comment_row(comment)
    if mime == _GOOGLE_DOC:
        row.update(section=section, section_path=section_path, tab_id=tab_id)
    return row


@mcp.tool()
def drive_comment_reply(account: AccountSlug, file_id: str, comment_id: str, content: str) -> dict:
    """Reply to an existing comment. The reply is posted as this account and
    notifies the thread's participants the way a reply in the Docs UI does."""
    return (
        auth.drive(account)
        .replies()
        .create(fileId=file_id, commentId=comment_id, body={"content": content}, fields=_REPLY_FIELDS)
        .execute()
    )


@mcp.tool()
def drive_comment_resolve(
    account: AccountSlug,
    file_id: str,
    comment_id: str,
    resolved: bool = True,
    content: str | None = None,
) -> dict:
    """Resolve a comment thread (or reopen it with resolved=false), optionally
    leaving `content` as a closing reply in the same step."""
    body: dict[str, Any] = {"action": "resolve" if resolved else "reopen"}
    if content:
        body["content"] = content
    return (
        auth.drive(account)
        .replies()
        .create(fileId=file_id, commentId=comment_id, body=body, fields=_REPLY_FIELDS)
        .execute()
    )


# ─── Docs (in-place editing) ────────────────────────────────────────────

# Edits go through the Docs API's batchUpdate, so they change the live
# document — formatting, comments and revision history elsewhere in it are
# left alone — instead of replacing the whole file the way
# drive_file_update_content does.
#
# Every write is pinned to the revision it was computed against
# (writeControl.requiredRevisionId): if someone edits the document between
# the read and the write, Google rejects the write rather than applying it at
# indices that no longer point where they did.


def _docs_load(account: str, document_id: str, tab_id: str | None, revision_id: str | None):
    """Fetch the document; refuse if it moved on from `revision_id`."""
    try:
        doc = auth.docs(account).documents().get(documentId=document_id, includeTabsContent=True).execute()
    except HttpError as e:
        if e.resp.status == 400:
            raise ValueError(
                f"{document_id} is not a Google Doc (the Docs API can't open it). Convert it first, "
                "e.g. drive_file_upload with convert_to_google_doc=True, then edit the converted copy."
            ) from e
        raise
    current = doc.get("revisionId")
    if revision_id and current and revision_id != current:
        raise ValueError(
            f"The document changed since revision {revision_id} (now {current}). "
            "Call docs_get again and redo the edit against the current text."
        )
    return doc, docs_model.resolve_tab(doc, tab_id), current


def _docs_batch(account: str, document_id: str, requests: list[dict], revision_id: str | None) -> dict:
    body: dict[str, Any] = {"requests": requests}
    if revision_id:
        body["writeControl"] = {"requiredRevisionId": revision_id}
    resp = auth.docs(account).documents().batchUpdate(documentId=document_id, body=body).execute()
    return {
        "document_id": document_id,
        "revision_id": (resp.get("writeControl") or {}).get("requiredRevisionId"),
        "requests_applied": len(requests),
        "replies": resp.get("replies", []),
    }


def _insert_index(body: dict, at: str, heading: str | None, index: int | None) -> int:
    """Resolve docs_insert's `at` / `heading` / `index` to a document index."""
    end = docs_model.body_end(body)
    if at == "end":
        return end - 1
    if at == "start":
        return next((p["start_index"] for p in docs_model.paragraphs(body)), end - 1)
    if at == "after_heading":
        if not heading:
            raise ValueError('at="after_heading" needs `heading`.')
        return docs_model.find_section(docs_model.outline(body), heading).end
    if index is None:
        raise ValueError('at="index" needs `index`.')
    if not 1 <= index < end:
        raise ValueError(f"index must be between 1 and {end - 1} for this tab, got {index}.")
    return index


# insertTable only creates an empty grid; there's no way to seed cell text
# in the same request. So a table goes in two batchUpdates: insertTable,
# then a re-fetch to find where the table actually landed (it may have
# pushed a newline in ahead of it) and fill its cells by index.
def _fill_table(account: str, document_id: str, table: dict, rows: list[list[str]], tab_id: str | None, revision: str | None) -> dict:
    """Fill a freshly-inserted (all-empty) table's cells with `rows`.

    Cells are filled in reverse document order so that inserting into one
    cell never shifts the start index of a cell still to be filled. The
    header row (rows[0]) is bolded.
    """
    order = [
        (cell["start_index"], r, c)
        for r, row_cells in enumerate(table["cells"])
        for c, cell in enumerate(row_cells)
    ]
    order.sort(key=lambda x: x[0], reverse=True)

    reqs: list[dict] = []
    for start, r, c in order:
        if r >= len(rows) or c >= len(rows[r]) or not rows[r][c]:
            continue
        runs = docs_markdown.parse_inline(rows[r][c])
        text = "".join(t for t, _ in runs)
        loc = {"index": start, **({"tabId": tab_id} if tab_id else {})}
        reqs.append({"insertText": {"text": text, "location": loc}})
        pos = start
        for t, style in runs:
            n = docs_model.utf16_len(t)
            if style:
                rng = {"startIndex": pos, "endIndex": pos + n, **({"tabId": tab_id} if tab_id else {})}
                reqs.append({"updateTextStyle": {"range": rng, "textStyle": style, "fields": ",".join(style)}})
            pos += n
        if r == 0:
            rng = {"startIndex": start, "endIndex": start + docs_model.utf16_len(text), **({"tabId": tab_id} if tab_id else {})}
            reqs.append({"updateTextStyle": {"range": rng, "textStyle": {"bold": True}, "fields": "bold"}})
    if not reqs:
        return {"document_id": document_id, "revision_id": revision, "requests_applied": 0, "replies": []}
    return _docs_batch(account, document_id, reqs, revision)


def _insert_table_segment(
    account: str, document_id: str, rows: list[list[str]], idx: int, mode: str, tab_id: str | None, revision: str | None
) -> tuple[int, str | None, dict]:
    """Insert one Markdown table at `idx`. Returns (index after the table,
    new revision, the batch result of the cell fill)."""
    loc = {"index": idx, **({"tabId": tab_id} if tab_id else {})}
    reqs: list[dict] = []
    tbl_at = idx
    if mode == "end":  # the last paragraph has text; a table needs one of its own
        reqs.append({"insertText": {"text": "\n", "location": loc}})
        tbl_at = idx + 1
        loc = {"index": tbl_at, **({"tabId": tab_id} if tab_id else {})}
    reqs.append({"insertTable": {"rows": len(rows), "columns": len(rows[0]) if rows else 0, "location": loc}})
    _docs_batch(account, document_id, reqs, revision)

    _, tab2, revision2 = _docs_load(account, document_id, tab_id, None)
    table = next((t for t in docs_model.tables(tab2["body"]) if t["start_index"] >= tbl_at), None)
    if table is None:
        raise ValueError("Inserted a table but could not find it again after re-fetching the document.")
    out2 = _fill_table(account, document_id, table, rows, tab_id, revision2)

    # insertTable always adds a newline before the table, leaving an empty
    # paragraph above it; that newline itself can't be deleted (Docs refuses
    # to delete the newline before a table), but the previous paragraph's can,
    # which folds the empty one away and keeps the previous paragraph's style
    # (verified live, a heading included). Cell fills sit after the table, so
    # the indices here are unchanged by them.
    t = table["start_index"]
    paras = {p["start_index"]: p for p in docs_model.paragraphs(tab2["body"])}
    gap = paras.get(t - 1)
    before = next((p for p in paras.values() if p["end_index"] == t - 1), None)
    if gap and not gap["text"] and before is not None:
        rng = {"startIndex": t - 2, "endIndex": t - 1, **({"tabId": tab_id} if tab_id else {})}
        out2 = _docs_batch(account, document_id, [{"deleteContentRange": {"range": rng}}], out2.get("revision_id"))
    return table["end_index"], out2.get("revision_id", revision2), out2


def _insert_markdown(account: str, document_id: str, markdown: str, idx: int, mode: str, tab: dict, revision: str | None) -> dict:
    """Insert `markdown` at `idx`, handling any GitHub pipe tables in it.

    Markdown with no table is a single insert, exactly as before. Markdown
    with one or more tables is applied segment by segment (text, then each
    table, then more text, ...), each later segment landing right after the
    one before it.
    """
    tab_id = tab["tab_id"]
    segments = docs_markdown.split_markdown_tables(markdown)
    if not any(kind == "table" for kind, _ in segments):
        reqs = docs_markdown.markdown_to_requests(markdown, idx, mode, tab_id)
        return {"inserted_at": idx, **_docs_batch(account, document_id, reqs, revision)}

    # Each segment changes the document's length, so rather than predicting
    # where the next one goes, re-read after each: whatever followed the
    # insertion point is untouched, so it stays `tail` units from the end.
    first_idx = idx
    tail = docs_model.body_end(tab["body"]) - idx
    last_out: dict = {}
    for kind, payload in segments:
        if kind == "text":
            if not payload.strip():
                continue
            reqs = docs_markdown.markdown_to_requests(payload, idx, mode, tab_id)
            last_out = _docs_batch(account, document_id, reqs, revision)
        else:
            _, _, last_out = _insert_table_segment(account, document_id, payload, idx, mode, tab_id, revision)
        _, fresh, revision = _docs_load(account, document_id, tab_id, None)
        idx = docs_model.body_end(fresh["body"]) - tail
        mode = docs_model.insertion_mode(fresh["body"], idx)
    return {"inserted_at": first_idx, **last_out}


@mcp.tool()
def docs_create(
    account: AccountSlug,
    title: str,
    markdown: str | None = None,
    folder_id: str | None = None,
    template_id: str | None = None,
    replacements: dict[str, str] | None = None,
) -> dict:
    """Create a new Google Doc, optionally filled with Markdown or copied from a template.

    Creates an empty doc via Drive (so `folder_id` places it in one call,
    instead of create-then-move) and, if `markdown` is given, inserts it into
    the new doc's empty body. Markdown support is as for docs_insert.

    `template_id` copies that Google Doc instead (via Drive files.copy, so
    its formatting, headers/footers and existing content carry over) and
    renames the copy to `title`. `replacements` then runs one
    documents.batchUpdate of replaceAllText over the copy — pass the
    template's literal placeholders as found in it, e.g.
    {"{{name}}": "Acme Corp"}; matching is case-sensitive, and
    `occurrences_replaced` in the result gives each key's count (0 means it
    wasn't found). `markdown`, if given together with `template_id`, is
    appended at the end of the copied doc, same as docs_insert(at="end").

    Returns `document_id`, `title`, `url`, `occurrences_replaced` (when
    `replacements` was given), and — when markdown was inserted — the
    batchUpdate result (`revision_id`, `requests_applied`, `replies`).
    """
    metadata: dict[str, Any] = {"name": title}
    if folder_id:
        metadata["parents"] = [folder_id]
    if template_id:
        file = (
            auth.drive(account)
            .files()
            .copy(fileId=template_id, body=metadata, fields="id, name, webViewLink", supportsAllDrives=True)
            .execute()
        )
    else:
        file = (
            auth.drive(account)
            .files()
            .create(
                body={**metadata, "mimeType": "application/vnd.google-apps.document"},
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
    document_id = file["id"]
    out = {"document_id": document_id, "title": file.get("name"), "url": file.get("webViewLink")}

    if replacements:
        reqs = [
            {"replaceAllText": {"containsText": {"text": k, "matchCase": True}, "replaceText": v}}
            for k, v in replacements.items()
        ]
        batch = _docs_batch(account, document_id, reqs, None)
        out["occurrences_replaced"] = {
            k: (r.get("replaceAllText") or {}).get("occurrencesChanged", 0)
            for k, r in zip(replacements, batch["replies"])
        }

    if markdown:
        _, tab, revision = _docs_load(account, document_id, None, None)
        body = tab["body"]
        idx = _insert_index(body, "end", None, None)
        mode = docs_model.insertion_mode(body, idx)
        out.update(_insert_markdown(account, document_id, markdown, idx, mode, tab, revision))
    return out


@mcp.tool()
def docs_get(
    account: AccountSlug,
    document_id: str,
    tab_id: str | None = None,
    include_paragraphs: bool = False,
    format: Literal["text", "markdown"] = "text",
) -> dict:
    """Read a Google Doc for editing: its text, tabs, and heading outline.

    Returns `revision_id` (pass it to the edit tools so they refuse to write
    over a newer version), `tabs`, `outline`, and `tables`. Outline entries:
    `level` (0 = Title, 1-6), `start_index`, `body_start_index` and
    `end_index` (the section's extent, subsections included). `tables` lists
    every top-level table with its `rows`, `columns` and per-cell
    `text`/index ranges; docs_table_edit's `table_index` is the position in
    this list. `include_paragraphs` adds every top-level paragraph with its
    index range, for exact edits with docs_delete_range / docs_insert(index=).
    Indices are per tab; without `tab_id` the first tab is used.

    `format="markdown"` returns the tab's content as `markdown` instead of
    `text`, in the same Markdown subset docs_insert / docs_replace_section
    write (headings, bold/italic/code/links, lists, quotes, tables; images
    and strikethrough render too, though they won't round-trip through a
    write). Prefer it when about to rewrite a section with docs_replace_section
    or docs_insert — plain `text` flattens formatting, so a rewrite built from
    it would silently lose bold/links/lists.
    """
    doc, tab, revision = _docs_load(account, document_id, tab_id, None)
    body = tab["body"]
    out = {
        "document_id": document_id,
        "title": doc.get("title"),
        "revision_id": revision,
        "tab_id": tab["tab_id"],
        "tabs": [
            {"tab_id": t["tab_id"], "title": t["title"], "depth": t["depth"]}
            for t in docs_model.flatten_tabs(doc)
        ],
        "body_end_index": docs_model.body_end(body),
        "outline": [s.as_dict() for s in docs_model.outline(body)],
        "tables": docs_model.tables(body),
    }
    if format == "markdown":
        out["markdown"] = docs_to_markdown.body_to_markdown(body, tab.get("lists", {}))
    else:
        out["text"] = docs_model.plain_text(body)
    if include_paragraphs:
        out["paragraphs"] = docs_model.paragraphs(body)
    return out


@mcp.tool()
def docs_insert(
    account: AccountSlug,
    document_id: str,
    markdown: str,
    at: Literal["end", "start", "after_heading", "index"] = "end",
    heading: str | None = None,
    index: int | None = None,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Insert Markdown into a Google Doc in place, styled as Docs formatting.

    Where: `at="end"` / `"start"` of the tab; `"after_heading"` puts it at the
    end of `heading`'s section (after any subsections); `"index"` at an
    explicit `index` from docs_get — a paragraph's start_index adds whole
    paragraphs there, any other index splices the text inline.

    Markdown supported: # headings, paragraphs, **bold**, *italic*, `code`,
    [links](url), - bullets, 1. numbered lists (indent 2 spaces to nest),
    - [ ] / - [x] checklists, ``` code blocks, > quotes (an indented
    paragraph), and GitHub-style pipe tables (a `| a | b |` header, a
    `|---|---|` separator row, then data rows; the header row is bolded).
    Anything else is inserted as literal text.
    """
    _, tab, revision = _docs_load(account, document_id, tab_id, revision_id)
    body = tab["body"]
    idx = _insert_index(body, at, heading, index)
    mode = docs_model.insertion_mode(body, idx)
    return _insert_markdown(account, document_id, markdown, idx, mode, tab, revision)


@mcp.tool()
def docs_replace_section(
    account: AccountSlug,
    document_id: str,
    heading: str,
    markdown: str,
    keep_heading: bool = True,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Rewrite everything under a heading, in place, with Markdown.

    The section runs from the heading to the next heading of the same or a
    higher level, so its subsections are replaced too. `keep_heading=False`
    replaces the heading line as well (include a new # heading in `markdown`
    to rename it). The heading must match exactly one heading (case and
    spacing ignored). A table inside the section is deleted along with the
    rest of it (use docs_table_edit to change a table without replacing its
    section). Refuses a section holding an image, table of contents or
    section break — remove those deliberately with docs_delete_range, or
    edit around them with docs_replace_text / docs_insert. Comments anchored
    to the replaced text lose their anchor. Markdown support is as for
    docs_insert.
    """
    _, tab, revision = _docs_load(account, document_id, tab_id, revision_id)
    body = tab["body"]
    end = docs_model.body_end(body)
    section = docs_model.find_section(docs_model.outline(body), heading)
    start = section.body_start if keep_heading else section.heading_start
    stop = section.end
    loc_tab = tab["tab_id"]

    reqs: list[dict] = []
    if stop > start:
        blocker = docs_model.structural_in_range(body, start, stop)
        if blocker and blocker != "table":
            raise ValueError(
                f"The section {section.heading!r} contains a {blocker}; replacing it would delete that too. "
                "Use docs_delete_range to remove it deliberately, or docs_replace_text / docs_insert instead."
            )
        rng = {"startIndex": start, "endIndex": stop}
        if loc_tab:
            rng["tabId"] = loc_tab
        reqs.append({"deleteContentRange": {"range": rng}})
        idx, mode = start, ("end_empty" if stop == end - 1 else "paragraph")
    else:  # empty section and the heading is the tab's last paragraph
        idx, mode = end - 1, "end"

    segments = docs_markdown.split_markdown_tables(markdown)
    if not any(kind == "table" for kind, _ in segments):
        reqs += docs_markdown.markdown_to_requests(markdown, idx, mode, loc_tab)
        return {
            "section": section.heading,
            "replaced_range": [start, stop],
            **_docs_batch(account, document_id, reqs, revision),
        }

    if reqs:  # the delete goes first, on its own, then the segments follow it
        _docs_batch(account, document_id, reqs, revision)
        # _insert_markdown measures from the end of the tab: give it the post-delete one.
        _, tab, revision = _docs_load(account, document_id, tab_id, None)
    result = _insert_markdown(account, document_id, markdown, idx, mode, tab, revision)
    return {"section": section.heading, "replaced_range": [start, stop], **result}


@mcp.tool()
def docs_replace_text(
    account: AccountSlug,
    document_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Find and replace plain text everywhere in a Google Doc (or one tab),
    in place. The replacement keeps the formatting of the text it replaces.
    Returns `occurrences_changed`; 0 means nothing matched."""
    req: dict[str, Any] = {
        "containsText": {"text": find, "matchCase": match_case},
        "replaceText": replace,
    }
    if tab_id:
        req["tabsCriteria"] = {"tabIds": [tab_id]}
    out = _docs_batch(account, document_id, [{"replaceAllText": req}], revision_id)
    changed = sum((r.get("replaceAllText") or {}).get("occurrencesChanged", 0) for r in out["replies"])
    return {**out, "occurrences_changed": changed}


@mcp.tool()
def docs_delete_range(
    account: AccountSlug,
    document_id: str,
    start_index: int,
    end_index: int,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Delete [start_index, end_index) from a Google Doc, in place. Take the
    indices from docs_get (outline or include_paragraphs) and pass its
    `revision_id` so a stale range is refused. A tab's final newline
    (body_end_index - 1) can't be deleted."""
    if not 1 <= start_index < end_index:
        raise ValueError(f"Need 1 <= start_index < end_index, got [{start_index}, {end_index}).")
    rng: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    if tab_id:
        rng["tabId"] = tab_id
    return _docs_batch(account, document_id, [{"deleteContentRange": {"range": rng}}], revision_id)


def _rgb(hex_color: str) -> dict:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"Colors are hex, like '#1a73e8' or '#e33'; got {hex_color!r}.")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


_ALIGN = {"left": "START", "center": "CENTER", "right": "END", "justify": "JUSTIFIED"}


@mcp.tool()
def docs_format(
    account: AccountSlug,
    document_id: str,
    target: Literal["heading", "section", "text", "range"],
    heading: str | None = None,
    text: str | None = None,
    start_index: int | None = None,
    end_index: int | None = None,
    color: str | None = None,
    background_color: str | None = None,
    font: str | None = None,
    size_pt: float | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    align: Literal["left", "center", "right", "justify"] | None = None,
    line_spacing: float | None = None,
    space_above_pt: float | None = None,
    space_below_pt: float | None = None,
    indent_pt: float | None = None,
    match_case: bool = True,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Change the look of existing text in a Google Doc, in place.

    What: `target="heading"` — the heading line itself; `"section"` — the
    text under `heading` (subsections included, heading line excluded);
    `"text"` — every occurrence of `text` (paragraph-level params apply to
    each match's whole paragraph); `"range"` — [start_index, end_index) from
    docs_get.

    How (set only what should change; the rest is left as is): text style —
    `color` and `background_color` as hex ('#1a73e8'), `font` as a Google
    Docs font name ('Georgia', 'Merriweather', 'Roboto Mono' — an unknown
    name renders as Arial), `size_pt`, and `bold` / `italic` / `underline`
    true or false. Paragraph style — `align` ('left'/'center'/'right'/
    'justify'), `line_spacing` (1.0, 1.15, 1.5, 2.0, ...), `space_above_pt` /
    `space_below_pt`, and `indent_pt` (left indent, first line included).
    """
    style: dict[str, Any] = {}
    if color:
        style["foregroundColor"] = _rgb(color)
    if background_color:
        style["backgroundColor"] = _rgb(background_color)
    if font:
        style["weightedFontFamily"] = {"fontFamily": font}
    if size_pt is not None:
        if size_pt <= 0:
            raise ValueError(f"size_pt must be positive, got {size_pt}.")
        style["fontSize"] = {"magnitude": size_pt, "unit": "PT"}
    for name, value in (("bold", bold), ("italic", italic), ("underline", underline)):
        if value is not None:
            style[name] = value

    pstyle: dict[str, Any] = {}
    if align:
        pstyle["alignment"] = _ALIGN[align]
    if line_spacing is not None:
        if line_spacing <= 0:
            raise ValueError(f"line_spacing must be positive, got {line_spacing}.")
        pstyle["lineSpacing"] = line_spacing * 100
    if space_above_pt is not None:
        pstyle["spaceAbove"] = {"magnitude": space_above_pt, "unit": "PT"}
    if space_below_pt is not None:
        pstyle["spaceBelow"] = {"magnitude": space_below_pt, "unit": "PT"}
    if indent_pt is not None:
        pstyle["indentStart"] = {"magnitude": indent_pt, "unit": "PT"}
        pstyle["indentFirstLine"] = {"magnitude": indent_pt, "unit": "PT"}

    if not style and not pstyle:
        raise ValueError("Nothing to change: pass at least one of color, background_color, font, size_pt, "
                         "bold, italic, underline, align, line_spacing, space_above_pt, space_below_pt, "
                         "indent_pt.")

    _, tab, revision = _docs_load(account, document_id, tab_id, revision_id)
    body = tab["body"]
    if target in ("heading", "section"):
        if not heading:
            raise ValueError(f'target="{target}" needs `heading`.')
        sec = docs_model.find_section(docs_model.outline(body), heading)
        if target == "heading":
            ranges = [(sec.heading_start, sec.body_start - 1)]
            para_ranges = [(sec.heading_start, sec.body_start)]
        else:
            ranges = [(sec.body_start, sec.end)]
            para_ranges = ranges
    elif target == "text":
        if not text:
            raise ValueError('target="text" needs `text`.')
        ranges = docs_model.find_all(body, text, match_case)
        if not ranges:
            raise ValueError(f"{text!r} does not appear in this tab.")
        paras = docs_model.paragraphs(body)
        para_ranges = []
        for s, _ in ranges:
            for p in paras:
                if p["start_index"] <= s < p["end_index"]:
                    pr = (p["start_index"], p["end_index"])
                    if pr not in para_ranges:
                        para_ranges.append(pr)
                    break
    else:
        if start_index is None or end_index is None or not 1 <= start_index < end_index:
            raise ValueError('target="range" needs 1 <= start_index < end_index.')
        ranges = [(start_index, end_index)]
        para_ranges = ranges

    def _rng(s: int, e: int) -> dict:
        r: dict[str, Any] = {"startIndex": s, "endIndex": e}
        if tab["tab_id"]:
            r["tabId"] = tab["tab_id"]
        return r

    reqs = []
    if style:
        for s, e in ranges:
            if e <= s:
                continue  # an empty section
            reqs.append({"updateTextStyle": {"range": _rng(s, e), "textStyle": style, "fields": ",".join(style)}})
    if pstyle:
        for s, e in para_ranges:
            if e <= s:
                continue  # an empty paragraph range
            reqs.append({
                "updateParagraphStyle": {"range": _rng(s, e), "paragraphStyle": pstyle, "fields": ",".join(pstyle)}
            })
    if not reqs:
        raise ValueError("The target is empty; there is no text to format.")
    return {"ranges": [list(r) for r in ranges], **_docs_batch(account, document_id, reqs, revision)}


# The Docs API only takes an image by URL, which Google fetches once and
# stores inside the document. Docs' own limits: PNG, JPEG or GIF, under 50 MB.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}
_IMAGE_CAP = 50 * 1024 * 1024


@mcp.tool()
def docs_insert_image(
    account: AccountSlug,
    document_id: str,
    image_url: str | None = None,
    local_path: str | None = None,
    at: Literal["end", "start", "after_heading", "index"] = "end",
    heading: str | None = None,
    index: int | None = None,
    width_pt: float | None = None,
    height_pt: float | None = None,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Insert an image into a Google Doc, in place — PNG, JPEG or GIF.

    Source: `image_url` (must be publicly fetchable) or `local_path`. A local
    file is uploaded to Drive and made link-readable only for the moment
    Google takes its copy; link access is then revoked and the upload
    trashed — the image lives on inside the document.

    Placement as for docs_insert; at a paragraph boundary or the end the
    image gets a paragraph of its own, at any other index it sits inline.
    Give `width_pt` or `height_pt` alone to keep the aspect ratio
    (a page body is about 468pt wide).
    """
    if (image_url is None) == (local_path is None):
        raise ValueError("Pass exactly one of image_url or local_path.")
    _, tab, revision = _docs_load(account, document_id, tab_id, revision_id)
    body = tab["body"]
    idx = _insert_index(body, at, heading, index)
    mode = docs_model.insertion_mode(body, idx)
    t = tab["tab_id"]

    def loc(i: int) -> dict:
        return {"index": i, **({"tabId": t} if t else {})}

    # Give the image its own paragraph unless it's being spliced inline.
    reqs: list[dict] = []
    if mode == "paragraph":
        reqs.append({"insertText": {"text": "\n", "location": loc(idx)}})
        img_at = idx
    elif mode == "end":
        reqs.append({"insertText": {"text": "\n", "location": loc(idx)}})
        img_at = idx + 1
    else:
        img_at = idx

    size: dict[str, Any] = {}
    if width_pt:
        size["width"] = {"magnitude": width_pt, "unit": "PT"}
    if height_pt:
        size["height"] = {"magnitude": height_pt, "unit": "PT"}

    def insert(uri: str) -> dict:
        image: dict[str, Any] = {"uri": uri, "location": loc(img_at)}
        if size:
            image["objectSize"] = size
        styled: list[dict] = [{"insertInlineImage": image}]
        if mode != "inline":
            # The new paragraph inherits the style of the one it was split
            # from (a heading, a list item); make it a plain paragraph.
            rng = {"startIndex": img_at, "endIndex": img_at + 2, **({"tabId": t} if t else {})}
            styled += [
                {"deleteParagraphBullets": {"range": rng}},
                {"updateParagraphStyle": {
                    "range": rng,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "fields": "namedStyleType,indentStart,indentFirstLine",
                }},
            ]
        return _docs_batch(account, document_id, [*reqs, *styled], revision)

    if image_url:
        out = insert(image_url)
    else:
        path = _local_file(local_path)
        mime, _ = mimetypes.guess_type(str(path))
        if mime not in _IMAGE_TYPES:
            raise ValueError(f"Docs takes PNG, JPEG or GIF images; {path.name} is {mime or 'unknown'}.")
        if path.stat().st_size > _IMAGE_CAP:
            raise ValueError(f"{path.name} is over Docs' 50 MB image limit.")
        files = auth.drive(account).files()
        perms = auth.drive(account).permissions()
        up = files.create(
            body={"name": f"(temporary image for a Doc) {path.name}"},
            media_body=MediaFileUpload(str(path), mimetype=mime),
            fields="id",
        ).execute()
        try:
            perms.create(fileId=up["id"], body={"type": "anyone", "role": "reader"}).execute()
            out = insert(f"https://drive.usercontent.google.com/download?id={up['id']}&export=download")
        finally:
            # The document now holds its own copy; don't leave the upload exposed.
            try:
                perms.delete(fileId=up["id"], permissionId="anyoneWithLink").execute()
            except HttpError:
                pass  # never shared (the create above failed); trashing still follows
            files.update(fileId=up["id"], body={"trashed": True}).execute()
    reply = next((r["insertInlineImage"] for r in out["replies"] if r.get("insertInlineImage")), {})
    return {"inserted_at": img_at, "object_id": reply.get("objectId"), **out}


@mcp.tool()
def docs_table_edit(
    account: AccountSlug,
    document_id: str,
    table_index: int,
    action: Literal["insert_row", "insert_column", "delete_row", "delete_column", "set_cell"],
    row: int,
    column: int = 0,
    text: str | None = None,
    below: bool = True,
    right: bool = True,
    tab_id: str | None = None,
    revision_id: str | None = None,
) -> dict:
    """Add, remove, or edit a row/column/cell of a table already in a Google
    Doc, in place. Use docs_insert / docs_replace_section (a Markdown pipe
    table) to create a table in the first place; use this to change one
    afterwards without rewriting the section around it.

    `table_index` is the table's position (0-based) among docs_get's
    `tables` for this tab — call docs_get first to find it and its current
    row/column counts. `row` / `column` (0-based) name the cell the action
    is anchored to:
    - "insert_row": a new blank row next to `row` — below it, or above with
      `below=False`.
    - "insert_column": a new blank column next to `column` — right of it, or
      left with `right=False`.
    - "delete_row" / "delete_column": removes that row / column.
    - "set_cell": replaces cell (`row`, `column`)'s text with `text`
      (required for this action; supports the same inline **bold**/*italic*/
      `code`/[link](url) markdown as docs_insert).
    """
    _, tab, revision = _docs_load(account, document_id, tab_id, revision_id)
    body = tab["body"]
    tables = docs_model.tables(body)
    if not 0 <= table_index < len(tables):
        raise ValueError(f"No table at table_index {table_index}; this tab has {len(tables)} table(s).")
    table = tables[table_index]
    loc_tab = tab["tab_id"]

    if not 0 <= row < table["rows"]:
        raise ValueError(f"row must be between 0 and {table['rows'] - 1} for this table, got {row}.")
    if action in ("set_cell", "insert_column", "delete_column") and not 0 <= column < table["columns"]:
        raise ValueError(f"column must be between 0 and {table['columns'] - 1} for this table, got {column}.")

    if action == "set_cell":
        if text is None:
            raise ValueError('action="set_cell" needs `text`.')
        cell = table["cells"][row][column]
        reqs: list[dict] = []
        if cell["end_index"] - 1 > cell["start_index"]:  # clear the existing content, its final newline kept
            rng = {"startIndex": cell["start_index"], "endIndex": cell["end_index"] - 1}
            if loc_tab:
                rng["tabId"] = loc_tab
            reqs.append({"deleteContentRange": {"range": rng}})
        runs = docs_markdown.parse_inline(text)
        full = "".join(t for t, _ in runs)
        loc = {"index": cell["start_index"], **({"tabId": loc_tab} if loc_tab else {})}
        reqs.append({"insertText": {"text": full, "location": loc}})
        pos = cell["start_index"]
        for t, style in runs:
            n = docs_model.utf16_len(t)
            if style:
                rng = {"startIndex": pos, "endIndex": pos + n, **({"tabId": loc_tab} if loc_tab else {})}
                reqs.append({"updateTextStyle": {"range": rng, "textStyle": style, "fields": ",".join(style)}})
            pos += n
        return {"table_index": table_index, "action": action, **_docs_batch(account, document_id, reqs, revision)}

    cell_loc = {
        "tableStartLocation": {"index": table["start_index"], **({"tabId": loc_tab} if loc_tab else {})},
        "rowIndex": row,
        "columnIndex": column,
    }
    if action == "insert_row":
        req = {"insertTableRow": {"tableCellLocation": cell_loc, "insertBelow": below}}
    elif action == "insert_column":
        req = {"insertTableColumn": {"tableCellLocation": cell_loc, "insertRight": right}}
    elif action == "delete_row":
        req = {"deleteTableRow": {"tableCellLocation": cell_loc}}
    else:
        req = {"deleteTableColumn": {"tableCellLocation": cell_loc}}
    return {"table_index": table_index, "action": action, **_docs_batch(account, document_id, [req], revision)}


# ─── Tasks (Google Tasks) ────────────────────────────────────────────────


def _task_summary(t: dict) -> dict:
    keys = ("id", "title", "status", "due", "notes", "parent",
            "position", "completed", "updated")
    return {k: t[k] for k in keys if k in t}


# ─── contacts ───────────────────────────────────────────────────────────
#
# Why this exists: Gmail shows the display name the SENDER supplies, so a
# recipient written as a bare address arrives as a raw address. The name for
# someone the user has only corresponded with lives in "other contacts", not
# in saved contacts, so both are searched. Read-only by scope.


_CONTACT_READ_MASK = "names,emailAddresses,organizations"
_OTHER_READ_MASK = "names,emailAddresses"


def _contact_rows(res: dict) -> list[dict]:
    """Flatten a People search response to {name, emails, organization}."""
    rows = []
    for hit in res.get("results") or []:
        person = hit.get("person") or {}
        names = person.get("names") or []
        emails = person.get("emailAddresses") or []
        orgs = person.get("organizations") or []
        rows.append({
            "name": names[0].get("displayName") if names else None,
            "emails": [e["value"] for e in emails if e.get("value")],
            "organization": orgs[0].get("name") if orgs else None,
        })
    return rows


def _people_search(account: str, query: str, page_size: int) -> list[dict]:
    """Search saved contacts, then other contacts. Warms the server-side cache.

    People search runs off a cache Google builds per session; the documented
    way to prime it is a request with an empty query. Rather than pay for that
    on every call, only warm up and retry when a search comes back empty.
    """
    svc = auth.people(account)

    def saved(q):
        return svc.people().searchContacts(
            query=q, pageSize=page_size, readMask=_CONTACT_READ_MASK).execute()

    def other(q):
        return svc.otherContacts().search(
            query=q, pageSize=page_size, readMask=_OTHER_READ_MASK).execute()

    rows = _contact_rows(saved(query)) + _contact_rows(other(query))
    if not rows:
        saved("")
        other("")
        rows = _contact_rows(saved(query)) + _contact_rows(other(query))

    seen, merged = set(), []
    for row in rows:
        key = (row["name"], tuple(row["emails"]))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged[:page_size]


@mcp.tool()
def contacts_search(
    account: AccountSlug,
    query: str,
    max_results: int = 10,
) -> dict:
    """Search the account's contacts by name, email address or company.

    Covers BOTH saved contacts and "other contacts" — the people Gmail
    recorded from correspondence but the user never saved — so someone who
    has only ever been emailed is still found.
    """
    page_size = max(1, min(int(max_results), 30))
    return {"contacts": _people_search(account, query, page_size)}


@mcp.tool()
def contacts_lookup(account: AccountSlug, email: str) -> dict:
    """Resolve ONE email address to the name to address that person by.

    Returns {"email", "name", "source"}. `source` is "contacts" (saved or
    other contacts) or "sent_mail" — the display name on a real message from
    that address, which is what Google itself shows — or null when nothing
    here knows the address, in which case the bare address is the correct
    form (a role mailbox like info@ usually lands here).

    Use it before writing a recipient: an address the account can name is
    addressed as "Firstname Lastname <addr@host>", never bare.
    """
    target = email.strip().lower()

    for row in _people_search(account, target, 30):
        if row["name"] and any(e.lower() == target for e in row["emails"]):
            return {"email": email, "name": row["name"], "source": "contacts"}

    # Fall back to what the address itself has signed mail as.
    res = auth.gmail(account).users().messages().list(
        userId="me", q=f"from:{target}", maxResults=1).execute()
    for ref in res.get("messages") or []:
        msg = auth.gmail(account).users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From"]).execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h.get("name", "").lower() != "from":
                continue
            name, addr = parseaddr(h.get("value", ""))
            if name and addr.lower() == target:
                return {"email": email, "name": name, "source": "sent_mail"}

    return {"email": email, "name": None, "source": None}


@mcp.tool()
def tasklist_list(account: AccountSlug) -> dict:
    """List the account's task lists (each has an id + title)."""
    res = auth.tasks(account).tasklists().list(maxResults=100).execute()
    return {"lists": [{"id": x["id"], "title": x.get("title", "")}
                      for x in res.get("items", [])]}


@mcp.tool()
def tasklist_create(account: AccountSlug, title: str) -> dict:
    """Create a new task list."""
    return auth.tasks(account).tasklists().insert(body={"title": title}).execute()


@mcp.tool()
def tasklist_delete(account: AccountSlug, tasklist_id: str) -> dict:
    """Delete a task list and all its tasks. Irreversible."""
    auth.tasks(account).tasklists().delete(tasklist=tasklist_id).execute()
    return {"deleted": tasklist_id}


@mcp.tool()
def task_list(
    account: AccountSlug,
    tasklist_id: str = "@default",
    show_completed: bool = False,
    show_hidden: bool = False,
    max_results: int = 100,
) -> dict:
    """List tasks in a list. Find ids via tasklist_list; '@default' is the account's default list."""
    res = (
        auth.tasks(account)
        .tasks()
        .list(
            tasklist=tasklist_id,
            showCompleted=show_completed,
            showHidden=show_hidden,
            maxResults=max_results,
        )
        .execute()
    )
    return {"tasks": [_task_summary(t) for t in res.get("items", [])]}


@mcp.tool()
def task_get(account: AccountSlug, task_id: str, tasklist_id: str = "@default") -> dict:
    """Fetch a single task."""
    return auth.tasks(account).tasks().get(tasklist=tasklist_id, task=task_id).execute()


@mcp.tool()
def task_create(
    account: AccountSlug,
    title: str,
    tasklist_id: str = "@default",
    notes: str | None = None,
    due: str | None = None,
    parent: str | None = None,
    previous: str | None = None,
) -> dict:
    """Create a task. `due` is RFC3339 (e.g. '2026-06-15T00:00:00Z') — Google Tasks
    keeps only the DATE part. `parent` makes it a subtask of that task id;
    `previous` orders it after that task id."""
    body: dict[str, Any] = {"title": title}
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    kwargs: dict[str, Any] = {"tasklist": tasklist_id, "body": body}
    if parent:
        kwargs["parent"] = parent
    if previous:
        kwargs["previous"] = previous
    return auth.tasks(account).tasks().insert(**kwargs).execute()


@mcp.tool()
def task_update(
    account: AccountSlug,
    task_id: str,
    tasklist_id: str = "@default",
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    status: Literal["needsAction", "completed"] | None = None,
) -> dict:
    """Patch a task's fields. status='completed' completes it (or use task_complete)."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    if status is not None:
        body["status"] = status
    return (
        auth.tasks(account)
        .tasks()
        .patch(tasklist=tasklist_id, task=task_id, body=body)
        .execute()
    )


@mcp.tool()
def task_complete(account: AccountSlug, task_id: str, tasklist_id: str = "@default") -> dict:
    """Mark a task completed (shortcut for status='completed')."""
    return (
        auth.tasks(account)
        .tasks()
        .patch(tasklist=tasklist_id, task=task_id, body={"status": "completed"})
        .execute()
    )


@mcp.tool()
def task_delete(account: AccountSlug, task_id: str, tasklist_id: str = "@default") -> dict:
    """Delete a task. Irreversible."""
    auth.tasks(account).tasks().delete(tasklist=tasklist_id, task=task_id).execute()
    return {"deleted": task_id}


@mcp.tool()
def task_move(
    account: AccountSlug,
    task_id: str,
    tasklist_id: str = "@default",
    parent: str | None = None,
    previous: str | None = None,
) -> dict:
    """Reposition a task: under `parent` (as a subtask) and/or after `previous` in the same list."""
    kwargs: dict[str, Any] = {"tasklist": tasklist_id, "task": task_id}
    if parent:
        kwargs["parent"] = parent
    if previous:
        kwargs["previous"] = previous
    return auth.tasks(account).tasks().move(**kwargs).execute()


# ─── entry ──────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()  # stdio transport — what Claude Code expects


if __name__ == "__main__":
    main()
