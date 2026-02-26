from typing import List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import User, Conversation, Message
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart
import json

async def get_or_create_user(session: AsyncSession, user_id: str, username: str) -> User:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id, username=username)
        session.add(user)
        await session.commit()
    return user

async def get_or_create_conversation(session: AsyncSession, conversation_id: str, user_id: str) -> Conversation:
    result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    conv = result.scalar_one_or_none()
    if not conv:
        conv = Conversation(id=conversation_id, user_id=user_id)
        session.add(conv)
        await session.commit()
    return conv

async def add_message(session: AsyncSession, conversation_id: str, role: str, content: str, metadata: dict = None):
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        metadata_json=metadata
    )
    session.add(message)
    await session.commit()

async def get_history(session: AsyncSession, conversation_id: str, limit: int = 20) -> List[ModelMessage]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    # Pydantic-AI history format
    # This is a simplified conversion. Pydantic-AI uses specific message types.
    # For a robust implementation, we'd store the full JSON of ModelMessage.
    
    history = []
    # Reverse to get chronological order
    for m in reversed(messages):
        if m.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
        elif m.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=m.content)]))
            
    return history
