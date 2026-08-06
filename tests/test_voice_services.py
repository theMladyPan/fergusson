import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings


# --- Config ----------------------------------------------------------------


def test_cartesia_config_defaults(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_VOICE_ID", raising=False)
    s = Settings(_env_file=None)
    assert s.cartesia.model_id == "sonic-3.5"
    assert s.cartesia.sample_rate == 24000
    assert s.cartesia.bit_rate == 128000
    assert s.cartesia.timeout == 30
    assert s.cartesia.is_configured is False


def test_cartesia_config_env_override(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "ck")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "v-1")
    monkeypatch.setenv("CARTESIA_MODEL_ID", "sonic-latest")
    monkeypatch.setenv("CARTESIA_SAMPLE_RATE", "44100")
    s = Settings(_env_file=None)
    assert s.cartesia.api_key == "ck"
    assert s.cartesia.voice_id == "v-1"
    assert s.cartesia.model_id == "sonic-latest"
    assert s.cartesia.sample_rate == 44100
    assert s.cartesia.is_configured is True


def test_stt_config_defaults(monkeypatch):
    monkeypatch.delenv("STT_PROJECT_ID", raising=False)
    monkeypatch.delenv("STT_CREDENTIALS_FILE", raising=False)
    s = Settings(_env_file=None)
    assert s.stt.credentials_file is None
    assert s.stt.location == "us"
    assert s.stt.language_codes == "auto"
    assert s.stt.model == "chirp_3"
    assert s.stt.timeout == 60
    assert s.stt.is_configured is False


def test_stt_config_env_override(monkeypatch):
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setenv("STT_CREDENTIALS_FILE", "~/chirp-key.json")
    monkeypatch.setenv("STT_LOCATION", "eu")
    monkeypatch.setenv("STT_LANGUAGE_CODES", "auto,sk-SK")
    s = Settings(_env_file=None)
    assert s.stt.project_id == "proj-x"
    assert s.stt.credentials_file == Path("~/chirp-key.json")
    assert s.stt.location == "eu"
    assert s.stt.language_codes == "auto,sk-SK"
    assert s.stt.is_configured is True


def test_stt_config_requires_credentials_file(monkeypatch):
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.delenv("STT_CREDENTIALS_FILE", raising=False)

    assert Settings(_env_file=None).stt.is_configured is False


def test_elevenlabs_config_removed():
    """ElevenLabsConfig and settings.elevenlabs must no longer exist."""
    s = Settings(_env_file=None)
    assert not hasattr(s, "elevenlabs")
    import src.config as cfg

    assert not hasattr(cfg, "ElevenLabsConfig")


# --- Cartesia TTS ----------------------------------------------------------


@pytest.mark.asyncio
async def test_tts_returns_none_when_not_configured(monkeypatch):
    from src.config import Settings

    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_VOICE_ID", raising=False)
    monkeypatch.setattr("src.services.cartesia.settings", Settings(_env_file=None))

    from src.services.cartesia import text_to_speech

    assert await text_to_speech("hello") is None


@pytest.mark.asyncio
async def test_tts_writes_mp3_on_happy_path(monkeypatch, tmp_path):
    from src.config import Settings

    monkeypatch.setenv("CARTESIA_API_KEY", "ck")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "v-1")
    settings = Settings(_env_file=None)
    settings = settings.model_copy(update={"workspace_folder": tmp_path})
    monkeypatch.setattr("src.services.cartesia.settings", settings)

    captured: dict = {}

    class _Response:
        async def write_to_file(self, path):
            captured["path"] = path
            Path(path).write_bytes(b"ID3mp3data")

    class _TTS:
        async def generate(self, **kwargs):
            captured["kwargs"] = kwargs
            return _Response()

    class _Client:
        def __init__(self, *a, **kw):
            captured["init_kwargs"] = kw

        async def __aenter__(self):
            self.tts = _TTS()
            return self

        async def __aexit__(self, *exc):
            return False

    fake_cartesia = SimpleNamespace(AsyncCartesia=_Client)
    monkeypatch.setitem(sys.modules, "cartesia", fake_cartesia)

    from src.services.cartesia import text_to_speech

    result = await text_to_speech("Ahoj svet")

    assert result is not None
    assert result.endswith(".mp3")
    assert Path(result).exists()
    assert captured["kwargs"]["model_id"] == "sonic-3.5"
    assert captured["kwargs"]["transcript"] == "Ahoj svet"
    assert captured["kwargs"]["voice"] == {"mode": "id", "id": "v-1"}
    assert captured["kwargs"]["output_format"]["container"] == "mp3"
    assert captured["kwargs"]["output_format"]["bit_rate"] == 128000
    # No language passed -> Cartesia auto-detects.
    assert "language" not in captured["kwargs"]

    # ensure media dir was created under the workspace
    assert Path(result).parent == (tmp_path / "media")


