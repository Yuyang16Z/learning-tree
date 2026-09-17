from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ModelConfigIn(BaseModel):
    label: str
    base_url: str
    llm_model: str
    api_key: str
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    context_window: int = Field(default=32768, ge=8192, le=2097152)
    is_default: bool = False

    @model_validator(mode="after")
    def validate_context_budget(self):
        headroom = max(512, self.context_window // 20)
        if self.context_window - self.max_tokens - headroom < 1024:
            raise ValueError(
                "context_window must leave at least 1024 input units after answer reserve and headroom"
            )
        return self


class ModelConfigOut(BaseModel):
    id: int
    label: str
    base_url: str
    llm_model: str
    key_hint: str  # Masked key showing only the last four characters.
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = 4096
    context_window: int = 32768
    is_default: bool


class TreeIn(BaseModel):
    title: str
    root_question: str | None = None  # Optional initial question when creating a tree.


class TreeOut(BaseModel):
    id: int
    title: str
    root_node_id: int


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
    revision_of: int | None = None
    request_id: str | None = None


class BranchIn(BaseModel):
    seed_text: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    source_message_id: int | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    learning_note: str | None = Field(default=None, max_length=10000)


class AskIn(BaseModel):
    question: str = Field(default="", max_length=100000)
    mode: Literal["continue", "retry", "revise"] = "continue"
    question_message_id: int | None = None
    request_id: str | None = Field(default=None, max_length=100)
    config_id: int | None = None  # Omit to use the default model.
    images: list[str] | None = None  # Attached data URIs included in model input.
    document_ids: list[str] = Field(default_factory=list, max_length=4)
    tools: list[str] | None = None  # Tools enabled for this request, e.g. ["fetch", "web_search"].
    deep_think: bool = False  # Request reasoning and stream it separately from the answer.


class TestOut(BaseModel):
    ok: bool
    detail: str


class McpServerIn(BaseModel):
    label: str
    command: str
    args: list[str] = []
    enabled: bool = True


class McpServerOut(BaseModel):
    id: int
    label: str
    command: str
    args: list[str]
    enabled: bool


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


class FactOut(MemoryOut):
    source_node_id: int | None
    tree_title: str | None
    created_at: datetime


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
    learning_note: str | None = Field(default=None, max_length=10000)


class StopIn(BaseModel):
    request_id: str | None = Field(default=None, max_length=100)


class TitleIn(BaseModel):
    config_id: int | None = None
