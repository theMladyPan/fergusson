import asyncio
import sys
from loguru import logger
from src.broker.bus import MessageBus
from src.broker.schemas import OutboundMessage, InboundMessage
from src.channels.discord import DiscordChannel
from src.config import app_config
from src.db.session import init_db, async_session
from src.agent.core import AgentManager
from src.agent.memory import add_message, get_history


async def agent_loop(bus: MessageBus, manager: AgentManager):
    """The main agent loop that processes inbound messages using Pydantic-AI."""
    logger.info("Fergusson Agent started. Listening for inbound messages...")

    while True:
        try:
            msg: InboundMessage = await bus.get_next_inbound()
            logger.info(f"Processing message from {msg.channel}/{msg.username}: {msg.content[:50]}...")

            async with async_session() as session:
                # 1. Retrieve history
                history = await get_history(session, msg.chat_id)

                # 2. Add current user message to DB
                await add_message(session, msg.chat_id, msg.channel, "user", msg.content)

                # 3. Run Agent
                try:
                    # We pass the history to the agent
                    result = await manager.run(msg.content, history=history)

                    # 4. Add assistant response to DB
                    await add_message(session, msg.chat_id, msg.channel, "assistant", result)

                    # 5. Publish outbound message
                    reply = OutboundMessage(
                        chat_id=msg.chat_id,
                        content=result,
                        channel=msg.channel,
                        reply_to=msg.metadata.get("message_id"),
                    )
                    await bus.publish_outbound(reply)

                except Exception as e:
                    logger.error(f"Agent execution error: {e}")
                    error_reply = OutboundMessage(
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}",
                        channel=msg.channel,
                        reply_to=msg.metadata.get("message_id"),
                    )
                    await bus.publish_outbound(error_reply)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Agent loop error: {e}")
            await asyncio.sleep(1)


async def main():
    # Setup logging
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # Initialize DB
    await init_db()

    bus = MessageBus()
    manager = AgentManager(bus)

    # Initialize and start active channels
    active_channels = []

    if "discord" in app_config.channels and app_config.channels["discord"].enabled:
        discord_channel = DiscordChannel(bus)
        active_channels.append(discord_channel)
        await discord_channel.start()
        logger.info("Discord channel enabled and started.")

    # Start the real agent loop
    agent_task = asyncio.create_task(agent_loop(bus, manager))

    logger.info("System fully operational. Press Ctrl+C to stop.")

    try:
        # Keep the main loop running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        logger.info("Shutting down...")
        for channel in active_channels:
            await channel.stop()
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
