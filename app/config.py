from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read settings from environment variables or .env.

    Avoid the model_ field prefix to prevent Pydantic protected-namespace warnings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./branch_learning.db"
    # Extra Host names to accept besides loopback names and Tailscale (*.ts.net),
    # comma-separated, e.g. "learning.local". Other hosts are rejected (DNS rebinding).
    allowed_hosts: str = ""
    memory_retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"
    # Share of the input budget at which older history is compacted; the hard
    # budget still bounds every request. CONTEXT_COMPACTION_RATIO in .env.
    context_compaction_ratio: float = Field(default=0.6, ge=0.1, le=1.0)

    # Seed this default model at startup if a key is configured and no models exist.
    default_label: str = "DeepSeek"
    default_base_url: str = "https://api.deepseek.com/v1"
    default_llm_model: str = "deepseek-chat"
    default_api_key: str = ""


settings = Settings()
