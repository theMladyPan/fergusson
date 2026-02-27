import json
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Base Models for JSON config
class ProviderConfig(BaseModel):
    type: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None


class ModelSelection(BaseModel):
    provider: str
    model: str


class ModelsConfig(BaseModel):
    smart: ModelSelection = Field(
        default_factory=lambda: ModelSelection(
            provider="default",
            model="gpt-4.1",
        )
    )
    fast: ModelSelection = Field(
        default_factory=lambda: ModelSelection(
            provider="default",
            model="gpt-4.1-mini",
        )
    )


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
    """The JSON Configuration file structure."""

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


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
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 33280  # Default intents for receiving messages
    model_config = SettingsConfigDict(
        env_prefix="DISCORD_",
        env_file=".env",
        extra="ignore",
    )


class AgentConfig(BaseSettings):
    tool_timeout: int = Field(..., description="Default timeout for tools used by this agent")
    retries: int = Field(..., description="Number of retries for this agent")


class Settings(BaseSettings):
    discord: DiscordConfig = DiscordConfig()
    agent: AgentConfig = AgentConfig(
        tool_timeout=30,
        retries=2,
    )
    subagent: AgentConfig = AgentConfig(
        tool_timeout=10,
        retries=3,
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
