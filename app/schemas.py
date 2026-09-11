from typing import Literal

from pydantic import BaseModel, Field


class ModelConfigIn(BaseModel):
    label: str
    base_url: str
    llm_model: str
    api_key: str
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    is_default: bool = False


class ModelConfigOut(BaseModel):
    id: int
    label: str
    base_url: str
    llm_model: str
    key_hint: str  # 脱敏后的 key，只露后 4 位
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = 4096
    is_default: bool


class TreeIn(BaseModel):
    title: str
    root_question: str | None = None  # 可选：建树时顺手问的第一个问题


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
    config_id: int | None = None  # 用哪个模型答；不传则用默认模型
    images: list[str] | None = None  # 随问题带的图片（data URI），会回传给模型
    tools: list[str] | None = None  # 本次开启的工具，如 ["fetch","web_search"]
    deep_think: bool = False  # 深度思考：走推理、把思考过程单独流出来


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
