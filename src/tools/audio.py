"""Native speech tools for the core agent.

Wraps Chirp 3 STT and Cartesia TTS so the agent can transcribe and synthesize
speech without shelling out to bash or ad-hoc Python commands. Both tools fail
fast via `ModelRetry` when their service is unavailable, preventing the agent
from improvising an unsupported local audio workflow.
"""

from pathlib import Path

from pydantic_ai import ModelRetry

from src.services.cartesia import text_to_speech
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


async def synthesize_speech(text: str, language: str | None = None) -> str:
    """Synthesize text to an MP3 file using Cartesia text-to-speech.

    Use this tool whenever the user requests generated speech or an audio reply.
    Do not invoke Cartesia through bash or write an ad-hoc Python script. To send
    the generated file to a channel, pass the returned path to
    `send_message_to_channel` through its `media_paths` argument.

    Args:
        text: Text to convert to speech.
        language: Optional two-letter ISO 639-1 language code, such as `sk` or
            `en`. Omit it to use configured language detection.

    Returns:
        Path to the generated MP3 file.

    Raises:
        ModelRetry: If text is empty, TTS is unconfigured, or synthesis fails.
    """
    if not text.strip():
        raise ModelRetry("Speech synthesis unavailable: text is empty.")

    generated_audio_path = await text_to_speech(text, language=language)
    if not generated_audio_path:
        raise ModelRetry(
            "Speech synthesis unavailable: Cartesia TTS is not configured or the request failed. "
            "Tell the user you could not generate the audio; do not use bash or Python as a fallback."
        )
    return generated_audio_path
