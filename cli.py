import asyncio
import sys

from loguru import logger

from src.broker.bus import MessageBus
from src.broker.schemas import InboundMessage, OutboundMessage


class CLIApp:
    def __init__(self, user_id: str = "cli_user", username: str = "CLI User"):
        self.bus = MessageBus()
        self.user_id = user_id
        self.username = username
        self.chat_id = "cli_chat"
        self.channel_name = "cli"
        self._running = False
        self._outbound_task: asyncio.Task | None = None
        self._input_task: asyncio.Task | None = None

    async def start(self):
        self._running = True
        self._outbound_task = asyncio.create_task(self._listen_for_replies())
        self._input_task = asyncio.create_task(self._input_loop())

        try:
            # Wait for either task to finish/cancel
            await asyncio.gather(self._outbound_task, self._input_task)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        if self._outbound_task:
            self._outbound_task.cancel()
        if self._input_task:
            self._input_task.cancel()

    async def _listen_for_replies(self):
        pubsub = await self.bus.subscribe_outbound(self.channel_name)
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    msg = OutboundMessage.model_validate_json(message["data"])
                    # Ensure prompt resets properly
                    sys.stdout.write(f"\033[KAgent: {msg.content}\nYou: ")
                    sys.stdout.flush()
                except Exception as e:
                    logger.error(f"Failed to process outbound message: {e}")

    async def _input_loop(self):
        loop = asyncio.get_event_loop()
        # Give a slight delay so the start logs appear before the prompt
        await asyncio.sleep(0.1)
        sys.stdout.write("You: ")
        sys.stdout.flush()

        while self._running:
            try:
                # Run the blocking input call in an executor
                content = await loop.run_in_executor(None, sys.stdin.readline)
                content = content.strip()

                if not content:
                    sys.stdout.write("You: ")
                    sys.stdout.flush()
                    continue

                if content.lower() in ["/quit", "/exit"]:
                    logger.info("Exiting CLI...")
                    self._running = False
                    break

                msg = InboundMessage(
                    sender_id=self.user_id,
                    username=self.username,
                    chat_id=self.chat_id,
                    content=content,
                    channel=self.channel_name,
                    metadata={},
                )
                await self.bus.publish_inbound(msg)

            except Exception as e:
                logger.error(f"CLI input error: {e}")
                break


if __name__ == "__main__":
    # Suppress verbose loguru output in CLI mode
    logger.remove()
    logger.add(sys.stderr, format="{message}", level="WARNING")

    app = CLIApp()
    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting...")