@pytest.mark.asyncio
async def test_tts_returns_none_on_sdk_error(monkeypatch, tmp_path):
    from src.config import Settings

    monkeypatch.setenv("CARTESIA_API_KEY", "ck")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "v-1")
    settings = Settings(_env_file=None)
    settings = settings.model_copy(update={"workspace_folder": tmp_path})
    monkeypatch.setattr("src.services.cartesia.settings", settings)

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "cartesia", SimpleNamespace(AsyncCartesia=_Client))

    from src.services.cartesia import text_to_speech

    assert await text_to_speech("x") is None


def test_cartesia_config_language_default_none(monkeypatch):
    monkeypatch.delenv("CARTESIA_LANGUAGE", raising=False)
    s = Settings(_env_file=None)
    assert s.cartesia.language is None


def test_cartesia_config_language_env_override(monkeypatch):
    monkeypatch.setenv("CARTESIA_LANGUAGE", "sk")
    s = Settings(_env_file=None)
    assert s.cartesia.language == "sk"


@pytest.mark.asyncio
async def test_tts_forwards_explicit_language_to_cartesia(monkeypatch, tmp_path):
    from src.config import Settings

    monkeypatch.setenv("CARTESIA_API_KEY", "ck")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "v-1")
    settings = Settings(_env_file=None)
    settings = settings.model_copy(update={"workspace_folder": tmp_path})
    monkeypatch.setattr("src.services.cartesia.settings", settings)

    captured: dict = {}

    class _Response:
        async def write_to_file(self, path):
            Path(path).write_bytes(b"ID3mp3data")

    class _TTS:
        async def generate(self, **kwargs):
            captured["kwargs"] = kwargs
            return _Response()

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            self.tts = _TTS()
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "cartesia", SimpleNamespace(AsyncCartesia=_Client))

    from src.services.cartesia import text_to_speech

    result = await text_to_speech("Ahoj, som tu.", language="sk")
    assert result is not None
    assert captured["kwargs"]["language"] == "sk"


@pytest.mark.asyncio
async def test_tts_uses_configured_default_language_when_arg_missing(monkeypatch, tmp_path):
    from src.config import Settings

    monkeypatch.setenv("CARTESIA_API_KEY", "ck")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "v-1")
    monkeypatch.setenv("CARTESIA_LANGUAGE", "sk")
    settings = Settings(_env_file=None)
    settings = settings.model_copy(update={"workspace_folder": tmp_path})
    monkeypatch.setattr("src.services.cartesia.settings", settings)

    captured: dict = {}

    class _Response:
        async def write_to_file(self, path):
            Path(path).write_bytes(b"ID3mp3data")

    class _TTS:
        async def generate(self, **kwargs):
            captured["kwargs"] = kwargs
            return _Response()

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            self.tts = _TTS()
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "cartesia", SimpleNamespace(AsyncCartesia=_Client))

    from src.services.cartesia import text_to_speech

    result = await text_to_speech("Ahoj")
    assert result is not None
    assert captured["kwargs"]["language"] == "sk"


def test_dubbing_result_model():
    from src.agent.voice import DubbingResult

    r = DubbingResult(spoken_text="Ahoj, som tu.", language="sk")
    assert r.spoken_text == "Ahoj, som tu."
    assert r.language == "sk"


def test_dubbing_agent_returns_structured_result():
    from src.agent.voice import get_dubbing_agent, DubbingResult
    from pydantic_ai import Agent

    agent = get_dubbing_agent(model="google-gla:gemini-3.5-flash-lite")
    assert isinstance(agent, Agent)
    assert agent._output_type is DubbingResult


# --- Chirp 3 STT -----------------------------------------------------------


@pytest.mark.asyncio
async def test_stt_returns_none_when_not_configured(monkeypatch):
    from src.config import Settings

    monkeypatch.delenv("STT_PROJECT_ID", raising=False)
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    from src.services.chirp3 import speech_to_text

    assert await speech_to_text("/nonexistent/audio.mp3") is None


@pytest.mark.asyncio
async def test_stt_returns_none_when_file_missing(monkeypatch, tmp_path):
    from src.config import Settings

    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setenv("STT_CREDENTIALS_FILE", str(tmp_path / "chirp-key.json"))
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    from src.services.chirp3 import speech_to_text

    assert await speech_to_text(str(tmp_path / "nope.mp3")) is None


