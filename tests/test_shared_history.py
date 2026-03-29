import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.memory import (
    add_message,
    check_and_compact,
    get_cron_history_thread_id,
    get_history,
    get_history_thread_id,
    get_inbound_history_role,
    get_recent_delivery_destinations,
    get_shared_history_thread_id,
)
from src.broker.schemas import InboundMessage
from src.config import settings
from src.db.models import Base, Message, Summary
from src.runners import agent_loop


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path):
    db_path = tmp_path / "state.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_history_thread_id_routes_cron_to_dedicated_thread():
    assert get_history_thread_id("cli") == get_shared_history_thread_id()
    assert get_history_thread_id("discord") == get_shared_history_thread_id()
    assert get_history_thread_id("cron") == get_cron_history_thread_id()
    assert get_history_thread_id("discord", sender_id="system_cron") == get_cron_history_thread_id()
    assert get_inbound_history_role("discord", sender_id="system_cron") == "system"


@pytest.mark.asyncio
async def test_user_history_excludes_cron_entries(session_factory, monkeypatch):
    monkeypatch.setattr(settings.memory, "cron_messages_as_system", True)

    async with session_factory() as session:
        shared_thread_id = get_shared_history_thread_id()
        cron_thread_id = get_cron_history_thread_id()

        await add_message(
            session,
            shared_thread_id,
            "cli",
            "user",
            "Remember that my favorite editor is Helix.",
            metadata={"transport_chat_id": "cli_chat"},
        )
        await add_message(
            session,
            cron_thread_id,
            "cron",
            get_inbound_history_role("cron"),
            "SYSTEM ALERT: Check the calendar.",
            metadata={"transport_chat_id": "cron_chat"},
        )
        await add_message(
            session,
            shared_thread_id,
            "discord",
            "assistant",
            "Noted. Your preferred editor is Helix.",
            metadata={"transport_chat_id": "discord_channel"},
        )

        user_history = await get_history(session, shared_thread_id)
        cron_history = await get_history(session, cron_thread_id)

    assert len(user_history) == 2
    assert isinstance(user_history[0], ModelRequest)
    assert isinstance(user_history[0].parts[0], UserPromptPart)
    assert user_history[0].parts[0].content == "Remember that my favorite editor is Helix."
    assert isinstance(user_history[1], ModelResponse)
    assert isinstance(user_history[1].parts[0], TextPart)
    assert user_history[1].parts[0].content == "Noted. Your preferred editor is Helix."

    assert len(cron_history) == 1
    assert isinstance(cron_history[0], ModelRequest)
    assert isinstance(cron_history[0].parts[0], SystemPromptPart)
    assert cron_history[0].parts[0].content == "SYSTEM ALERT: Check the calendar."


@pytest.mark.asyncio
async def test_recent_delivery_destinations_use_transport_chat_ids(session_factory):
    async with session_factory() as session:
        shared_thread_id = get_shared_history_thread_id()

        await add_message(
            session,
            shared_thread_id,
            "discord",
            "user",
            "Ping me there.",
            metadata={"transport_chat_id": "discord-123"},
        )
        await add_message(
            session,
            shared_thread_id,
            "discord",
            "assistant",
            "Will do.",
            metadata={"transport_chat_id": "discord-123"},
        )
        await add_message(
            session,
            shared_thread_id,
            "cli",
            "user",
            "Also mention it here.",
            metadata={"transport_chat_id": "cli_chat"},
        )

        recent = await get_recent_delivery_destinations(session)

    assert len(recent) == 2
    assert recent[0].startswith("Channel: cli, Chat ID: cli_chat")
    assert recent[1].startswith("Channel: discord, Chat ID: discord-123")


class _FakeArchiver:
    def __init__(self):
        self.calls = []

    async def summarize(self, messages, previous_summary=None):
        self.calls.append((messages, previous_summary))
        return "compacted summary"


@pytest.mark.asyncio
async def test_check_and_compact_runs_per_thread(session_factory, monkeypatch):
    monkeypatch.setattr(settings.memory, "max_conversation_history_len", 3)

    async with session_factory() as session:
        shared_thread_id = get_shared_history_thread_id()
        cron_thread_id = get_cron_history_thread_id()

        for index, channel in enumerate(["cli", "discord", "cli", "discord"], start=1):
            await add_message(
                session,
                shared_thread_id,
                channel,
                "user",
                f"message-{index}",
                metadata={"transport_chat_id": f"{channel}-{index}"},
            )

        for index in range(1, 3):
            await add_message(
                session,
                cron_thread_id,
                "cron",
                "system",
                f"cron-message-{index}",
                metadata={"transport_chat_id": f"cron-{index}"},
            )

        archiver = _FakeArchiver()
        await check_and_compact(session, shared_thread_id, archiver)
        await check_and_compact(session, cron_thread_id, archiver)

        valid_user_messages = (
            await session.execute(
                select(Message).where(Message.chat_id == shared_thread_id, Message.is_valid == True)  # noqa: E712
            )
        ).scalars().all()
        valid_cron_messages = (
            await session.execute(
                select(Message).where(Message.chat_id == cron_thread_id, Message.is_valid == True)  # noqa: E712
            )
        ).scalars().all()
        user_summaries = (await session.execute(select(Summary).where(Summary.chat_id == shared_thread_id))).scalars().all()
        cron_summaries = (await session.execute(select(Summary).where(Summary.chat_id == cron_thread_id))).scalars().all()

    assert len(archiver.calls) == 1
    assert len(user_summaries) == 1
    assert user_summaries[0].content == "compacted summary"
    assert len(valid_user_messages) == 2
    assert len(valid_cron_messages) == 2
    assert cron_summaries == []


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_tokens: int = 0


