from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import JSON, Column, String
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ModelConfig(SQLModel, table=True):
    """Model configuration with separate protocol and endpoint fields; keys stay in the backend."""

    id: int | None = Field(default=None, primary_key=True)
    label: str  # Display name, e.g. "DeepSeek V3".
    base_url: str  # https://api.deepseek.com/v1
    llm_model: str  # API model identifier, e.g. "deepseek-chat".
    api_key: str  # Stored only in the backend and masked in API responses.
    protocol: Literal["openai", "anthropic"] = Field(
        default="openai", sa_column=Column(String, nullable=False, default="openai")
    )
    max_tokens: int = 4096
    context_window: int = 32768
    is_default: bool = False
    created_at: datetime = Field(default_factory=_now)


class Memory(SQLModel, table=True):
    """Long-term memory: global user preferences/habits or topic-specific facts.

    Preferences leave tree_id unset; facts are scoped to a tree."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "preference" | "fact"
    content: str
    tree_id: int | None = Field(default=None, index=True)  # Unset for global preferences.
    source_node_id: int | None = None
    created_at: datetime = Field(default_factory=_now)


class MemoryEmbedding(SQLModel, table=True):
    """Rebuildable local vectors; original Memory records remain authoritative."""

    memory_id: int = Field(foreign_key="memory.id", primary_key=True)
    model_key: str = Field(primary_key=True)
    content_hash: str
    vector: list[float] = Field(sa_column=Column(JSON, nullable=False))


class PreferenceProfile(SQLModel, table=True):
    """User-owned singleton; an empty profile intentionally suppresses legacy preferences."""

    id: int = Field(default=1, primary_key=True)
    content: str = ""
    revision: str = Field(default_factory=lambda: uuid4().hex)
    # Clear-all advances this token so an in-flight extraction cannot restore deleted memories.
    reset_revision: str = ""
    updated_at: datetime = Field(default_factory=_now)


class PreferenceSupplement(SQLModel, table=True):
    """Source-linked AI additions kept separate from the user-owned profile."""

    id: int | None = Field(default=None, primary_key=True)
    content: str
    scope: Literal["global", "topic"] = Field(
        default="global", sa_column=Column(String, nullable=False, default="global")
    )
    tree_id: int | None = Field(default=None, index=True)
    source_node_id: int | None = Field(default=None, index=True)
    source_tree_id: int | None = Field(default=None, index=True)
    evidence: str
    status: Literal["active", "pending"] = Field(
        default="active", sa_column=Column(String, nullable=False, default="active")
    )
    user_edited: bool = False
    revision: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class PreferenceLearningState(SQLModel, table=True):
    """Singleton switch; revisions invalidate extraction started before a settings change."""

    id: int = Field(default=1, primary_key=True)
    enabled: bool = True
    revision: str = Field(default_factory=lambda: uuid4().hex)


class PreferenceExtraction(SQLModel, table=True):
    """Content-free processed-input ledger retained after preference deletion."""

    key: str = Field(primary_key=True)
    node_id: int = Field(index=True)
    tree_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=_now)


class McpServer(SQLModel, table=True):
    """A user-configured stdio MCP server whose tools are available to agent turns when enabled."""

    id: int | None = Field(default=None, primary_key=True)
    label: str  # Display name, e.g. "fetch" or "filesystem".
    command: str  # Launch command, e.g. "uvx", "npx" or "python".
    args: list | None = Field(default=None, sa_column=Column(JSON))  # E.g. ["mcp-server-fetch"].
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class KnowledgeTree(SQLModel, table=True):
    """A knowledge tree represents one learning topic listed in the left sidebar."""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    archived: bool = False
    created_at: datetime = Field(default_factory=_now)


class TreeIdSequence(SQLModel, table=True):
    """Content-free high-water marks: id=1 for topics, id=2 for conversation nodes."""

    id: int = Field(default=1, primary_key=True)
    last_id: int = 0


class Node(SQLModel, table=True):
    """A node represents a focused conversation within a tree.

    parent_id links the tree; seed_text stores the text selected to start a branch."""

    id: int | None = Field(default=None, primary_key=True)
    tree_id: int = Field(foreign_key="knowledgetree.id", index=True)
    parent_id: int | None = Field(default=None, foreign_key="node.id", index=True)
    title: str
    seed_text: str | None = None  # Text selected in the parent node when creating a branch.
    title_state: str = "legacy"  # empty/pending/ai/fallback/manual; separate from answer status
    summary: str | None = None  # Cached summary used as context along the ancestor path.
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
    """A question/answer message within a node; answered_by records the responding model."""

    id: int | None = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="node.id", index=True)
    role: str  # "user" | "assistant"
    status: str = "complete"
    content: str
    answered_by: str | None = None  # Assistant messages only: model display name.
    # Multimodal input: user images as data URIs, passed with the ancestor path.
    images: list | None = Field(default=None, sa_column=Column(JSON))
    document_ids: list[str] | None = Field(default=None, sa_column=Column(JSON))
    # Deep thinking: assistant reasoning from the model's reasoning_content field.
    reasoning: str | None = None
    # Tool steps: [{"tool":..,"args":..,"result":..}] for collapsible frontend display.
    steps: list | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class ContextSummary(SQLModel, table=True):
    """Disposable, source-versioned context compression; never a long-term memory."""

    key: str = Field(primary_key=True)
    tree_id: int = Field(foreign_key="knowledgetree.id", index=True)
    source_node_ids: list[int] = Field(sa_column=Column(JSON, nullable=False))
    source_message_ids: list[int] = Field(sa_column=Column(JSON, nullable=False))
    fingerprint: str
    model_fingerprint: str
    prompt_version: str
    content: str
    created_at: datetime = Field(default_factory=_now)
