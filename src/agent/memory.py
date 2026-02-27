from typing import List
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Message
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart

async def add_message(session: AsyncSession, chat_id: str, channel: str, role: str, content: str, metadata: dict = None):
    message = Message(
        chat_id=chat_id,
        channel=channel,
        role=role,
        content=content,
        metadata_json=metadata
    )
    session.add(message)
    await session.commit()

async def get_history(session: AsyncSession, chat_id: str, limit: int = 20) -> List[ModelMessage]:
    result = await session.execute(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    
    history = []
    # Reverse to get chronological order for context
    for m in reversed(messages):
        if m.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
        elif m.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=m.content)]))
            
    return history
