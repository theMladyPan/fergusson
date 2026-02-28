import asyncio

import logfire

from src.agent.archiver import Archiver
from src.agent.core import AgentManager
from src.broker.bus import MessageBus
from src.channels.discord import DiscordChannel
from src.config import app_config, settings
from src.db.session import init_db
from src.runners import agent_loop, routine_loop


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
    logfire.instrument_pydantic_ai()

    with logfire.span("Starting Ferguson Agent") as span:
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
