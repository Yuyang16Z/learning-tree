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
    # "required": requests that do not come from this computer (a non-loopback Host or a
    # proxy) must carry the phone-access cookie. "off" disables that check entirely.
    access_token_mode: Literal["required", "off"] = "required"
    # Where that token is kept; a relative path starts at the project root. The Docker
    # image keeps it in the /data volume beside the database.
    access_token_file: str = ".runtime/phone-access-token"
    memory_retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"
    # Share of the input budget at which older history is compacted; the hard
    # budget still bounds every request. CONTEXT_COMPACTION_RATIO in .env.
    context_compaction_ratio: float = Field(default=0.6, ge=0.1, le=1.0)
    # Tool rounds of one answer stop after this many seconds, or once the tool results
    # gathered reach this many estimated tokens; the model then answers from what it
    # has. Checked between rounds, so a running call is never interrupted.
    # AGENT_TIME_LIMIT_SECONDS / AGENT_TOOL_TOKEN_BUDGET in .env.
    agent_time_limit_seconds: float = Field(default=180, gt=0)
    agent_tool_token_budget: int = Field(default=60000, gt=0)

    # Seed this default model at startup if a key is configured and no models exist.
    default_label: str = "DeepSeek"
    default_base_url: str = "https://api.deepseek.com/v1"
    default_llm_model: str = "deepseek-chat"
    default_api_key: str = ""


settings = Settings()
