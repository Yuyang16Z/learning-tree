from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read settings from environment variables or .env.

    Avoid the model_ field prefix to prevent Pydantic protected-namespace warnings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./branch_learning.db"
    memory_retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"

    # Seed this default model at startup if a key is configured and no models exist.
    default_label: str = "DeepSeek"
    default_base_url: str = "https://api.deepseek.com/v1"
    default_llm_model: str = "deepseek-chat"
    default_api_key: str = ""


settings = Settings()
