from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量 / .env 读配置。字段名避开 model_ 前缀，省得触发 pydantic 保护命名空间告警。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./branch_learning.db"
    memory_retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"

    # 启动时如果 default_api_key 非空、且库里还没有任何模型，就自动写入这一条默认模型。
    default_label: str = "DeepSeek"
    default_base_url: str = "https://api.deepseek.com/v1"
    default_llm_model: str = "deepseek-chat"
    default_api_key: str = ""


settings = Settings()
