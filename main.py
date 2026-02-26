import asyncio
import sys
from loguru import logger
from src.broker.bus import MessageBus
from src.broker.schemas import OutboundMessage
from src.channels.discord import DiscordChannel


async def agent_mock_loop(bus: MessageBus):
    """A mock agent loop that just echoes messages back."""
    logger.info("Mock agent started. Listening for inbound messages...")
    while True:
        try:
            msg = await bus.get_next_inbound()
            logger.info(f"Agent received from {msg.channel}: {msg.content}")

            # Create a simple echo response
            reply = OutboundMessage(
                chat_id=msg.chat_id,
                content=f"Echo: {msg.content}",
                channel=msg.channel,
                reply_to=msg.metadata.get("message_id"),
            )
            await bus.publish_outbound(reply)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Mock agent error: {e}")


async def main():
    logger.add(sys.stderr, format="{time} {level} {message}", filter="my_module", level="INFO")

    bus = MessageBus()

    # Initialize channels
    discord_channel = DiscordChannel(bus)  # Requires token in config

    # Start channels
    # Uncomment to enable discord (if configured)
    await discord_channel.start()

    # Start the mock agent
    agent_task = asyncio.create_task(agent_mock_loop(bus))

    try:
        # Keep the main loop running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        logger.info("Shutting down...")
        # await discord_channel.stop()
        agent_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
