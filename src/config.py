import json
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base Models for JSON config
class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: str = "OPENAI_API_KEY"

class ChannelConfig(BaseModel):
    enabled: bool = False

class MCPServerConfig(BaseModel):
    """MCP server connection configuration (stdio or HTTP)."""
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: List[str] = Field(default_factory=list)  # Stdio: command arguments
    env: Dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP: streamable HTTP endpoint URL
    headers: Dict[str, str] = Field(default_factory=dict)  # HTTP: Custom HTTP Headers
    tool_timeout: int = 30  # Seconds before a tool call is cancelled

class AppConfig(BaseModel):
    """The JSON Configuration file structure."""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    channels: Dict[str, ChannelConfig] = Field(default_factory=dict)
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=dict)

# Load JSON config
def load_config(path: str = "workspace/config/config.json") -> AppConfig:
    config_file = Path(path)
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return AppConfig.model_validate(data)
        except Exception as e:
            from loguru import logger
            logger.error(f"Failed to load config.json: {e}. Using defaults.")
    return AppConfig()

# Environment settings (secrets, etc.)
class DiscordConfig(BaseSettings):
    token: Optional[str] = None
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 33280  # Default intents for receiving messages
    model_config = SettingsConfigDict(env_prefix="DISCORD_")

class Settings(BaseSettings):
    discord: DiscordConfig = DiscordConfig()
    redis_host: str = "localhost"
    redis_port: int = 6379

app_config = load_config()
settings = Settings()
