import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChannelConfig(BaseModel):
    enabled: bool = False


class MCPServerConfig(BaseModel):
    """MCP server connection configuration (stdio or HTTP)."""

    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP: streamable HTTP endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP: Custom HTTP Headers
    tool_timeout: int = 30  # Seconds before a tool call is cancelled


class AppConfig(BaseModel):
    """The JSON configuration file structure for non-model runtime config."""

    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    model_config = ConfigDict(extra="ignore")


def load_config(path: str | Path) -> AppConfig:
    config_file = Path(path) if isinstance(path, str) else path
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return AppConfig.model_validate(data)
        except Exception as e:
            import logfire

            logfire.error(f"Failed to load config.json: {e}. Using defaults.")

    return AppConfig()


# Environment settings (secrets, etc.)
class DiscordConfig(BaseSettings):
    token: str | None = None
    default_channel_id: str | None = None
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 33280  # Default intents for receiving messages
    model_config = SettingsConfigDict(
        env_prefix="DISCORD_",
        env_file=".env",
        extra="ignore",
    )


class CartesiaConfig(BaseSettings):
    """Cartesia TTS (Text-to-Speech) credentials and tunables.

    Replaces ElevenLabs TTS. Uses the `cartesia` Python SDK (`AsyncCartesia`).
    Voice is selected by id; model defaults to `sonic-3.5`. Output is mp3 so it
    drops in as a Discord media attachment like the previous ElevenLabs output.
    """

    api_key: str | None = None
    voice_id: str | None = None
    model_id: str = "sonic-3.5"
    # Optional default language (two-letter ISO 639-1, e.g. "sk", "en"). When
    # None, the dubbing agent's per-turn `language` is used, falling back to
    # Cartesia auto-detect if that is also missing.
    language: str | None = None
    # mp3 output: container + bit_rate. sample_rate is required for mp3 too.
    sample_rate: int = 24000
    bit_rate: int = 128000
    timeout: int = 30
    model_config = SettingsConfigDict(
        env_prefix="CARTESIA_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and bool(self.voice_id)


class SttConfig(BaseSettings):
    """Google Cloud Speech-to-Text (Chirp 3) settings for voice transcription.

    Replaces ElevenLabs STT. Uses `google-cloud-speech` v2 with the `chirp_3`
    model and online (synchronous) recognize over inline audio bytes, so no
    GCS bucket is required. The service-account key is loaded explicitly from
    `STT_CREDENTIALS_FILE` so generic child processes cannot inherit the STT
    identity through Application Default Credentials.
    """

    project_id: str | None = Field(default=None, description="Google Cloud project that hosts Chirp 3 STT.")
    credentials_file: Path | None = Field(
        default=None,
        description="Service-account JSON key used only by the Chirp 3 client.",
    )
    location: str = "us"
    # Chirp 3 supports language-agnostic transcription via "auto".
    language_codes: str = "auto"
    model: str = "chirp_3"
    timeout: int = 60
    model_config = SettingsConfigDict(
        env_prefix="STT_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.project_id and self.credentials_file)


class AgentConfig(BaseSettings):
    tool_timeout: int = Field(..., description="Default timeout for tools used by this agent")
    retries: int = Field(..., description="Number of retries for this agent")
    request_limit: int = Field(..., description="Maximum number of model requests allowed in a single run")


class Neo4jConfig(BaseSettings):
    uri: str | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    enabled: bool = True
    model_config = SettingsConfigDict(
        env_prefix="NEO4J_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("database", mode="before")
    @classmethod
    def blank_database_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.uri and self.user and self.password)


class ExaConfig(BaseSettings):
    """Exa search API credentials and tunables used by the `web_search` tool.

    The same tool is registered in both the core agent and the router, but each
    uses a different search type: the core agent uses `search_type` (quality),
    the router uses `router_search_type` (low-latency fast path).
    """

    api_key: str | None = None
    num_results: int = Field(5, description="Number of results returned per search")
    search_type: str = Field("auto", description="Exa search type for the core agent (auto/fast/instant/deep-lite/deep)")
    router_search_type: str = Field("fast", description="Exa search type for the router's fail-fast path")
    timeout: int = Field(15, description="Per-request timeout in seconds for the Exa HTTP call")
    model_config = SettingsConfigDict(
        env_prefix="EXA_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


class EmbeddingConfig(BaseSettings):
    provider: str = Field(
        "google-gla",
        description="Embedding provider for graph memory (e.g. google-gla, google-vertex)",
    )
    model: str = Field(
        "gemini-embedding-001",
        description="Embedding model name for graph memory",
    )
    dimensions: int = Field(
        1536,
        description="Embedding vector dimensions used by graph-memory indexes",
    )
    model_config = SettingsConfigDict(
        env_prefix="MEMORY_EMBEDDING_",
        env_file=".env",
        extra="ignore",
    )


class MemoryConfig(BaseSettings):
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    shared_history_thread_id: str = Field(
        "main",
        validation_alias="SHARED_HISTORY_THREAD_ID",
        description="Shared short-term history thread used by conversational user channels",
    )
    cron_history_thread_id: str = Field(
        "cron",
        validation_alias="CRON_HISTORY_THREAD_ID",
        description="Dedicated short-term history thread used by cron-triggered turns",
    )
    max_conversation_history_len: int = Field(
        15,
        validation_alias="MAX_CONVERSATION_HISTORY_LEN",
        description="Maximum number of messages to keep in conversation history before compacting",
    )
    cron_messages_as_system: bool = Field(
        True,
        validation_alias="CRON_MESSAGES_AS_SYSTEM",
        description="Store cron-originated inbound prompts as system-context entries in the cron history thread",
    )
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


class GwsConfig(BaseModel):
    """Settings for the native Google Workspace (`gws`) CLI tools.

    These tools shell out to the `gws` binary with a sanitized environment (the
    STT service-account ADC is stripped) and use `summary_model` for flash-lite
    summaries of emails, calendar events, and Drive documents.
    """

    binary: str = Field("gws", description="Path/name of the gws CLI binary")
    tool_timeout: int = Field(30, description="Per-gws-subprocess timeout in seconds")
    export_char_limit: int = Field(
        8000, description="Max chars of a Drive/bemail body sent to the summary model"
    )
    model_config = ConfigDict(env_prefix="GWS_")


class RouterConfig(BaseModel):
    """Tunable limits for the auto-routing router agent.

    The router is a cheap first-stage model that either answers simple requests
    directly or escalates to the full core agent (smart model + tools).
    Limits are intentionally tighter than the core agent so the cheap path stays
    low-latency and fails fast into escalation.
    """

    enabled: bool = Field(default=True, description="Toggle the auto-routing first stage")
    # 25s accommodates the gws CLI reads (network + OAuth round trips to Google).
    # The router is still fail-fast: a second timeout/retry exhaustion escalates.
    tool_timeout: int = Field(default=25, description="Per-tool timeout for router read-only tools (gws reads need network)")
    retries: int = Field(default=1, description="Router tool retries; one retry before a tool failure escalates")
    request_limit: int = Field(default=3, description="Max model requests for a single router decision")
    history_window: int = Field(
        default=6,
        description="Number of most recent history messages passed to the router for context",
    )


class Settings(BaseSettings):
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    cartesia: CartesiaConfig = Field(default_factory=CartesiaConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    exa: ExaConfig = Field(default_factory=ExaConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(
        default_factory=lambda: AgentConfig(
            tool_timeout=30,
            retries=2,
            request_limit=10,
        )
    )
    router: RouterConfig = Field(default_factory=RouterConfig)
    smart_model: str = Field(
        "google-gla:gemini-3.6-flash",
        description="Primary (escalation) agent model in native PydanticAI provider:model format",
    )
    fast_model: str = Field(
        "google-gla:gemini-3.5-flash-lite",
        description="Fast/utility agent model in native PydanticAI provider:model format",
    )
    router_model: str = Field(
        "google-gla:gemini-3.5-flash-lite",
        description="Cheap first-stage router model in native PydanticAI provider:model format",
    )
    summary_model: str = Field(
        "openrouter:openai/gpt-oss-20b:nitro",
        description=(
            "Model used for flash-lite summaries in Google Workspace tools, in native "
            "PydanticAI provider:model format. Default routes gpt-oss-20b through OpenRouter "
            "nitro (throughput-priority) to the fastest provider."
        ),
    )
    summary_reasoning_effort: str = Field(
        "medium",
        description=(
            "Reasoning effort for the summary model. Only applied when summary_model is an "
            "openrouter:* model. One of: xhigh, high, medium, low, minimal, none."
        ),
    )
    gws: GwsConfig = Field(default_factory=GwsConfig)
    redis_host: str = "localhost"
    redis_port: int = 6379
    logfire_token: str | None = None
    environment: str = "local"
    project: str = "fergusson"
    debug: bool = False
    workspace_folder: Path = Path("workspace").absolute()
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
app_config = load_config(
    settings.workspace_folder / "config" / "config.json",
)
