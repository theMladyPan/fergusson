from datetime import datetime
import json
from pathlib import Path
import httpx
import logfire
from src.config import settings


async def speech_to_text(audio_path: str) -> str | None:
    """
    Konvertuje prijatú hlasovú správu používateľa na text pomocou ElevenLabs STT (scribe_v1).
    Volá sa v `runners.py:agent_loop` PRED tým, ako správa vôjde do jadra agenta.
    Odpoveď sa vkladá do msg.content.
    """
    if not settings.elevenlabs.api_key:
        logfire.warning("ElevenLabs API klúč nie je konfigurovaný. STT preskočené.")
        return None

    path = Path(audio_path)
    if not path.exists():
        logfire.error(f"Súbor pre STT nenájdený: {audio_path}")
        return None

    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": settings.elevenlabs.api_key}

    try:
        with open(audio_path, "rb") as f:
            files = {"file": (path.name, f, "audio/mpeg")}
            data = {"model_id": settings.elevenlabs.model_id_stt}

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, files=files, data=data, headers=headers)
                response.raise_for_status()
                return response.json().get("text")
    except Exception as e:
        logfire.error(f"Chyba pri ElevenLabs STT pre {audio_path}: {e}")
        return None


async def text_to_speech(text: str) -> str | None:
    """
    Konvertuje vygenerovanú odpoveď agenta späť na hlas uložiteľný ako súbor.
    Volá sa v `runners.py:agent_loop` PO TOM, ako agent dobehne, ale len ak bola podaná otázka hlasom.
    Vracia cestu na disk k lokálnemu mp3 súboru po stiahnutí.
    """
    if not settings.elevenlabs.api_key:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs.voice_id}"
    headers = {"xi-api-key": settings.elevenlabs.api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": settings.elevenlabs.model_id_tts,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()

            # Vytvorenie a uloženie temp súboru do 'workspace/media/'
            media_dir = settings.workspace_folder / "media"
            media_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = media_dir / f"tts_response_{timestamp}.mp3"

            file_path.write_bytes(response.content)
            return str(file_path)

    except Exception as e:
        logfire.error(f"Chyba pri ElevenLabs TTS generácii zvuku: {e}")
        return None
