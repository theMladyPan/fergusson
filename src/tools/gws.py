"""Native Google Workspace (`gws`) tools for the agent.

Wraps the `gws` CLI so the agent (and the cheap read-only router) can read
Gmail, Calendar, and Drive directly without shelling out through the generic
bash tool. The router gets the four read-only tools so it can answer/prepare
context cheaply; the write tool (``create_calendar_event``) stays on the Core
Agent.

Design:
- ``_run_gws`` shells out to the ``gws`` binary with a sanitized environment
  (``GOOGLE_APPLICATION_CREDENTIALS`` is stripped so the STT service-account
  ADC can never leak in). Auth failures are detected and surfaced as a single
  ``ModelRetry`` that tells the agent to load the ``gws-debug`` skill — fail
  ultra fast, never retry the command.
- ``_summarize`` calls ``settings.summary_model`` (default OpenRouter
  gpt-oss-20b nitro, medium reasoning) to condense emails/events/docs. If the
  summary model is unavailable, tools degrade gracefully and return raw data.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from pydantic_ai import Agent, ModelRetry

from src.config import settings

# --------------------------------------------------------------------------- #
# Subprocess helper
# --------------------------------------------------------------------------- #

_AUTH_MARKERS = (
    "gws auth login",
    "no credentials",
    "google_workspace_cli_credentials_file",
    "access denied",
    "permission denied",
    "invalid_grant",
    "401",
)


def _is_auth_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


async def _run_gws(args: list[str], timeout: int | None = None) -> str:
    """Run a `gws` subcommand and return stdout text.

    Strips ``GOOGLE_APPLICATION_CREDENTIALS`` from the child environment so the
    STT service-account ADC can never be inherited by `gws`. Fails fast with
    ``ModelRetry`` on auth errors (never retries), timeouts, or non-zero exits.

    Raises:
        ModelRetry: On missing binary, auth failure, timeout, or command error.
    """
    binary = settings.gws.binary
    timeout = timeout if timeout is not None else settings.gws.tool_timeout
    # Defense in depth: never let gws fall back to the STT service-account ADC.
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_APPLICATION_CREDENTIALS"}

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ModelRetry("Google Workspace CLI (`gws`) is not installed.") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ModelRetry(f"gws command timed out after {timeout}s; escalate to avoid retry loops.") from None

    stderr_text = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        if _is_auth_error(stderr_text):
            raise ModelRetry(
                "Google Workspace auth missing or invalid. Load the `gws-debug` skill and run "
                "`gws auth login`; do not retry the same command."
            )
        first_line = stderr_text.splitlines()[0] if stderr_text else f"exit code {proc.returncode}"
        raise ModelRetry(f"gws command failed: {first_line}")

    return stdout.decode(errors="replace")


# --------------------------------------------------------------------------- #
# Summary helper
# --------------------------------------------------------------------------- #

_summarizer_agent: Agent | None = None
_summarizer_unavailable = False


def _get_summarizer_agent() -> Agent | None:
    """Lazily build (and cache) the summary-model agent.

    Returns ``None`` if the model cannot be constructed, so tools degrade to
    raw data instead of hard-failing the whole read.
    """
    global _summarizer_agent, _summarizer_unavailable
    if _summarizer_unavailable:
        return None
    if _summarizer_agent is None:
        try:
            _summarizer_agent = Agent(
                settings.summary_model,
                system_prompt=(
                    "You are a concise summarizer. Produce only the requested summary, "
                    "no preamble, no labels unless asked. Match the language of the content."
                ),
            )
        except Exception:  # pragma: no cover - defensive, depends on env
            _summarizer_unavailable = True
            return None
    return _summarizer_agent


async def _summarize(text: str, instruction: str) -> str | None:
    """Run the summary model on `text` with `instruction`.

    Returns the summary, or ``None`` if the summary model is unavailable or the
    request fails (callers fall back to raw data).
    """
    if not text.strip():
        return None
    agent = _get_summarizer_agent()
    if agent is None:
        return None
    model_settings: dict = {}
    if settings.summary_model.startswith("openrouter:"):
        model_settings["openrouter_reasoning"] = {"effort": settings.summary_reasoning_effort}
    try:
        result = await agent.run(
            f"{instruction}\n\nContent:\n{text[: settings.gws.export_char_limit]}",
            model_settings=model_settings,
        )
        return str(result.output).strip()
    except Exception:  # pragma: no cover - depends on network/credentials
        return None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def _pick(d: dict, *keys: str, default=""):
    """Read the first non-empty value for any of `keys` (case-insensitive)."""
    for k in keys:
        if d.get(k):
            return d[k]
    lowered = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v:
            return v
    return default


def _as_list(raw: str) -> list:
    """Parse JSON that may be a bare list or an object wrapping a list."""
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


# --------------------------------------------------------------------------- #
# Tools — read-only (router + core agent)
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")


async def list_inbox_emails(limit: int = 10, summarize: bool = False) -> str:
    """List recent inbox emails (read + unread, not archived), newest first.

    Available to the router for quick inbox scans. By default returns sender,
    subject, date, and a snippet per email (one fast `gws +triage` call). Set
    `summarize=True` only when the user wants the body summarized — that fetches
    each message body and runs the flash-lite summary model, which is slower.

    Args:
        limit: Number of emails to return (1..50, default 10).
        summarize: If True, fetch each body and produce a 2-3 sentence summary.

    Returns:
        A compact inbox digest.
    """
    if not 1 <= limit <= 50:
        raise ModelRetry("`limit` must be between 1 and 50.")

    raw = await _run_gws([
        "gmail", "+triage", "--max", str(limit),
        "--query", "in:inbox", "--format", "json",
    ])
    items = _as_list(raw)
    if not items:
        return "Inbox is empty."

    lines = [f"Inbox ({len(items)} emails, newest first):"]
    for i, item in enumerate(items, 1):
        sender = _pick(item, "from", "sender")
        subject = _pick(item, "subject")
        date = _pick(item, "date", "internalDate")
        lines.append(f"{i}. {sender} — {subject}  ({date})")

    if summarize:
        # Fetch bodies + summarize in parallel (one round trip per email is the
        # bottleneck; running them concurrently keeps the summarize path fast).
        async def _email_summary(item: dict, fallback: str) -> str:
            msg_id = _pick(item, "id", "messageId")
            if not msg_id:
                return fallback
            body = await _read_message_body(msg_id)
            summary = await _summarize(
                f"From: {_pick(item, 'from', 'sender')}\nSubject: {_pick(item, 'subject')}\n\n{body}",
                "Summarize this email in 2-3 sentences, focusing on what the sender wants and any action needed.",
            )
            return summary if summary else fallback

        snippets = [_pick(item, "snippet") for item in items]
        summaries = await asyncio.gather(*(_email_summary(it, snip) for it, snip in zip(items, snippets)))
        for i, summary in enumerate(summaries, 1):
            lines.append(f"   Summary: {summary}")
    else:
        for i, item in enumerate(items, 1):
            snippet = _pick(item, "snippet")
            if snippet:
                lines.append(f"   {snippet}")
    return "\n".join(lines)


async def _read_message_body(message_id: str) -> str:
    """Fetch the plain-text body of a Gmail message via `gws gmail +read`."""
    try:
        return await _run_gws(["gmail", "+read", "--id", message_id, "--format", "text"])
    except ModelRetry:
        return ""


async def get_contact(query: str, kind: str = "email") -> str:
    """Find an email address or phone number for a person by mining email threads.

    Searches sent/received mail for `query` (a name or email fragment). For
    `kind="email"` (default) it extracts addresses from the matching messages
    in one fast call. For `kind="phone"` it fetches up to 3 message bodies and
    asks the summary model to extract phone numbers from signatures/bodies.

    Args:
        query: Person name or email fragment (e.g. "kubo", "jakub@example.com").
        kind: "email" or "phone".

    Returns:
        The found contact value(s) with a note on how many threads matched.
    """
    kind = kind.lower()
    if kind not in ("email", "phone"):
        raise ModelRetry("`kind` must be 'email' or 'phone'.")

    gmail_query = f"from:{query} OR to:{query}"
    raw = await _run_gws([
        "gmail", "+triage", "--max", "10",
        "--query", gmail_query, "--format", "json",
    ])
    items = _as_list(raw)
    if not items:
        return f"No email threads found for '{query}'."

    if kind == "email":
        found: list[str] = []
        for item in items:
            blob = " ".join(str(v) for v in (
                _pick(item, "from", "sender"), _pick(item, "to"), _pick(item, "snippet")
            ) if v)
            found.extend(_EMAIL_RE.findall(blob))
        unique = list(dict.fromkeys(found))
        if not unique:
            return f"No email addresses found in {len(items)} threads for '{query}'."
        return f"Emails for '{query}' (from {len(items)} threads): {', '.join(unique)}"

    # phone: mine bodies in parallel
    msg_ids = [_pick(item, "id", "messageId") for item in items[:3] if _pick(item, "id", "messageId")]
    bodies = [b for b in await asyncio.gather(*(_read_message_body(mid) for mid in msg_ids)) if b]
    if not bodies:
        return f"No readable message bodies for '{query}' to scan for a phone number."
    extracted = await _summarize(
        "\n---\n".join(bodies),
        "Extract any phone numbers from these email bodies/signatures. Return only the phone numbers, one per line, or 'none' if there are none.",
    )
    if not extracted or extracted.strip().lower() == "none":
        return f"No phone numbers found for '{query}' in {len(items)} threads."
    return f"Phone numbers for '{query}' (from {len(items)} threads):\n{extracted}"


async def list_upcoming_events(days: int = 7, summarize: bool = False) -> str:
    """List upcoming calendar events across all calendars for the next N days.

    Available to the router for quick agenda reads. Set `summarize=True` to add
    a one-line flash-lite summary per event (useful for busy days).

    Args:
        days: Number of days ahead to show (1..30, default 7).
        summarize: If True, add a one-sentence summary per event.

    Returns:
        A compact agenda digest.
    """
    if not 1 <= days <= 30:
        raise ModelRetry("`days` must be between 1 and 30.")

    raw = await _run_gws(["calendar", "+agenda", "--days", str(days), "--format", "json"])
    items = _as_list(raw)
    if not items:
        return f"No upcoming events in the next {days} days."

    lines = [f"Upcoming events (next {days} days):"]

    # Summarize in parallel before building blocks so summaries interleave per event.
    if summarize:
        async def _event_summary(item: dict) -> str | None:
            return await _summarize(
                f"Title: {_pick(item, 'summary')}\nLocation: {_pick(item, 'location')}\nDescription: {_pick(item, 'description')}",
                "Summarize this calendar event in one sentence.",
            )

        summaries = await asyncio.gather(*(_event_summary(it) for it in items))
    else:
        summaries = [None] * len(items)

    for i, item in enumerate(items, 1):
        summary_text = _pick(item, "summary")
        start = _pick(item, "start")
        if isinstance(start, dict):
            start = _pick(start, "dateTime", "date")
        end = _pick(item, "end")
        if isinstance(end, dict):
            end = _pick(end, "dateTime", "date")
        location = _pick(item, "location")
        attendees_raw = _pick(item, "attendees", default=[])
        attendees = []
        if isinstance(attendees_raw, list):
            for a in attendees_raw:
                if isinstance(a, dict):
                    attendees.append(_pick(a, "email"))
                elif isinstance(a, str):
                    attendees.append(a)
        lines.append(f"{i}. {summary_text}  ({start} → {end})")
        if location:
            lines.append(f"   Location: {location}")
        if attendees:
            lines.append(f"   Attendees: {', '.join(attendees)}")
        if summaries[i - 1]:
            lines.append(f"   Summary: {summaries[i - 1]}")
    return "\n".join(lines)


async def search_drive_docs(query: str, limit: int = 10) -> str:
    """Search Google Drive for documents matching `query` and summarize each.

    Lists matching files (name, type, modified time, link) and, for each, tries
    to export it as plain text and produces a one-sentence flash-lite summary.
    Binary files that cannot be exported are listed without a summary.

    Args:
        query: Name or content search fragment.
        limit: Max files to return (1..20, default 10).

    Returns:
        A compact list of matching documents with one-sentence summaries.
    """
    if not query.strip():
        raise ModelRetry("`query` must not be empty.")
    if not 1 <= limit <= 20:
        raise ModelRetry("`limit` must be between 1 and 20.")

    params = json.dumps({
        "q": f"name contains '{query}' and trashed = false",
        "pageSize": limit,
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
    })
    raw = await _run_gws(["drive", "files", "list", "--params", params, "--format", "json"])
    files = _as_list(raw)
    if not files:
        return f"No Drive documents found for '{query}'."

    lines = [f"Drive results for '{query}' ({len(files)}):"]
    # Export + summarize each doc in parallel (export is the bottleneck).
    summaries = await asyncio.gather(*(_summarize_drive_doc(_pick(f, "id"), _pick(f, "mimeType")) for f in files))
    for i, (f, summary) in enumerate(zip(files, summaries), 1):
        name = _pick(f, "name")
        mime = _pick(f, "mimeType")
        modified = _pick(f, "modifiedTime")
        link = _pick(f, "webViewLink")
        lines.append(f"{i}. {name} ({mime}, modified {modified})")
        if link:
            lines.append(f"   Link: {link}")
        if summary:
            lines.append(f"   Summary: {summary}")
        else:
            lines.append("   Summary: [content not available]")
    return "\n".join(lines)


async def _summarize_drive_doc(file_id: str, mime: str) -> str | None:
    """Export a Drive file as plain text and summarize it in one sentence."""
    if not file_id:
        return None
    params = json.dumps({"fileId": file_id, "mimeType": "text/plain"})
    try:
        body = await _run_gws(["drive", "files", "export", "--params", params], timeout=20)
    except ModelRetry:
        return None
    if not body.strip():
        return None
    return await _summarize(
        body,
        "Summarize this document in one sentence. If it is not meaningful text, return 'none'.",
    )


# --------------------------------------------------------------------------- #
# Tool — write (Core Agent only)
# --------------------------------------------------------------------------- #

async def create_calendar_event(
    summary: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    add_meet: bool = False,
    location: str | None = None,
    description: str | None = None,
) -> str:
    """Create a new event in the primary calendar, optionally with attendees and Meet.

    Core-Agent only (the router escalates writes). Times are ISO 8601 with
    timezone, e.g. ``2026-03-18T09:00:00+01:00`` (passthrough to `gws`).

    Args:
        summary: Event title.
        start: Start time (ISO 8601 with timezone).
        end: End time (ISO 8601 with timezone).
        attendees: Optional list of attendee emails to invite.
        add_meet: If True, add a Google Meet conference link.
        location: Optional location text.
        description: Optional description/body.

    Returns:
        A confirmation string with the event id and link.
    """
    if not summary.strip():
        raise ModelRetry("`summary` must not be empty.")
    if not start.strip() or not end.strip():
        raise ModelRetry("`start` and `end` (ISO 8601) are required.")

    args = ["calendar", "+insert", "--summary", summary, "--start", start, "--end", end]
    if location:
        args += ["--location", location]
    if description:
        args += ["--description", description]
    for email in attendees or []:
        if not _EMAIL_RE.fullmatch(email):
            raise ModelRetry(f"Invalid attendee email: {email}")
        args += ["--attendee", email]
    if add_meet:
        args.append("--meet")

    raw = await _run_gws(args)
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return f"Event '{summary}' created."

    event_id = _pick(event, "id") or "?"
    link = _pick(event, "htmlLink") or _pick(event, "hangoutLink") or ""
    parts = [f"Event created: '{summary}' (id={event_id})"]
    if link:
        parts.append(f"Link: {link}")
    return " | ".join(parts)
