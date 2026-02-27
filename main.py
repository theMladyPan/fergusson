import asyncio

from dataclasses import asdict
import logfire

from src.agent.core import AgentManager
from src.agent.memory import add_message, get_history, check_and_compact
from src.agent.archiver import Archiver
from src.broker.bus import MessageBus
from src.broker.schemas import InboundMessage, OutboundMessage, MessageMetadata, TokenUsage
from src.channels.discord import DiscordChannel
from src.config import app_config, settings
from src.db.session import async_session, init_db


async def agent_loop(bus: MessageBus, manager: AgentManager, archiver: Archiver):
    """The main agent loop that processes inbound messages using Pydantic-AI."""
    logfire.info("Fergusson Agent started. Listening for inbound messages...")

    while True:
        try:
            msg: InboundMessage = await bus.get_next_inbound()
            with logfire.span(f"Processing message from {msg.channel}/{msg.username}: {msg.content[:50]}...") as span:
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
                        await add_message(session, msg.chat_id, msg.channel, "assistant", result.output)

                        # 5. Publish outbound message
                        usage = result.usage()

                        token_usage = TokenUsage(
                            input=usage.input_tokens,
                            output=usage.output_tokens,
                            cache=usage.cache_read_tokens,
                        )
                        metadata = MessageMetadata(token_usage=token_usage, message_count=len(history) + 2)
                        # Add original metadata if present (like message_id)
                        if msg.metadata:
                            for k, v in msg.metadata.items():
                                if not hasattr(metadata, k):
                                    setattr(metadata, k, v)

                        reply = OutboundMessage(
                            chat_id=msg.chat_id,
                            content=result.output,
                            channel=msg.channel,
                            reply_to=msg.metadata.get("message_id") if msg.metadata else None,
                            metadata=metadata,
                        )
                        await bus.publish_outbound(reply)

                        # 6. Trigger background history compaction
                        async def background_compaction(chat_id: str):
                            try:
                                async with async_session() as comp_session:
                                    await check_and_compact(comp_session, chat_id, archiver)
                            except Exception as e:
                                logfire.error(f"Compaction error for {chat_id}: {e}")

                        asyncio.create_task(background_compaction(msg.chat_id))

                        span.set_attributes(
                            {
                                "usage": asdict(usage),
                                "channel": msg.channel,
                                "reply_to": msg.metadata.get("message_id") if msg.metadata else None,
                                "chat_id": msg.chat_id,
                            }
                        )

                    except Exception as e:
                        logfire.error(f"Agent execution error: {e}")
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
            logfire.error(f"Agent loop error: {e}")
            await asyncio.sleep(1)


async def routine_loop(bus: MessageBus, interval: int = 3600):
    """
    Periodically reads workspace/ROUTINE.md and injects it as a system message.
    """
    from pathlib import Path
    from datetime import datetime

    logfire.info(f"Routine loop started with interval {interval}s")
    
    # Wait a bit for the system to fully initialize
    await asyncio.sleep(10)

    while True:
        try:
            routine_path = Path("ROUTINE.md")
            if not routine_path.exists():
                logfire.warning("ROUTINE.md not found, skipping routine check.")
            else:
                content = routine_path.read_text()
                
                # The agent will parse this.
                # We must be clear this is a system instruction to check routines.
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                prompt = f"SYSTEM ALERT: It is now {current_time}.\n\nReview the following ROUTINE.md and execute any tasks that are due now:\n\n{content}"

                msg = InboundMessage(
                    sender_id="system_cron",
                    username="System Cron",
                    chat_id="cron_chat",
                    content=prompt,
                    channel="cron",
                )
                
                logfire.info("Triggering routine execution.")
                await bus.publish_inbound(msg)
            
            # Wait for next interval
            await asyncio.sleep(interval)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logfire.error(f"Routine loop error: {e}")
            await asyncio.sleep(60)


async def main():
    # Setup logging
    logfire.configure(
        token=settings.logfire_token,
        send_to_logfire="if-token-present",
        distributed_tracing=False,
        environment=settings.environment,
        service_name=settings.project,
        scrubbing=False if settings.debug else None,
    )

    with logfire.span("Starting Ferguson Agent") as span:
        # change working directory to the workspace folder
        # this way all file paths are relative to the workspace
        import os

        os.chdir(settings.workspace_folder)
        logfire.info(f"Changed working directory to {settings.workspace_folder}")

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
            logfire.info("Discord channel enabled and started.")

        # Initialize Archiver
        archiver = Archiver(model=manager.smart_model)

    # Start the real agent loop
    agent_task = asyncio.create_task(agent_loop(bus, manager, archiver))
    
    # Start the routine loop
    routine_task = asyncio.create_task(routine_loop(bus))

    logfire.notice("System fully operational. Press Ctrl+C to stop.")

    try:
        # Keep the main loop running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logfire.info("Interrupted by user.")
    finally:
        logfire.info("Shutting down...")
        for channel in active_channels:
            await channel.stop()
        agent_task.cancel()
        routine_task.cancel()
        await asyncio.gather(agent_task, routine_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
