from typing import List

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.config import settings
from src.db.models import Message, Summary


def get_shared_history_thread_id() -> str:
    return settings.shared_history_thread_id


def get_inbound_history_role(channel: str) -> str:
    if channel == "cron" and settings.cron_messages_as_system:
        return "system"
    return "user"


async def get_recent_delivery_destinations(session: AsyncSession, limit: int = 100) -> list[str]:
    result = await session.execute(
        select(Message)
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    messages = result.scalars().all()

    seen = set()
    recent_chats = []
    for msg in messages:
        metadata = msg.metadata_json or {}
        transport_chat_id = metadata.get("transport_chat_id", msg.chat_id)
        key = (msg.channel, transport_chat_id)
        if key not in seen:
            seen.add(key)
            recent_chats.append(
                f"Channel: {msg.channel}, Chat ID: {transport_chat_id}, Last Active: {msg.timestamp}"
            )

    return recent_chats


async def add_message(
    session: AsyncSession,
    history_thread_id: str,
    channel: str,
    role: str,
    content: str,
    metadata: dict | None = None,
):
    message = Message(
        chat_id=history_thread_id,
        channel=channel,
        role=role,
        content=content,
        metadata_json=metadata,
        is_valid=True,
    )
    session.add(message)
    await session.commit()


async def get_history(session: AsyncSession, history_thread_id: str, limit: int = 20) -> List[ModelMessage]:
    # 1. Fetch the latest Summary for this shared thread
    result = await session.execute(
        select(Summary).where(Summary.chat_id == history_thread_id).order_by(Summary.timestamp.desc()).limit(1)
    )
    summary: Summary | None = result.scalars().first()

    # 2. Fetch valid messages
    result = await session.execute(
        select(Message)
        .where(Message.chat_id == history_thread_id, Message.is_valid == True)  # noqa: E712
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    messages = result.scalars().all()

    history = []

    if summary:
        # PydanticAI usually handles SystemPromptPart.
        history.append(
            ModelRequest(
                parts=[
                    SystemPromptPart(
                        content=f"# Prior Conversation Summary:\n{summary.content}\n\n---\n",
                    ),
                ]
            )
        )

    # Reverse to get chronological order for context
    for m in reversed(messages):
        if m.role == "system":
            history.append(ModelRequest(parts=[SystemPromptPart(content=m.content)]))
        elif m.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
        elif m.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=m.content)]))

    return history


async def check_and_compact(session: AsyncSession, history_thread_id: str, archiver_agent: "Archiver") -> None:
    """
    Checks if message history exceeds the maximum length defined in settings.
    If so, compacts the oldest half of the messages into a summary and archives them.
    """
    from sqlalchemy import func

    # Check count of valid messages
    stmt = select(func.count(Message.id)).where(
        Message.chat_id == history_thread_id, Message.is_valid == True
    )  # noqa: E712
    count_result = await session.execute(stmt)
    count = count_result.scalar()

    if count > settings.max_conversation_history_len:
        # Fetch oldest messages to compact (approx 2 thirds to allow some buffer)
        stmt = (
            select(Message)
            .where(Message.chat_id == history_thread_id, Message.is_valid == True)  # noqa: E712
            .order_by(Message.timestamp.asc())
            .limit(settings.max_conversation_history_len * 2 // 3)
        )
        result = await session.execute(stmt)
        messages_to_compact = result.scalars().all()

        if not messages_to_compact:
            return

        # Fetch previous summary
        prev_summary_result = await session.execute(
            select(Summary).where(Summary.chat_id == history_thread_id).order_by(Summary.timestamp.desc()).limit(1)
        )
        previous_summary: Summary | None = prev_summary_result.scalars().first()
        previous_summary_content = previous_summary.content if previous_summary else None

        # Generate summary
        summary_text = await archiver_agent.summarize(
            messages_to_compact,
            previous_summary=previous_summary_content,
        )

        # Save Summary
        summary = Summary(
            chat_id=history_thread_id,
            content=summary_text,
            range_start_id=messages_to_compact[0].id,
            range_end_id=messages_to_compact[-1].id,
        )
        session.add(summary)

        # Archive messages
        for msg in messages_to_compact:
            msg.is_valid = False

        await session.commit()
