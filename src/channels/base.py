import asyncio
import json
from abc import ABC, abstractmethod
from src.broker.bus import MessageBus
from src.broker.schemas import OutboundMessage

class BaseChannel(ABC):
    name: str

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self._running = False
        self._outbound_task: asyncio.Task | None = None
        self._ingress_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the channel ingress and egress loops."""
        self._running = True
        self._outbound_task = asyncio.create_task(self._outbound_loop())
        self._ingress_task = asyncio.create_task(self._start_ingress())

    async def stop(self) -> None:
        """Stop the channel."""
        self._running = False
        if getattr(self, '_outbound_task', None):
            self._outbound_task.cancel()
        if getattr(self, '_ingress_task', None):
            self._ingress_task.cancel()
        await self._stop_ingress()

    async def _outbound_loop(self) -> None:
        """Listen to the message bus for outbound messages for this channel."""
        pubsub = await self.bus.subscribe_outbound(self.name)
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    msg = OutboundMessage.model_validate_json(message["data"])
                    await self.send(msg)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Failed to process outbound message: {e}")

    @abstractmethod
    async def _start_ingress(self) -> None:
        """Start listening for incoming messages."""
        pass

    @abstractmethod
    async def _stop_ingress(self) -> None:
        """Stop listening for incoming messages."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through the channel's API."""
        pass