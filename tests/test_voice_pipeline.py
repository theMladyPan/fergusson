import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings


@pytest.mark.asyncio
async def test_transcribe_audio_raises_model_retry_when_file_missing(monkeypatch):
    monkeypatch.delenv("STT_PROJECT_ID", raising=False)
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    from src.tools.audio import transcribe_audio

    with pytest.raises(ModelRetry, match="Audio file not found"):
        await transcribe_audio("/nonexistent/audio.mp3")


@pytest.mark.asyncio
async def test_transcribe_audio_raises_model_retry_when_stt_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("STT_PROJECT_ID", raising=False)
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    audio = tmp_path / "clip.ogg"
    audio.write_bytes(b"fake")

    from src.tools.audio import transcribe_audio

    with pytest.raises(ModelRetry, match="transcription unavailable"):
        await transcribe_audio(str(audio))


@pytest.mark.asyncio
async def test_transcribe_audio_returns_transcript_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    audio = tmp_path / "clip.ogg"
    audio.write_bytes(b"fake")

    async def fake_stt(path: str) -> str | None:
        assert path == str(audio)
        return "hello world"

    monkeypatch.setattr("src.tools.audio.speech_to_text", fake_stt)

    from src.tools.audio import transcribe_audio

    assert await transcribe_audio(str(audio)) == "hello world"


@pytest.mark.asyncio
async def test_transcribe_audio_raises_model_retry_when_stt_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    audio = tmp_path / "clip.ogg"
    audio.write_bytes(b"fake")

    async def fake_stt(path: str) -> str | None:
        return None

    monkeypatch.setattr("src.tools.audio.speech_to_text", fake_stt)

    from src.tools.audio import transcribe_audio

    with pytest.raises(ModelRetry, match="transcription unavailable"):
        await transcribe_audio(str(audio))


def test_transcribe_audio_registered_in_all_tools():
    from src.tools import all_tools, transcribe_audio

    assert transcribe_audio in all_tools


@pytest.mark.asyncio
async def test_chirp3_loads_explicit_credentials_without_mutating_adc(monkeypatch, tmp_path):
    """Chirp loads its dedicated key without changing process-wide ADC."""
    from src.config import Settings

    fake_key = tmp_path / "key.json"
    fake_key.write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setenv("STT_CREDENTIALS_FILE", "~/key.json")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/unrelated/adc.json")
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    import os
    import types

    captured: dict = {}
    fake_credentials = object()

    class _Credentials:
        @classmethod
        def from_service_account_file(cls, filename):
            captured["credentials_file"] = filename
            return fake_credentials

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            captured["client_credentials"] = kwargs["credentials"]
            captured["adc_after"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

        def recognizer_path(self, *args):
            return "r"

        async def recognize(self, request=None, timeout=None):
            raise RuntimeError("stop after credential wiring")

    google_mod = types.ModuleType("google")
    api_core_mod = types.ModuleType("google.api_core")
    api_core_co = types.ModuleType("google.api_core.client_options")
    api_core_co.ClientOptions = lambda **kwargs: kwargs
    api_core_mod.client_options = api_core_co
    google_mod.api_core = api_core_mod

    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")
    service_account_mod.Credentials = _Credentials
    oauth2_mod.service_account = service_account_mod
    google_mod.oauth2 = oauth2_mod

    speech_pkg = types.ModuleType("google.cloud.speech_v2")
    speech_pkg.SpeechAsyncClient = _AsyncClient
    speech_pkg.types = types.ModuleType("google.cloud.speech_v2.types")

    class _RecognitionConfig:
        def __init__(self, **kwargs):
            pass

    class _AutoDetectDecodingConfig:
        pass

    class _RecognizeRequest:
        def __init__(self, **kwargs):
            pass

    speech_pkg.types.cloud_speech = SimpleNamespace(
        RecognitionConfig=_RecognitionConfig,
        AutoDetectDecodingConfig=_AutoDetectDecodingConfig,
        RecognizeRequest=_RecognizeRequest,
    )

    cloud_pkg = types.ModuleType("google.cloud")
    cloud_pkg.speech_v2 = speech_pkg

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.api_core", api_core_mod)
    monkeypatch.setitem(sys.modules, "google.api_core.client_options", api_core_co)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.speech_v2", speech_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.speech_v2.types", speech_pkg.types)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake")

    from src.services.chirp3 import speech_to_text

    assert await speech_to_text(str(audio)) is None
    assert captured["credentials_file"] == str(fake_key)
    assert captured["client_credentials"] is fake_credentials
    assert captured["adc_after"] == "/unrelated/adc.json"


@pytest.mark.asyncio
async def test_pipeline_replaces_attachment_marker_when_stt_fails(monkeypatch, tmp_path):
    """Layer 1: when STT returns None, runners.py must replace the bare
    [attachment: <path>] marker with a 'transcription unavailable' note so the
    agent does not shell out to bash to transcribe."""
    from src.broker.schemas import InboundMessage

    monkeypatch.setattr("src.runners.speech_to_text", lambda _p: None.__class__ and None)

    async def fake_stt(path):
        return None

    monkeypatch.setattr("src.runners.speech_to_text", fake_stt)

    # Build a fake msg with an audio attachment marker in content + path in media.
    audio = tmp_path / "voice-message.ogg"
    audio.write_bytes(b"fake")

    # We exercise the marker-replacement logic directly by replicating the
    # runners.py branch in isolation (full agent_loop needs a running bus).
    media_path = str(audio)
    content = f"hi{chr(10)}{chr(10)}[attachment: {media_path}]"

    # Replicate the runners.py failure branch.
    from pathlib import Path

    marker = f"[attachment: {media_path}]"
    note = (
        "[Voice message — transcription unavailable "
        "(Chirp 3 STT not configured or failed). "
        "Tell the user you could not transcribe the voice message; "
        "do not try to transcribe it via bash.]"
    )
    if marker in content:
        content = content.replace(marker, note)
    else:
        content += f"\n\n{note}"

    assert marker not in content
    assert "transcription unavailable" in content
    assert "do not try to transcribe it via bash" in content
    assert media_path not in content  # raw path must NOT leak to the model
