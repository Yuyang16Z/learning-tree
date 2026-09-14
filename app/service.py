"""Shared route helpers for model resolution, ancestor lookup, key masking and tool assembly."""

from collections.abc import Callable

from sqlmodel import Session, select

from . import mcp_client
from .db import engine
from .llm import LLMSpec, extract_memories
from .models import McpServer, Memory, Message, ModelConfig, Node
from .tools import TOOL_DEFS, execute_tool, mcp_tool_def


def mask_key(key: str) -> str:
    if not key or key == "mock":
        return key or ""
    tail = key[-4:] if len(key) >= 4 else key
    return "••••" + tail


def to_spec(cfg: ModelConfig) -> LLMSpec:
    return LLMSpec(
        label=cfg.label,
        base_url=cfg.base_url,
        llm_model=cfg.llm_model,
        api_key=cfg.api_key,
        protocol=cfg.protocol,
        max_tokens=cfg.max_tokens,
        context_window=cfg.context_window,
    )


def resolve_config(session: Session, config_id: int | None) -> ModelConfig | None:
    """Resolve the requested model, otherwise the default or first model; return None if absent."""
    if config_id is not None:
        return session.get(ModelConfig, config_id)
    default = session.exec(select(ModelConfig).where(ModelConfig.is_default == True)).first()  # noqa: E712
    if default:
        return default
    return session.exec(select(ModelConfig).order_by(ModelConfig.id)).first()


def get_messages(session: Session, node_id: int) -> list[Message]:
    return list(
        session.exec(select(Message).where(Message.node_id == node_id).order_by(Message.id))
    )


def get_ancestors(session: Session, node: Node) -> list[tuple[Node, list[Message]]]:
    """Return nodes and their messages from root to parent, excluding the current node."""
    chain: list[Node] = []
    pid = node.parent_id
    seen = {node.id}
    while pid is not None and pid not in seen:
        seen.add(pid)
        parent = session.get(Node, pid)
        if parent is None or parent.tree_id != node.tree_id:
            break
        chain.append(parent)
        pid = parent.parent_id
    chain.reverse()  # Root to parent.
    return [(n, get_messages(session, n.id)) for n in chain]


def get_thread(session: Session, node: Node) -> list[tuple[Node, list[Message]]]:
    """Return the complete thread from root through the current node, including each node's messages."""
    return get_ancestors(session, node) + [(node, get_messages(session, node.id))]


def assemble_tools(session: Session, requested: list[str]) -> tuple[list[dict], dict]:
    """Resolve frontend tool selections into (OpenAI tool definitions, routing table).

    - Built-in tools (fetch/web_search): use their definitions directly.
    - "mcp_server_{id}": connect and list tools; surface connection failures explicitly.
    Routing table: oai_name -> ("builtin", real) | ("mcp", spec, real).
    """
    defs: list[dict] = []
    router: dict = {}
    for name in requested:
        if name in TOOL_DEFS:
            defs.append(TOOL_DEFS[name])
            router[name] = ("builtin", name)
        elif name.startswith("mcp_server_"):
            try:
                sid = int(name[len("mcp_server_") :])
            except ValueError:
                continue
            srv = session.get(McpServer, sid)
            if not srv or not srv.enabled:
                continue
            spec = {"command": srv.command, "args": srv.args}
            try:
                for t in mcp_client.list_tools(spec):
                    oai = f"mcp_{sid}_{t['name']}"
                    defs.append(mcp_tool_def(oai, t))
                    router[oai] = ("mcp", spec, t["name"])
            except Exception as exc:
                raise RuntimeError(f"MCP「{srv.label}」连接失败，请在设置中测试连接。") from exc
    return defs, router


def make_executor(router: dict) -> Callable[[str, dict], str]:
    def execute(name: str, args: dict) -> str:
        r = router.get(name)
        if not r:
            return f"未知工具：{name}"
        if r[0] == "builtin":
            return execute_tool(name, args)
        return mcp_client.call_tool(r[1], r[2], args)  # ("mcp", spec, real_name)

    return execute


def fetch_memory_note(
    session: Session,
    tree_id: int | None,
    source_node_ids: list[int] | None = None,
    query: str = "",
    quoted_text: str = "",
    existing_text: str = "",
) -> str:
    """Choose relevant memories within the allowed source path, independently of preferences."""
    from .retrieval import retrieve_memory

    return retrieve_memory(
        session, tree_id, source_node_ids, query, quoted_text, existing_text
    ).text


def extract_and_save(
    spec: LLMSpec, tree_id: int | None, node_id: int, question: str, answer: str
) -> None:
    """Extract, deduplicate and store memories from a question/answer pair in the background.

    Extraction failures are silent."""
    if spec.api_key == "mock" or spec.base_url.startswith("mock"):
        return
    res = extract_memories(spec, question, answer)
    prefs, facts = res.get("preferences", []), res.get("facts", [])
    if not prefs and not facts:
        return
    with Session(engine) as s:
        source = s.get(Node, node_id)
        if not source or source.status != "complete":
            return
        existing = {(m.kind, m.source_node_id, m.content) for m in s.exec(select(Memory)).all()}
        for p in prefs:
            if p and ("preference", node_id, p) not in existing:
                s.add(Memory(kind="preference", content=p, tree_id=None, source_node_id=node_id))
                existing.add(("preference", node_id, p))
        for f in facts:
            if f and ("fact", node_id, f) not in existing:
                s.add(Memory(kind="fact", content=f, tree_id=tree_id, source_node_id=node_id))
                existing.add(("fact", node_id, f))
        s.commit()
