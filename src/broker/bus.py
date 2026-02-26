import json
import asyncio
import redis.asyncio as redis
from loguru import logger
from .schemas import InboundMessage, OutboundMessage

class MessageBus:
    INBOUND_QUEUE = "fergusson:inbound"
    OUTBOUND_CHANNEL_PREFIX = "fergusson:outbound:"

    def __init__(self, host='localhost', port=6379):
        self.redis = redis.Redis(host=host, port=port, decode_responses=True)

    async def publish_inbound(self, msg: InboundMessage):
        """Channels call this to push messages to the agent."""
        await self.redis.lpush(self.INBOUND_QUEUE, msg.model_dump_json())
        logger.debug(f"Published inbound from {msg.channel}: {msg.sender_id}")

    async def get_next_inbound(self) -> InboundMessage:
        """Agent calls this to consume messages."""
        _, data = await self.redis.brpop(self.INBOUND_QUEUE)
        return InboundMessage.model_validate_json(data)

    async def publish_outbound(self, msg: OutboundMessage):
        """Agent calls this to push responses back to channels."""
        channel_topic = f"{self.OUTBOUND_CHANNEL_PREFIX}{msg.channel}"
        await self.redis.publish(channel_topic, msg.model_dump_json())
        logger.debug(f"Published outbound to {msg.channel}: {msg.chat_id}")

    async def subscribe_outbound(self, channel_name: str):
        """Channels call this to listen for responses."""
        pubsub = self.redis.pubsub()
        topic = f"{self.OUTBOUND_CHANNEL_PREFIX}{channel_name}"
        await pubsub.subscribe(topic)
        logger.info(f"Subscribed to outbound channel: {topic}")
        return pubsub
