import json
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

class InboundMessage(BaseModel):
    sender_id: str
    username: str
    chat_id: str
    content: str
    media: List[str] = Field(default_factory=list)
    channel: str  # 'discord', 'cli', 'cron'
    metadata: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class OutboundMessage(BaseModel):
    chat_id: str
    content: str
    reply_to: Optional[str] = None
    media: List[str] = Field(default_factory=list)
    channel: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
