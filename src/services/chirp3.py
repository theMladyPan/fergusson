"""Google Cloud Speech-to-Text (Chirp 3) voice transcription.

Replaces the ElevenLabs STT path. Uses `google-cloud-speech` v2 with the
`chirp_3` model and online (synchronous) `recognize` over inline audio bytes, so
no GCS bucket is required and short Discord voice clips (<1 min) are handled
inline. Authentication is Application Default Credentials: on the headless host
point `GOOGLE_APPLICATION_CREDENTIALS` at a service account JSON key.

Design:
- `SpeechAsyncClient` keeps the recognize call off the event loop in `runners`.
- `language_codes=["auto"]` (default) lets Chirp 3 detect the dominant language,
  which fits the multilingual Slovak/English user without per-message config.
- `auto_decoding_config` lets the API infer encoding/format from the bytes.
- If `STT_PROJECT_ID` is unset or the file is missing/unreadable, the function
  returns `None` (graceful no-op) — matches the old contract in `runners.py`,
  which just skips transcription and forwards the raw message.
"""

import os
from pathlib import Path

import logfire

from src.config import settings


async def speech_to_text(audio_path: str) -> str | None:
    """Transcribe the audio file at `audio_path` to text via Chirp 3.

    Called from `runners.py:agent_loop` before the message enters the core agent,
    when a Discord media attachment is an audio clip. The returned text is
    appended to the message content as a transcription. Returns `None` if Chirp 3
    is not configured, the file is missing, or the request fails.
    """
    if not settings.stt.is_configured:
        if settings.stt.project_id is not None:
            logfire.warning("Chirp 3 STT partially configured (need STT_PROJECT_ID).")
        return None

    path = Path(audio_path)
    if not path.exists():
        logfire.error(f"Súbor pre STT nenájdený: {audio_path}")
        return None

    # Imported lazily so the module imports cleanly in tests without the SDK.
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechAsyncClient
    from google.cloud.speech_v2.types import cloud_speech

    # Layer 0: Google auth reads GOOGLE_APPLICATION_CREDENTIALS literally and
    # does NOT expand `~`, so a `~/key.json` path fails with "File not found".
    # Expand it here so users can write `~` in .env. Safe no-op when already
    # absolute or unset.
    creds_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_env:
        expanded = os.path.expanduser(creds_env)
        if expanded != creds_env:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = expanded
            logfire.info(f"Expanded GOOGLE_APPLICATION_CREDENTIALS ~ -> {expanded}")

    try:
        client = SpeechAsyncClient(
            client_options=ClientOptions(api_endpoint=f"{settings.stt.location}-speech.googleapis.com"),
        )
        recognizer = client.recognizer_path(settings.stt.project_id, settings.stt.location, "_")

        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            model=settings.stt.model,
            language_codes=settings.stt.language_codes.split(","),
        )

        audio_content = path.read_bytes()
        request = cloud_speech.RecognizeRequest(
            recognizer=recognizer,
            config=config,
            content=audio_content,
        )

        response = await client.recognize(request=request, timeout=settings.stt.timeout)

        results = response.results
        if not results:
            return None

        # Concatenate all result alternatives' top transcript; Chirp 3 returns
        # one result per utterance/segment for inline recognize.
        return " ".join(result.alternatives[0].transcript for result in results).strip() or None
    except Exception as exc:  # noqa: BLE001 — surface as a soft failure, log only
        logfire.error(f"Chyba pri Chirp 3 STT pre {audio_path}: {exc}")
        return None
