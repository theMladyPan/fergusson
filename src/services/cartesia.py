"""Cartesia Text-to-Speech.

Replaces the ElevenLabs TTS path. Uses the `cartesia` Python SDK's async client
(`AsyncCartesia`) so it integrates cleanly with the async agent loop in
`runners.py` without blocking the event loop.

Design:
- One `AsyncCartesia` client is created per call inside an `async with` block.
  The SDK manages its own HTTP backend; we keep the call short-lived so a
  stale connection never outlives a turn.
- Output is mp3 (container=mp3, bit_rate, sample_rate) so it drops in as a
  Discord media attachment exactly like the previous ElevenLabs mp3 output.
- If `CARTESIA_API_KEY` / `CARTESIA_VOICE_ID` are unset, the function returns
  `None` (graceful no-op) instead of raising — matches the old contract in
  `runners.py`, which simply skips the audio attachment when TTS is unavailable.
"""

from datetime import datetime

import logfire

from src.config import settings


async def text_to_speech(text: str, language: str | None = None) -> str | None:
    """Synthesize `text` to an mp3 file via Cartesia and return its path.

    Called from `runners.py:agent_loop` after the agent finishes, but only when
    the inbound turn was a voice request (voice-for-voice). Returns the local
    mp3 path under `workspace/media/`, or `None` if Cartesia is not configured or
    the request fails.

    Args:
        text: The text to synthesize.
        language: Optional two-letter ISO 639-1 language code (e.g. "sk", "en")
            forwarded to Cartesia so the voice matches the reply language. When
            `None`, falls back to `settings.cartesia.language` (also possibly
            `None`, in which case Cartesia auto-detects).
    """
    if not settings.cartesia.is_configured:
        if settings.cartesia.api_key or settings.cartesia.voice_id:
            logfire.warning("Cartesia TTS partially configured (need CARTESIA_API_KEY + CARTESIA_VOICE_ID).")
        return None

    # Imported lazily so the module imports cleanly in tests without the SDK.
    from cartesia import AsyncCartesia

    # Resolve language: explicit arg > configured default > omit (auto-detect).
    language = language or settings.cartesia.language
    generate_kwargs: dict = {
        "model_id": settings.cartesia.model_id,
        "transcript": text,
        "voice": {"mode": "id", "id": settings.cartesia.voice_id},
        "output_format": {
            "container": "mp3",
            "bit_rate": settings.cartesia.bit_rate,
            "sample_rate": settings.cartesia.sample_rate,
        },
    }
    if language:
        generate_kwargs["language"] = language

    try:
        async with AsyncCartesia(api_key=settings.cartesia.api_key, timeout=settings.cartesia.timeout) as client:
            response = await client.tts.generate(**generate_kwargs)

        media_dir = settings.workspace_folder / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = media_dir / f"tts_response_{timestamp}.mp3"
        await response.write_to_file(str(file_path))
        return str(file_path)
    except Exception as exc:  # noqa: BLE001 — surface as a soft failure, log only
        logfire.error(f"Chyba pri Cartesia TTS generácii zvuku: {exc}")
        return None
