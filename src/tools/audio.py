"""Audio transcription tool for the core agent.

Wraps the Chirp 3 STT service (`src.services.chirp3.speech_to_text`) so the
agent can transcribe an audio file on demand, instead of shelling out to
whisper/ffmpeg/pip via bash. Used as a fallback when the pre-pipeline STT in
`runners.py` did not run or failed, or when the user explicitly asks to
transcribe a clip.

The tool fails fast via `ModelRetry` when STT is unconfigured or the request
errors, so the agent surfaces the problem to the user instead of improvising.
"""

from pathlib import Path

from pydantic_ai import ModelRetry

from src.services.chirp3 import speech_to_text


async def transcribe_audio(path: str) -> str:
    """Transcribe an audio file to text using Google Chirp 3 speech-to-text.

    Use this when you receive a voice message / audio clip path and need its
    text content. Do NOT try to transcribe audio via bash (whisper, ffmpeg,
    pip, etc.) — always use this tool. Supports the formats Chirp 3 accepts
    (mp3, ogg/opus, wav, m4a, flac, ...).

    Args:
        path: Absolute or workspace-relative path to the audio file.

    Returns:
        The transcribed text.

    Raises:
        ModelRetry: If the file is missing, STT is not configured, or the
            transcription request fails.
    """
    if not Path(path).exists():
        raise ModelRetry(f"Audio file not found: {path}")

    transcript = await speech_to_text(path)
    if not transcript:
        raise ModelRetry(
            "Audio transcription unavailable: Chirp 3 STT is not configured or the request failed. "
            "Tell the user you could not transcribe the voice message."
        )
    return transcript
