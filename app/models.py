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
    # Soft target for one request's input in tokens; None = automatic (ContextPolicy).
    target_input_tokens: int | None = None
    is_default: bool = False
    # At most one model writes the context summaries of every answer; none = the answer's.
    use_for_summaries: bool = False
    created_at: datetime = Field(default_factory=_now)


class ModelCalibration(SQLModel, table=True):
    """Learned ratio between provider-reported and locally estimated input tokens.

    Side data outside the change feed: it is updated after answers, so it must not
    make every open window reload the model list. Label, key, window, reserve and target
    edits keep it; it is deleted with its model or when an edit switches the model's protocol,
    address or name, and writes require the model to exist (ids are reused)."""

    config_id: int = Field(primary_key=True)
    samples: int = 0
    ratio: float = 1.0
    estimated_total: int = 0
    actual_total: int = 0
    updated_at: datetime = Field(default_factory=_now)


class Memory(SQLModel, table=True):
    """Long-term memory: global user preferences/habits or topic-specific facts.

    Preferences leave tree_id unset; facts are scoped to a tree."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "preference" | "fact"
    content: str
    tree_id: int | None = Field(default=None, index=True)  # Unset for global preferences.
    source_node_id: int | None = None
    created_at: datetime = Field(default_factory=_now)
    # Structured facts: definition | condition | misconception | open_question |
    # learner_state | conclusion. Legacy rows read as conclusions.
    fact_type: str = Field(
        default="conclusion", sa_column=Column(String, nullable=False, default="conclusion")
    )
    terms: list[str] | None = Field(default=None, sa_column=Column(JSON))
    # A later fact that corrects this one; superseded facts leave retrieval and the
    # search index but stay visible in Settings until deleted.
    superseded_by: int | None = None
    updated_at: datetime | None = None  # Set by hand edits only; never by supersession.
    # "extracted": written by automatic extraction. "legacy": stored before origins
    # were recorded, when hand edits left no trace, so it may hold the user's own
    # correction. Only extracted facts never edited by hand can be superseded.
    origin: str = Field(
        default="legacy", sa_column=Column(String, nullable=False, default="legacy")
    )


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


class RetrievalSettings(SQLModel, table=True):
    """Singleton: wider memory scopes are opt-in; revisions detect concurrent saves.

    The current learning path is always searched. Siblings and other topics only
    contribute labelled, separately budgeted facts after the user turns them on."""

    id: int = Field(default=1, primary_key=True)
    include_siblings: bool = False
    include_other_trees: bool = False
    revision: str = Field(default_factory=lambda: uuid4().hex)


class PreferenceExtraction(SQLModel, table=True):
    """Content-free processed-input ledger retained after preference deletion."""

    key: str = Field(primary_key=True)
    node_id: int = Field(index=True)
    tree_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=_now)


class McpServer(SQLModel, table=True):
    """A user-configured MCP server whose tools are available to agent turns when enabled.

    stdio servers are local programs (command/args); http servers are remote Streamable
    HTTP endpoints (url/headers). Header values are credentials: they stay in this table
    and are never returned by the API, logged or exported."""

    id: int | None = Field(default=None, primary_key=True)
    label: str  # Display name, e.g. "fetch" or "filesystem".
    command: str  # Launch command, e.g. "uvx", "npx" or "python"; empty for http servers.
    args: list | None = Field(default=None, sa_column=Column(JSON))  # E.g. ["mcp-server-fetch"].
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    transport: Literal["stdio", "http"] = Field(
        default="stdio", sa_column=Column(String, nullable=False, default="stdio")
    )
    url: str | None = None  # Streamable HTTP endpoint, e.g. "https://example.com/mcp".
    headers: dict | None = Field(default=None, sa_column=Column(JSON))  # Name -> value.
    # Opt-in for loopback, private, link-local and CGNAT addresses (a local dev server).
    allow_private: bool = False


class KnowledgeTree(SQLModel, table=True):
    """A knowledge tree represents one learning topic listed in the left sidebar."""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    archived: bool = False
    created_at: datetime = Field(default_factory=_now)
    # The learning-style preset new questions in this topic are asked with
    # (app/learning_presets.py); NULL = the default way of answering. Local
    # organization like `archived`: not exported.
    learning_preset: str | None = None


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
    # The learner's own mark for this node: open | partial | clear; NULL = not marked.
    # Not source content: it never changes answers, titles, summaries or request state.
    understanding: str | None = None
    # The learning-style preset this node's question was asked with; NULL = default.
    # Kept as history when that style later disappears (its answer then used none).
    learning_preset: str | None = None
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
    # Tool steps for collapsible display: [{"tool", "result" (a 3,000-character preview),
    # "truncated", "call_index", "request_id"}]; the last two locate the ToolResult row.
    steps: list | None = Field(default=None, sa_column=Column(JSON))
    # Per-answer numbers (token estimates, usage, timings) and fixed labels such as the
    # budget that stopped tool rounds; never prompt text.
    diagnostics: dict | None = Field(default=None, sa_column=Column(JSON))
    # Memory records the answer was given: [{memory_id, source_node_id, tree_id, scope}];
    # topic titles are read from the live topic (app/answer_sources.py), and a tree_title
    # that earlier rows stored is ignored. Local provenance only; excluded from exports.
    sources: list | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class ToolResult(SQLModel, table=True):
    """The complete text one tool call returned during an answer, for paging.

    Bound to the node and request that produced it; `call_index` counts the answer's
    tool calls from 1. Side data outside the change feed: deleted with its node or
    topic and never exported. `truncated` marks text cut at the storage limit."""

    id: int | None = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="node.id", index=True)
    request_id: str = Field(index=True)
    call_index: int
    tool_name: str
    arguments: dict | None = Field(default=None, sa_column=Column(JSON))
    content: str
    truncated: bool = False
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
