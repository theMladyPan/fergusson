from typing import List
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Message, Summary
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart, SystemPromptPart

async def add_message(session: AsyncSession, chat_id: str, channel: str, role: str, content: str, metadata: dict = None):
    message = Message(
        chat_id=chat_id,
        channel=channel,
        role=role,
        content=content,
        metadata_json=metadata,
        is_valid=True
    )
    session.add(message)
    await session.commit()

async def get_history(session: AsyncSession, chat_id: str, limit: int = 20) -> List[ModelMessage]:
    # 1. Fetch the latest Summary for this chat
    result = await session.execute(
        select(Summary)
        .where(Summary.chat_id == chat_id)
        .order_by(Summary.timestamp.desc())
        .limit(1)
    )
    summary: Summary | None = result.scalars().first()

    # 2. Fetch valid messages
    result = await session.execute(
        select(Message)
        .where(Message.chat_id == chat_id, Message.is_valid == True)
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    
    history = []
    
    if summary:
        # PydanticAI usually handles SystemPromptPart.
        history.append(ModelRequest(parts=[SystemPromptPart(content=f"Prior Conversation Summary:\n{summary.content}")]))

    # Reverse to get chronological order for context
    for m in reversed(messages):
        if m.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
        elif m.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=m.content)]))
            
    return history

async def check_and_compact(session: AsyncSession, chat_id: str, archiver_agent: 'Archiver') -> None:
    """
    Checks if message history exceeds 26 messages.
    If so, compacts the oldest 13 messages into a summary and archives them.
    """
    from sqlalchemy import func
    
    # Check count of valid messages
    count_result = await session.execute(
        select(func.count()).select_from(Message).where(Message.chat_id == chat_id, Message.is_valid == True)
    )
    count = count_result.scalar()
    
    if count > 26:
        # Fetch oldest 13 messages
        result = await session.execute(
            select(Message)
            .where(Message.chat_id == chat_id, Message.is_valid == True)
            .order_by(Message.timestamp.asc())
            .limit(13)
        )
        messages_to_compact = result.scalars().all()
        
        if not messages_to_compact:
            return

        # Generate summary
        summary_text = await archiver_agent.summarize(messages_to_compact)
        
        # Save Summary
        summary = Summary(
            chat_id=chat_id,
            content=summary_text,
            range_start_id=messages_to_compact[0].id,
            range_end_id=messages_to_compact[-1].id
        )
        session.add(summary)
        
        # Archive messages
        for msg in messages_to_compact:
            msg.is_valid = False
            
        await session.commit()
