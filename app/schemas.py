import re
from datetime import datetime
from typing import Annotated, Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class ModelConfigIn(BaseModel):
    label: str
    base_url: str
    llm_model: str
    api_key: str
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    context_window: int = Field(default=32768, ge=8192, le=2097152)
    # Soft target for one request's input; None = automatic. Compaction starts at it.
    target_input_tokens: int | None = Field(default=None, ge=16384, le=2097152)
    is_default: bool = False
    # Write every answer's context summaries with this model (at most one model).
    use_for_summaries: bool = False

    @model_validator(mode="after")
    def validate_context_budget(self):
        headroom = max(512, self.context_window // 20)
        if self.context_window - self.max_tokens - headroom < 1024:
            raise ValueError(
                "context_window must leave at least 1024 input units after answer reserve and headroom"
            )
        if self.target_input_tokens is not None and self.target_input_tokens > self.context_window:
            raise ValueError("target_input_tokens must not exceed context_window")
        return self


class ModelDiscoverIn(BaseModel):
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(min_length=1, max_length=2000)
    llm_model: str = Field(min_length=1, max_length=200)
    api_key: str = ""  # Blank with config_id: use the stored key of that configuration.
    config_id: int | None = None


class ModelDiscoverOut(BaseModel):
    context_window: int | None
    max_output_tokens: int | None
    source: Literal["provider", "catalog", "unknown"]
    note: str


class ModelConfigOut(BaseModel):
    id: int
    label: str
    base_url: str
    llm_model: str
    key_hint: str  # Masked key showing only the last four characters.
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = 4096
    context_window: int = 32768
    target_input_tokens: int | None = None
    is_default: bool
    use_for_summaries: bool = False


class TreeIn(BaseModel):
    title: str
    root_question: str | None = None  # Optional initial question when creating a tree.


class TreeOut(BaseModel):
    id: int
    title: str
    root_node_id: int
    archived: bool = False
    # Learning-style preset id for new questions in this topic; None = the default way.
    learning_preset: str | None = None


class TreePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    archived: bool | None = Field(default=None, strict=True)
    # A preset id, or null to return new questions to the default way of answering.
    learning_preset: str | None = Field(default=None, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("Provide a title, archive status or learning style")
        if any(
            getattr(self, field) is None for field in self.model_fields_set - {"learning_preset"}
        ):
            raise ValueError("Title and archive status cannot be null")
        return self


# A learner's own mark on a node; None means "not marked".
Understanding = Literal["open", "partial", "clear"]
UNDERSTANDING_LEVELS: tuple[str, ...] = get_args(Understanding)


class NodeOut(BaseModel):
    id: int
    tree_id: int
    parent_id: int | None
    title: str
    title_state: str = "legacy"
    seed_text: str | None
    has_summary: bool
    kind: str = "followup"
    status: str = "idle"
    error: str | None = None
    source_node_id: int | None = None
    source_message_id: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    learning_note: str | None = None
    understanding: Understanding | None = None
    learning_preset: str | None = None
    revision_of: int | None = None
    request_id: str | None = None


class BranchIn(BaseModel):
    seed_text: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    source_message_id: int | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    learning_note: str | None = Field(default=None, max_length=10000)


# Match the composer: at most four images of 4 MiB each, sent as base64 data URLs.
MAX_QUESTION_IMAGES = 4
MAX_IMAGE_URL_CHARS = (4 * 1024 * 1024 * 4) // 3 + 128  # Base64 size plus the data-URL prefix.


class AskIn(BaseModel):
    question: str = Field(default="", max_length=100000)
    mode: Literal["continue", "retry", "revise"] = "continue"
    question_message_id: int | None = None
    request_id: str | None = Field(default=None, max_length=100)
    config_id: int | None = None  # Omit to use the default model.
    # Attached data URIs included in model input.
    images: list[Annotated[str, Field(max_length=MAX_IMAGE_URL_CHARS)]] | None = Field(
        default=None, max_length=MAX_QUESTION_IMAGES
    )
    document_ids: list[str] = Field(default_factory=list, max_length=4)
    tools: list[str] | None = None  # Tools enabled for this request, e.g. ["fetch", "web_search"].
    deep_think: bool = False  # Request reasoning and stream it separately from the answer.
    # Learning-style preset id. Absent: the topic's default (revise: the original
    # question's); null: the default way of answering. Retry ignores it.
    learning_preset: str | None = Field(default=None, max_length=80)


class TestOut(BaseModel):
    ok: bool
    detail: str


# Header names the transport sets itself; a configured value would break the protocol.
RESERVED_MCP_HEADERS = frozenset(
    {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,100}")


class McpServerIn(BaseModel):
    """stdio needs a command; http needs an http(s) URL without credentials.

    Header values may be blank on update: a blank value keeps the stored value of
    that header. Omitting `headers` keeps every stored header; `{}` clears them."""

    label: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = []
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] | None = None
    allow_private: bool = False
    enabled: bool = True
    # Which calls wait for approval: tools not marked read-only, every tool, or none.
    confirm_tools: Literal["writes", "always", "never"] = "writes"

    @model_validator(mode="after")
    def validate_transport(self):
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("A local MCP server needs a launch command")
            return self
        parts = urlsplit((self.url or "").strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("A remote MCP server needs an http(s) URL")
        if parts.port == 0:  # Reading the port also raises ValueError when out of range.
            raise ValueError("Invalid port")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Put credentials in headers, not in the URL")
        headers = self.headers or {}
        if len(headers) > 20:
            raise ValueError("At most 20 headers")
        seen: set[str] = set()
        for name, value in headers.items():
            lowered = name.lower()
            if not _HEADER_NAME.fullmatch(name) or lowered in RESERVED_MCP_HEADERS:
                raise ValueError("Invalid or reserved header name")
            if lowered in seen:
                raise ValueError("Duplicate header name")
            seen.add(lowered)
            if len(value) > 8000 or any(char in value for char in "\r\n\0"):
                raise ValueError("Invalid header value")
        return self


class McpServerOut(BaseModel):
    """Header values never leave the backend; only their names are listed."""

    id: int
    label: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str
    args: list[str]
    url: str | None = None
    header_names: list[str] = []
    allow_private: bool = False
    enabled: bool
    confirm_tools: Literal["writes", "always", "never"] = "writes"


class McpTestOut(BaseModel):
    ok: bool
    detail: str
    tools: list[str] = []


class MemoryOut(BaseModel):
    id: int
    kind: str
    content: str
    tree_id: int | None


class PreferenceProfileOut(BaseModel):
    content: str
    revision: str
    managed: bool


class PreferenceProfileIn(BaseModel):
    content: str = Field(max_length=6000)
    revision: str = Field(min_length=1, max_length=100)


class PreferenceLearningOut(BaseModel):
    enabled: bool
    revision: str


class PreferenceLearningIn(BaseModel):
    enabled: bool
    revision: str = Field(min_length=1, max_length=100)


class RetrievalSettingsOut(BaseModel):
    include_siblings: bool
    include_other_trees: bool
    revision: str


class RetrievalSettingsIn(BaseModel):
    include_siblings: bool
    include_other_trees: bool
    revision: str = Field(min_length=1, max_length=100)


class PreferenceSupplementOut(BaseModel):
    id: int
    content: str
    scope: Literal["global", "topic"]
    tree_id: int | None
    source_node_id: int | None
    source_tree_id: int | None
    evidence: str
    status: Literal["active", "pending"]
    user_edited: bool
    revision: str
    created_at: datetime
    updated_at: datetime
    tree_title: str | None
    source_tree_title: str | None


class PreferenceSupplementPageOut(BaseModel):
    items: list[PreferenceSupplementOut]
    total: int
    page: int
    page_size: int


class PreferenceSupplementPatch(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    revision: str = Field(min_length=1, max_length=100)


class FactOut(MemoryOut):
    source_node_id: int | None
    tree_title: str | None
    created_at: datetime
    fact_type: str = "conclusion"
    superseded_by: int | None = None
    # The learner stopped this fact from being used in answers.
    muted: bool = False


class FactUseIn(BaseModel):
    """Stop a fact from being used in answers (true), or use it again (false)."""

    muted: StrictBool


class FactTopicOut(BaseModel):
    tree_id: int | None
    title: str
    count: int


class FactPageOut(BaseModel):
    items: list[FactOut]
    total: int
    page: int
    page_size: int
    topics: list[FactTopicOut]


class FactPatch(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    # Legacy facts can exceed the editing limit; preserving the original is required for CAS.
    expected_content: str


class FactDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


class ExplainIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    config_id: int | None = None
    source_message_id: int | None = None
    locale: Literal["zh-CN", "en"] = "zh-CN"


class NodePatch(BaseModel):
    """Only the fields sent are changed; an explicit null clears that field."""

    learning_note: str | None = Field(default=None, max_length=10000)
    understanding: Understanding | None = None


class StopIn(BaseModel):
    request_id: str | None = Field(default=None, max_length=100)


class ToolConfirmIn(BaseModel):
    """The learner's decision on the tool call an answer is waiting for."""

    request_id: str = Field(min_length=1, max_length=100)
    index: int = Field(ge=1)
    allow: bool


class TitleIn(BaseModel):
    config_id: int | None = None
