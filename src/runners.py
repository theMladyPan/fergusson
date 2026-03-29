import asyncio
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import logfire

from src.agent.archiver import Archiver
from src.agent.core import AgentManager
from src.agent.memory import (
    add_message,
    check_and_compact,
    get_history,
    get_inbound_history_role,
    get_shared_history_thread_id,
)
from src.broker.bus import MessageBus
from src.broker.schemas import InboundMessage, MessageMetadata, OutboundMessage, TokenUsage
from src.config import settings
from src.db.session import async_session
from src.services.elevenlabs import speech_to_text, text_to_speech
from src.tools.fs import read_file_content


async def agent_loop(bus: MessageBus, manager: AgentManager, archiver: Archiver):
    """The main agent loop that processes inbound messages using Pydantic-AI."""
    logfire.info("Fergusson Agent started. Listening for inbound messages...")

    while True:
        try:
            msg: InboundMessage = await bus.get_next_inbound()
            with logfire.span(f"Processing message from {msg.channel}/{msg.username}: {msg.content[:50]}...") as span:
                async with async_session() as session:
                    history_thread_id = get_shared_history_thread_id()

                    # 1. Retrieve history
                    history = await get_history(session, history_thread_id)

                    # --- PRIDANÁ LOGIKA: STT (Speech-to-Text) ---
                    # Skontroluj či v stiahnutých médiách z Discordu bola prípona odkazujúca na audio.
                    is_voice_request = False
                    audio_extensions = {".mp3", ".ogg", ".wav", ".m4a"}
                    for media_path in msg.media:
                        if Path(media_path).suffix.lower() in audio_extensions:
                            stt_text = await speech_to_text(media_path)
                            if stt_text:
                                msg.content += f"\n\n[Hlasová transkripcia z audia: '{stt_text}']"
                                is_voice_request = True
                            break  # Prepisujeme iba prvú hlasovku z poľa pre zjednodušenie
                    # --------------------------------------------

                    # 2. Add current user message to DB
                    inbound_role = get_inbound_history_role(msg.channel)
                    await add_message(
                        session,
                        history_thread_id,
                        msg.channel,
                        inbound_role,
                        msg.content,
                        metadata={
                            **(msg.metadata or {}),
                            "transport_chat_id": msg.chat_id,
                            "sender_id": msg.sender_id,
                            "username": msg.username,
                        },
                    )

                    # 3. Run Agent
                    try:
                        # We pass the history to the agent
                        result = await manager.run(
                            msg.content,
                            history=history,
                            chat_id=msg.chat_id,
                            channel=msg.channel,
                            sender_id=msg.sender_id,
                        )

                        # 4. Add assistant response to DB
                        await add_message(
                            session,
                            history_thread_id,
                            msg.channel,
                            "assistant",
                            result.output,
                            metadata={
                                "transport_chat_id": msg.chat_id,
                                "reply_to": msg.metadata.get("message_id") if msg.metadata else None,
                            },
                        )

                        # --- PRIDANÁ LOGIKA: TTS (Text-to-Speech) ---
                        # Generujeme hlas iba vtedy, ak sme prijali otázku akoukoľvek hlasovkou
                        # (tzv. Hlas-za-Hlas) kvoli šetreniu limitov STT API.
                        outbound_media = []
                        if is_voice_request:
                            from src.agent.voice import get_dubbing_agent

                            dubbing_agent = get_dubbing_agent(manager.fast_model)
                            with logfire.span("Rewriting response for voice dubbing"):
                                dub_result = await dubbing_agent.run(f"Rewrite this for voice:\n\n{result.output}")
                                spoken_text = dub_result.output

                            generated_audio = await text_to_speech(spoken_text)
                            if generated_audio:
                                outbound_media.append(generated_audio)
                        # --------------------------------------------

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

                        setattr(metadata, "is_voice_request", is_voice_request)

                        reply = OutboundMessage(
                            chat_id=msg.chat_id,
                            content=result.output,
                            channel=msg.channel,
                            reply_to=msg.metadata.get("message_id") if msg.metadata else None,
                            metadata=metadata,
                            media=outbound_media,
                        )
                        await bus.publish_outbound(reply)

                        # 6. Trigger background history compaction
                        async def background_compaction(shared_thread_id: str):
                            try:
                                async with async_session() as comp_session:
                                    await check_and_compact(comp_session, shared_thread_id, archiver)
                            except Exception as e:
                                logfire.error(f"Compaction error for {shared_thread_id}: {e}")

                        asyncio.create_task(background_compaction(history_thread_id))

                        span.set_attributes(
                            {
                                "usage": asdict(usage),
                                "channel": msg.channel,
                                "reply_to": msg.metadata.get("message_id") if msg.metadata else None,
                                "chat_id": msg.chat_id,
                                "history_thread_id": history_thread_id,
                            }
                        )

                    except Exception as e:
                        logfire.error(
                            f"Agent execution error: {e}",
                            _exc_info=True,
                        )
                        error_reply = OutboundMessage(
                            chat_id=msg.chat_id,
                            content=f"Sorry, I encountered an error: {str(e)}",
                            channel=msg.channel,
                            reply_to=msg.metadata.get("message_id") if msg.metadata else None,
                        )
                        await bus.publish_outbound(error_reply)

        except asyncio.CancelledError:
            break

        except Exception as e:
            logfire.error(f"Agent loop error: {e}", _exc_info=True)
            await asyncio.sleep(1)


async def routine_loop(bus: MessageBus, interval: int = 3600):
    """
    Periodically reads workspace/ROUTINE.md and injects it as a system message.
    """
    logfire.info(f"Routine loop started with interval {interval}s")

    # Wait a bit for the system to fully initialize
    await asyncio.sleep(10)

    while True:
        try:
            routine_path = settings.workspace_folder / "ROUTINE.md"
            if not routine_path.exists():
                logfire.warning("ROUTINE.md not found, skipping routine check.")
            else:
                # content = routine_path.read_text()

                # The agent will parse this.
                # We must be clear this is a system instruction to check routines.

                content = await read_file_content(
                    str(routine_path),
                    elevated_privileges=True,
                )

                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prompt = f"""SYSTEM ALERT: It is now {current_time}.
Review the following routine and execute any tasks that are due now.
Content of the workspace/ROUTINE.md file:
```markdown
{content}
```
"""

                chat_id = settings.discord.default_channel_id if settings.discord.default_channel_id else "cron_chat"
                channel = "discord" if settings.discord.default_channel_id else "cron"

                msg = InboundMessage(
                    sender_id="system_cron",
                    username="System Cron",
                    chat_id=chat_id,
                    content=prompt,
                    channel=channel,
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