class _FakeResult:
    output = "Shared history reply"

    def usage(self):
        return _FakeUsage()


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def run(self, user_input, history=None, chat_id="cli", channel="cli", sender_id=None, history_thread_id=None):
        self.calls.append(
            {
                "user_input": user_input,
                "history_len": len(history or []),
                "chat_id": chat_id,
                "channel": channel,
                "sender_id": sender_id,
                "history_thread_id": history_thread_id,
            }
        )
        return _FakeResult()


class _FakeBus:
    def __init__(self, inbound_message):
        self.inbound_message = inbound_message
        self.outbound_messages = []
        self._delivered = False

    async def get_next_inbound(self):
        if self._delivered:
            raise asyncio.CancelledError()
        self._delivered = True
        return self.inbound_message

    async def publish_outbound(self, msg):
        self.outbound_messages.append(msg)


class _FailingManager:
    async def run(self, user_input, history=None, chat_id="cli", channel="cli", sender_id=None, history_thread_id=None):
        raise RuntimeError("forced failure")


@pytest.mark.asyncio
async def test_agent_loop_uses_shared_history_but_preserves_outbound_chat_id(session_factory, monkeypatch):
    monkeypatch.setattr("src.runners.async_session", session_factory)

    inbound = InboundMessage(
        sender_id="user-1",
        username="User",
        chat_id="discord-channel-42",
        content="What did I just say in CLI?",
        channel="discord",
        metadata={"message_id": "msg-1"},
    )

    bus = _FakeBus(inbound)
    manager = _FakeManager()
    archiver = _FakeArchiver()

    await agent_loop(bus, manager, archiver)

    assert len(bus.outbound_messages) == 1
    outbound = bus.outbound_messages[0]
    assert outbound.chat_id == "discord-channel-42"
    assert outbound.channel == "discord"
    assert outbound.content == "Shared history reply"

    async with session_factory() as session:
        rows = (await session.execute(select(Message).order_by(Message.id.asc()))).scalars().all()

    assert len(rows) == 2
    assert all(row.chat_id == get_shared_history_thread_id() for row in rows)
    assert rows[0].metadata_json["transport_chat_id"] == "discord-channel-42"
    assert rows[1].metadata_json["transport_chat_id"] == "discord-channel-42"
    assert manager.calls[0]["chat_id"] == "discord-channel-42"
    assert manager.calls[0]["channel"] == "discord"
    assert manager.calls[0]["sender_id"] == "user-1"
    assert manager.calls[0]["history_thread_id"] == get_shared_history_thread_id()


@pytest.mark.asyncio
async def test_agent_loop_routes_cron_to_dedicated_history_thread(session_factory, monkeypatch):
    monkeypatch.setattr("src.runners.async_session", session_factory)

    inbound = InboundMessage(
        sender_id="system_cron",
        username="System Cron",
        chat_id="cron_chat",
        content="Run the routine now.",
        channel="cron",
    )

    bus = _FakeBus(inbound)
    manager = _FakeManager()
    archiver = _FakeArchiver()

    await agent_loop(bus, manager, archiver)

    async with session_factory() as session:
        rows = (await session.execute(select(Message).order_by(Message.id.asc()))).scalars().all()

    assert len(rows) == 2
    assert all(row.chat_id == get_cron_history_thread_id() for row in rows)
    assert manager.calls[0]["history_thread_id"] == get_cron_history_thread_id()


@pytest.mark.asyncio
async def test_agent_loop_routes_system_cron_from_discord_to_cron_history_thread(session_factory, monkeypatch):
    monkeypatch.setattr("src.runners.async_session", session_factory)

    inbound = InboundMessage(
        sender_id="system_cron",
        username="System Cron",
        chat_id="discord-channel-42",
        content="Run the routine now.",
        channel="discord",
    )

    bus = _FakeBus(inbound)
    manager = _FakeManager()
    archiver = _FakeArchiver()

    await agent_loop(bus, manager, archiver)

    async with session_factory() as session:
        rows = (await session.execute(select(Message).order_by(Message.id.asc()))).scalars().all()

    assert len(rows) == 2
    assert all(row.chat_id == get_cron_history_thread_id() for row in rows)
    assert rows[0].role == "system"
    assert manager.calls[0]["history_thread_id"] == get_cron_history_thread_id()


@pytest.mark.asyncio
async def test_agent_loop_sends_error_reply_when_metadata_is_missing(session_factory, monkeypatch):
    monkeypatch.setattr("src.runners.async_session", session_factory)

    inbound = InboundMessage(
        sender_id="system_cron",
        username="System Cron",
        chat_id="cron_chat",
        content="Run the routine now.",
        channel="cron",
    )

    bus = _FakeBus(inbound)
    manager = _FailingManager()
    archiver = _FakeArchiver()

    await agent_loop(bus, manager, archiver)

    assert len(bus.outbound_messages) == 1
    outbound = bus.outbound_messages[0]
    assert outbound.chat_id == "cron_chat"
    assert outbound.channel == "cron"
    assert outbound.reply_to is None
    assert "forced failure" in outbound.content
