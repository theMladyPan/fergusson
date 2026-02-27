from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, Text, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class Message(Base):
    """A simplified table for all conversations, grouped by chat_id (e.g., CLI, Discord thread)."""
    __tablename__ = "messages"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String, index=True)  # To separate threads/channels
    channel: Mapped[str] = mapped_column(String, default="unknown")  # e.g., 'discord', 'cli'
    role: Mapped[str] = mapped_column(String)  # 'user', 'assistant'
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
