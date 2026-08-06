"""Google Cloud Speech-to-Text (Chirp 3) voice transcription.

Replaces the ElevenLabs STT path. Uses `google-cloud-speech` v2 with the
`chirp_3` model and online (synchronous) `recognize` over inline audio bytes, so
no GCS bucket is required and short Discord voice clips (<1 min) are handled
inline. Authentication uses a service-account key loaded explicitly from
`STT_CREDENTIALS_FILE`; the key is never published as process-wide ADC.

Design:
- `SpeechAsyncClient` keeps the recognize call off the event loop in `runners`.
- `language_codes=["auto"]` (default) lets Chirp 3 detect the dominant language,
  which fits the multilingual Slovak/English user without per-message config.
- `auto_decoding_config` lets the API infer encoding/format from the bytes.
- If `STT_PROJECT_ID` or `STT_CREDENTIALS_FILE` is unset, or either input file
  is missing/unreadable, the function returns `None` (graceful no-op) — matches
  the old contract in `runners.py`,
  which just skips transcription and forwards the raw message.
"""

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
        logfire.warning("Chirp 3 STT not configured (need STT_PROJECT_ID and STT_CREDENTIALS_FILE).")
        return None

    path = Path(audio_path)
    if not path.exists():
        logfire.error(f"Súbor pre STT nenájdený: {audio_path}")
        return None

    # Imported lazily so the module imports cleanly in tests without the SDK.
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechAsyncClient
    from google.cloud.speech_v2.types import cloud_speech
    from google.oauth2 import service_account

    credentials_path = settings.stt.credentials_file.expanduser()

    try:
        credentials = service_account.Credentials.from_service_account_file(str(credentials_path))
        client = SpeechAsyncClient(
            credentials=credentials,
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
