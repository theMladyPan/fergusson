import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
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


class ElevenLabsConfig(BaseSettings):
    api_key: str | None = None
    voice_id: str = "pNInz6obpgDQGcFmaJgB"  # Adam
    model_id_tts: str = "eleven_multilingual_v2"
    model_id_stt: str = "scribe_v1"
    model_config = SettingsConfigDict(
        env_prefix="ELEVENLABS_",
        env_file=".env",
        extra="ignore",
    )


class AgentConfig(BaseSettings):
    tool_timeout: int = Field(..., description="Default timeout for tools used by this agent")
    retries: int = Field(..., description="Number of retries for this agent")
    tool_call_limit: int = Field(..., description="Number of tool calls a agent can invoke")


class Settings(BaseSettings):
    discord: DiscordConfig = DiscordConfig()
    elevenlabs: ElevenLabsConfig = ElevenLabsConfig()
    agent: AgentConfig = AgentConfig(
        tool_timeout=20,
        retries=2,
        tool_call_limit=4,
    )
    subagent: AgentConfig = AgentConfig(
        tool_timeout=5,
        retries=3,
        tool_call_limit=7,
    )
    max_conversation_history_len: int = Field(
        15,
        description="Maximum number of messages to keep in conversation history before compacting",
    )
    smart_model: str = Field(
        "google-gla:gemini-3-flash-preview",
        description="Primary agent model in native PydanticAI provider:model format",
    )
    fast_model: str = Field(
        "google-gla:gemini-3.1-flash-lite-preview",
        description="Fast/utility agent model in native PydanticAI provider:model format",
    )
    shared_history_thread_id: str = Field(
        "main",
        description="Single shared short-term history thread used across all channels",
    )
    cron_messages_as_system: bool = Field(
        True,
        description="Store cron-originated inbound prompts as system-context entries in shared history",
    )
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
