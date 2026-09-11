from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import JSON, Column, String
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ModelConfig(SQLModel, table=True):
    """一条模型配置；协议与服务地址分开保存，密钥只留在后端。"""

    id: int | None = Field(default=None, primary_key=True)
    label: str  # 显示名，如 "DeepSeek V3"
    base_url: str  # https://api.deepseek.com/v1
    llm_model: str  # deepseek-chat（发给 API 的 model 字段）
    api_key: str  # 只存后端，接口返回时脱敏
    protocol: Literal["openai", "anthropic"] = Field(
        default="openai", sa_column=Column(String, nullable=False, default="openai")
    )
    max_tokens: int = 4096
    is_default: bool = False
    created_at: datetime = Field(default_factory=_now)


class Memory(SQLModel, table=True):
    """长期记忆。preference=关于用户的持久偏好/习惯（全局，tree_id 空）；fact=某话题的关键结论（按 tree）。"""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "preference" | "fact"
    content: str
    tree_id: int | None = Field(default=None, index=True)  # fact 挂到具体知识树；preference 为空
    source_node_id: int | None = None
    created_at: datetime = Field(default_factory=_now)


class McpServer(SQLModel, table=True):
    """用户配置的一个 MCP server（stdio 传输）。启用后，它暴露的工具会进 agent 回合。"""

    id: int | None = Field(default=None, primary_key=True)
    label: str  # 显示名，如 "fetch"、"filesystem"
    command: str  # 启动命令，如 "uvx"、"npx"、"python"
    args: list | None = Field(default=None, sa_column=Column(JSON))  # 参数，如 ["mcp-server-fetch"]
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class KnowledgeTree(SQLModel, table=True):
    """一整棵知识树 = 一个学习主题。左侧栏列的就是它。"""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    created_at: datetime = Field(default_factory=_now)


class Node(SQLModel, table=True):
    """树上的一个节点 = 一小段聚焦的问答。parent_id 串成树；seed_text 是开分支时选中的引子。"""

    id: int | None = Field(default=None, primary_key=True)
    tree_id: int = Field(foreign_key="knowledgetree.id", index=True)
    parent_id: int | None = Field(default=None, foreign_key="node.id", index=True)
    title: str
    seed_text: str | None = None  # 从父节点划词开分支时选中的那段概念
    title_state: str = "legacy"  # empty/pending/ai/fallback/manual; separate from answer status
    summary: str | None = None  # 缓存摘要，喂给子节点当上下文（脊柱用）
    kind: str = "followup"
    status: str = "idle"
    error: str | None = None
    source_node_id: int | None = None
    source_message_id: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    learning_note: str | None = None
    revision_of: int | None = None
    request_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)


class Message(SQLModel, table=True):
    """节点内的一轮问答。answered_by 记录这段是哪个模型答的。"""

    id: int | None = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="node.id", index=True)
    role: str  # "user" | "assistant"
    status: str = "complete"
    content: str
    answered_by: str | None = None  # assistant 专用：模型显示名
    # 多模态：用户消息带的图片（data URI 列表），会随脊柱一起回传给模型
    images: list | None = Field(default=None, sa_column=Column(JSON))
    # 深度思考：assistant 的思考过程（推理模型的 reasoning_content）
    reasoning: str | None = None
    # 工具调用步骤：[{"tool":..,"args":..,"result":..}]，供前端折叠展示
    steps: list | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
