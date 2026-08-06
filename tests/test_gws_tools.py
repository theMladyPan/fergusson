"""Tests for the native Google Workspace (`gws`) tools."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from pydantic_ai import ModelRetry

from src.tools import all_tools
from src.tools import gws as gws_mod
from src.tools.gws import (
    create_calendar_event,
    get_contact,
    list_inbox_emails,
    list_upcoming_events,
    search_drive_docs,
)


def _patch_run(monkeypatch, return_value: str):
    mock = AsyncMock(return_value=return_value)
    monkeypatch.setattr(gws_mod, "_run_gws", mock)
    return mock


def _patch_summarize(monkeypatch, return_value: str | None):
    mock = AsyncMock(return_value=return_value)
    monkeypatch.setattr(gws_mod, "_summarize", mock)
    return mock


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def test_all_five_gws_tools_registered():
    names = {t.__name__ for t in all_tools}
    assert {
        "list_inbox_emails",
        "get_contact",
        "list_upcoming_events",
        "search_drive_docs",
        "create_calendar_event",
    } <= names


# --------------------------------------------------------------------------- #
# list_inbox_emails
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_list_inbox_emails_non_summarize_one_call(monkeypatch):
    triage = json.dumps([
        {"id": "m1", "from": "a@x.com", "subject": "S1", "date": "2026-01-01", "snippet": "hi"},
        {"id": "m2", "from": "b@x.com", "subject": "S2", "date": "2026-01-02", "snippet": "yo"},
    ])
    run = _patch_run(monkeypatch, triage)
    summ = _patch_summarize(monkeypatch, "SUM")

    out = await list_inbox_emails(limit=10, summarize=False)

    assert "a@x.com — S1" in out
    assert "Inbox (2 emails" in out
    # summarize=False must not fetch bodies or call the summary model.
    assert run.await_count == 1
    summ.await_count == 0


@pytest.mark.asyncio
async def test_list_inbox_emails_summarize_fetches_bodies(monkeypatch):
    triage = json.dumps([{"id": "m1", "from": "a@x.com", "subject": "S1", "date": "d", "snippet": "snip"}])
    run = _patch_run(monkeypatch, triage)
    # second call onwards is the body read
    run.side_effect = [triage, "BODY TEXT"]
    _patch_summarize(monkeypatch, "A short summary.")

    out = await list_inbox_emails(limit=1, summarize=True)

    assert run.await_count == 2  # triage + one body read
    assert "Summary: A short summary." in out


@pytest.mark.asyncio
async def test_list_inbox_emails_summarize_runs_in_parallel(monkeypatch):
    """Body fetches for summarize=True must run concurrently, not sequentially."""
    import asyncio as _asyncio
    triage = json.dumps([
        {"id": "m1", "from": "a@x.com", "subject": "S1", "date": "d", "snippet": "s"},
        {"id": "m2", "from": "b@x.com", "subject": "S2", "date": "d", "snippet": "s"},
        {"id": "m3", "from": "c@x.com", "subject": "S3", "date": "d", "snippet": "s"},
    ])
    peak = 0
    inflight = 0
    lock = _asyncio.Lock()

    async def tracked_body(args, timeout=None):
        nonlocal peak, inflight
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        await _asyncio.sleep(0.05)
        async with lock:
            inflight -= 1
        return "BODY"

    async def triage_only(args, timeout=None):
        return triage

    run = AsyncMock(side_effect=triage_only)
    # First call is triage; subsequent calls (body reads) go through tracked_body.
    call_indices = {0}
    original_run = gws_mod._run_gws

    async def dispatch(args, timeout=None):
        # Heuristic: triage command contains "+triage"; body read contains "+read".
        if "+triage" in args:
            return await triage_only(args, timeout)
        return await tracked_body(args, timeout)

    monkeypatch.setattr(gws_mod, "_run_gws", dispatch)
    _patch_summarize(monkeypatch, "summary")

    await list_inbox_emails(limit=3, summarize=True)

    assert peak >= 2, f"body reads were sequential (peak concurrency={peak})"


@pytest.mark.asyncio
async def test_list_inbox_emails_empty(monkeypatch):
    _patch_run(monkeypatch, "[]")
    assert await list_inbox_emails() == "Inbox is empty."


@pytest.mark.asyncio
async def test_list_inbox_emails_invalid_limit(monkeypatch):
    with pytest.raises(ModelRetry):
        await list_inbox_emails(limit=0)


# --------------------------------------------------------------------------- #
# get_contact
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_contact_email_extracts_addresses(monkeypatch):
    triage = json.dumps([
        {"id": "1", "from": "kubo <kubo@example.com>", "to": "me", "snippet": "see you"},
        {"id": "2", "from": "x", "to": "kubo@example.com", "snippet": "ok"},
    ])
    run = _patch_run(monkeypatch, triage)
    summ = _patch_summarize(monkeypatch, None)

    out = await get_contact("kubo", kind="email")

    assert "kubo@example.com" in out
    assert run.await_count == 1  # single triage call, no per-message fetch
    assert summ.await_count == 0


@pytest.mark.asyncio
async def test_get_contact_phone_mines_bodies(monkeypatch):
    triage = json.dumps([{"id": "1", "from": "k", "snippet": "s"}, {"id": "2", "from": "k", "snippet": "s"}])
    run = _patch_run(monkeypatch, triage)
    run.side_effect = [triage, "BODY1", "BODY2"]
    _patch_summarize(monkeypatch, "+421 900 123 456")

    out = await get_contact("kubo", kind="phone")

    assert "+421 900 123 456" in out
    # triage + up to 3 body reads
    assert run.await_count >= 2


@pytest.mark.asyncio
async def test_get_contact_no_threads(monkeypatch):
    _patch_run(monkeypatch, "[]")
    assert "No email threads found" in await get_contact("nobody")


@pytest.mark.asyncio
async def test_get_contact_invalid_kind(monkeypatch):
    with pytest.raises(ModelRetry):
        await get_contact("x", kind="fax")


# --------------------------------------------------------------------------- #
# list_upcoming_events
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_list_upcoming_events_parses_agenda(monkeypatch):
    agenda = json.dumps([
        {"summary": "Dentist", "start": {"dateTime": "2026-01-01T09:00:00"}, "end": {"dateTime": "2026-01-01T10:00:00"}, "location": "Clinic", "attendees": [{"email": "doc@x.com"}]},
    ])
    run = _patch_run(monkeypatch, agenda)
    summ = _patch_summarize(monkeypatch, "SUM")

    out = await list_upcoming_events(days=7, summarize=False)

    assert "Dentist" in out
    assert "Clinic" in out
    assert "doc@x.com" in out
    assert run.await_count == 1
    assert summ.await_count == 0


@pytest.mark.asyncio
async def test_list_upcoming_events_invalid_days(monkeypatch):
    with pytest.raises(ModelRetry):
        await list_upcoming_events(days=99)


# --------------------------------------------------------------------------- #
# search_drive_docs
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_search_drive_docs_lists_and_summarizes(monkeypatch):
    listing = json.dumps({"files": [{"id": "f1", "name": "Report", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-01-01", "webViewLink": "https://link"}]})
    body = "This report covers Q1 results and revenue."
    run = _patch_run(monkeypatch, listing)
    run.side_effect = [listing, body]
    _patch_summarize(monkeypatch, "Q1 revenue summary.")

    out = await search_drive_docs("Report", limit=5)

    assert "Report" in out
    assert "https://link" in out
    assert "Q1 revenue summary." in out
    # list + one export call
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_search_drive_docs_export_failure_degrades(monkeypatch):
    listing = json.dumps({"files": [{"id": "f1", "name": "Bin", "mimeType": "application/pdf"}]})
    run = _patch_run(monkeypatch, listing)
    run.side_effect = [listing, ModelRetry("export failed")]
    _patch_summarize(monkeypatch, None)

    out = await search_drive_docs("Bin")
    assert "[content not available]" in out


@pytest.mark.asyncio
async def test_search_drive_docs_empty_query(monkeypatch):
    with pytest.raises(ModelRetry):
        await search_drive_docs("")


# --------------------------------------------------------------------------- #
# create_calendar_event
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_calendar_event_builds_insert_args(monkeypatch):
    event = json.dumps({"id": "evt1", "htmlLink": "https://cal/event"})
    run = _patch_run(monkeypatch, event)

    out = await create_calendar_event(
        summary="Lunch",
        start="2026-03-20T12:00:00+01:00",
        end="2026-03-20T13:00:00+01:00",
        attendees=["sam@example.com"],
        add_meet=True,
    )

    args = run.await_args.args[0]
    assert "calendar" in args and "+insert" in args
    assert "--summary" in args and "Lunch" in args
    assert "--start" in args and "2026-03-20T12:00:00+01:00" in args
    assert "--meet" in args
    assert "--attendee" in args and "sam@example.com" in args
    assert "evt1" in out
    assert "https://cal/event" in out


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_bad_email(monkeypatch):
    with pytest.raises(ModelRetry):
        await create_calendar_event(summary="X", start="s", end="e", attendees=["not-an-email"])


@pytest.mark.asyncio
async def test_create_calendar_event_requires_summary():
    import asyncio
    with pytest.raises(ModelRetry):
        await create_calendar_event(summary=" ", start="s", end="e")


# --------------------------------------------------------------------------- #
# _run_gws failure modes
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_run_gws_missing_binary(monkeypatch):
    import asyncio
    async def boom(*a, **k):
        raise FileNotFoundError("no gws")
    monkeypatch.setattr(gws_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(ModelRetry, match="not installed"):
        await gws_mod._run_gws(["gmail", "+triage"])


@pytest.mark.asyncio
async def test_run_gws_auth_error_fails_fast(monkeypatch):
    class _Proc:
        returncode = 1
        async def communicate(self):
            return (b"", b"No credentials provided. Run gws auth login")
    async def fake_exec(*a, **k):
        return _Proc()
    monkeypatch.setattr(gws_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(ModelRetry, match="gws-debug"):
        await gws_mod._run_gws(["gmail", "+triage"])


@pytest.mark.asyncio
async def test_run_gws_nonzero_error(monkeypatch):
    class _Proc:
        returncode = 2
        async def communicate(self):
            return (b"", b"some runtime error")
    async def fake_exec(*a, **k):
        return _Proc()
    monkeypatch.setattr(gws_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(ModelRetry, match="gws command failed"):
        await gws_mod._run_gws(["gmail", "+triage"])
