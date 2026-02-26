from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscordConfig(BaseSettings):
    token: Optional[str] = None
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 33280  # Default intents for receiving messages

    model_config = SettingsConfigDict(
        env_prefix="DISCORD_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


class Settings(BaseSettings):
    discord: DiscordConfig = DiscordConfig()
    redis_host: str = "localhost"
    redis_port: int = 6379

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()
