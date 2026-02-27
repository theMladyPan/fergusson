from datetime import datetime
from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Based on RunUsage but simplified for our needs."""

    input: int
    output: int
    cache: int


class MessageMetadata(BaseModel):
    token_usage: TokenUsage | None = None
    message_count: int | None = None

    # Allow extra fields for other metadata
    class Config:
        extra = "allow"


class InboundMessage(BaseModel):
    sender_id: str
    username: str
    chat_id: str
    content: str
    media: list[str] = Field(default_factory=list)
    channel: str  # 'discord', 'cli', 'cron'
    metadata: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class OutboundMessage(BaseModel):
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = Field(default_factory=list)
    channel: str
    metadata: MessageMetadata = Field(default_factory=MessageMetadata)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