@pytest.mark.asyncio
async def test_stt_transcribes_inline_audio(monkeypatch, tmp_path):
    from src.config import Settings

    credentials_file = tmp_path / "chirp-key.json"
    credentials_file.write_text("{}")
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setenv("STT_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fakeaudio")

    captured: dict = {}
    fake_credentials = object()

    class _Alt:
        def __init__(self, transcript):
            self.transcript = transcript

    class _Result:
        def __init__(self, transcript):
            self.alternatives = [_Alt(transcript)]

    class _Response:
        def __init__(self, results):
            self.results = results

    class _AsyncClient:
        def __init__(self, *a, **kw):
            captured["client_kwargs"] = kw

        def recognizer_path(self, project, location, rec):
            return f"projects/{project}/locations/{location}/recognizers/{rec}"

        async def recognize(self, request=None, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            # Echo the language codes + content back via the request object.
            captured["content_len"] = len(request.content)
            captured["language_codes"] = list(request.config.language_codes)
            captured["model"] = request.config.model
            return _Response([_Result("Ahoj svet"), _Result("ako sa máš")])

    fake_speech_v2 = SimpleNamespace(SpeechAsyncClient=_AsyncClient)
    fake_types = SimpleNamespace(cloud_speech=SimpleNamespace())  # placeholder
    fake_client_options = SimpleNamespace(ClientOptions=lambda **kw: kw)

    # cloud_speech types are used to *construct* the request/config objects, so
    # we need real-ish classes. Use SimpleNamespace factories that store args.
    class _RecognitionConfig:
        def __init__(self, auto_decoding_config=None, model=None, language_codes=None):
            self.auto_decoding_config = auto_decoding_config
            self.model = model
            self.language_codes = language_codes

    class _AutoDetectDecodingConfig:
        pass

    class _RecognizeRequest:
        def __init__(self, recognizer=None, config=None, content=None):
            self.recognizer = recognizer
            self.config = config
            self.content = content

    fake_cloud_speech = SimpleNamespace(
        RecognitionConfig=_RecognitionConfig,
        AutoDetectDecodingConfig=_AutoDetectDecodingConfig,
        RecognizeRequest=_RecognizeRequest,
    )

    # Patch the lazy imports done inside speech_to_text.
    import types

    google_mod = types.ModuleType("google")
    api_core_mod = types.ModuleType("google.api_core")
    api_core_co = types.ModuleType("google.api_core.client_options")
    api_core_co.ClientOptions = fake_client_options.ClientOptions
    api_core_mod.client_options = api_core_co
    google_mod.api_core = api_core_mod

    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")

    class _Credentials:
        @classmethod
        def from_service_account_file(cls, filename):
            captured["credentials_file"] = filename
            return fake_credentials

    service_account_mod.Credentials = _Credentials
    oauth2_mod.service_account = service_account_mod
    google_mod.oauth2 = oauth2_mod

    speech_pkg = types.ModuleType("google.cloud.speech_v2")
    speech_pkg.SpeechAsyncClient = _AsyncClient
    speech_pkg.types = types.ModuleType("google.cloud.speech_v2.types")
    speech_pkg.types.cloud_speech = fake_cloud_speech

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

    from src.services.chirp3 import speech_to_text

    transcript = await speech_to_text(str(audio))

    assert transcript == "Ahoj svet ako sa máš"
    assert captured["model"] == "chirp_3"
    assert captured["language_codes"] == ["auto"]
    assert captured["content_len"] == len(b"fakeaudio")
    assert captured["credentials_file"] == str(credentials_file)
    assert captured["client_kwargs"]["credentials"] is fake_credentials
    assert "us-speech.googleapis.com" in captured["client_kwargs"]["client_options"]["api_endpoint"]


@pytest.mark.asyncio
async def test_stt_returns_none_on_sdk_error(monkeypatch, tmp_path):
    from src.config import Settings

    credentials_file = tmp_path / "chirp-key.json"
    credentials_file.write_text("{}")
    monkeypatch.setenv("STT_PROJECT_ID", "proj-x")
    monkeypatch.setenv("STT_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setattr("src.services.chirp3.settings", Settings(_env_file=None))

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fakeaudio")

    class _AsyncClient:
        def __init__(self, *a, **kw):
            pass

        def recognizer_path(self, *a):
            return "r"

        async def recognize(self, request=None, timeout=None):
            raise RuntimeError("google error")

    import types

    google_mod = types.ModuleType("google")
    api_core_mod = types.ModuleType("google.api_core")
    api_core_co = types.ModuleType("google.api_core.client_options")
    api_core_co.ClientOptions = lambda **kw: kw
    api_core_mod.client_options = api_core_co
    google_mod.api_core = api_core_mod

    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")
    service_account_mod.Credentials = SimpleNamespace(from_service_account_file=lambda _filename: object())
    oauth2_mod.service_account = service_account_mod
    google_mod.oauth2 = oauth2_mod

    speech_pkg = types.ModuleType("google.cloud.speech_v2")
    speech_pkg.SpeechAsyncClient = _AsyncClient
    speech_pkg.types = types.ModuleType("google.cloud.speech_v2.types")

    class _RecognitionConfig:
        def __init__(self, **kw):
            pass

    class _AutoDetectDecodingConfig:
        pass

    class _RecognizeRequest:
        def __init__(self, **kw):
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

    from src.services.chirp3 import speech_to_text

    assert await speech_to_text(str(audio)) is None
