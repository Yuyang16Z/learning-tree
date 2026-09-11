"""跨路由复用的小工具：模型解析、脊柱查询、key 脱敏、工具装配。"""

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
    )


def resolve_config(session: Session, config_id: int | None) -> ModelConfig | None:
    """指定了就用指定的；否则用默认；再否则用第一条；都没有返回 None。"""
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
    """从根到父节点，连同各自的消息。当前节点不含在内。"""
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
    chain.reverse()  # 根 → 父
    return [(n, get_messages(session, n.id)) for n in chain]


def get_thread(session: Session, node: Node) -> list[tuple[Node, list[Message]]]:
    """整条线程：根 → 当前节点（含当前），每个节点连同它那一次问答。"""
    return get_ancestors(session, node) + [(node, get_messages(session, node.id))]


def assemble_tools(session: Session, requested: list[str]) -> tuple[list[dict], dict]:
    """把前端请求的工具名解析成 (OpenAI 工具定义, 路由表)。

    - 内置工具（fetch/web_search）：直接取定义。
    - "mcp_server_{id}"：连接该 MCP server 列出工具；连接失败明确报错。
    路由表：oai_name -> ("builtin", real) | ("mcp", spec, real)。
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
    session: Session, tree_id: int | None, source_node_ids: list[int] | None = None
) -> str:
    """把相关记忆拼成一段，注入脊柱 system：全局偏好 + 本知识树的事实。"""
    prefs = session.exec(
        select(Memory).where(Memory.kind == "preference").order_by(Memory.id.desc())
    ).all()[:8]
    facts = (
        session.exec(
            select(Memory)
            .where(
                Memory.kind == "fact",
                Memory.tree_id == tree_id,
                Memory.source_node_id.in_(source_node_ids or []),
            )
            .order_by(Memory.id.desc())
        ).all()[:8]
        if tree_id is not None
        else []
    )
    if not prefs and not facts:
        return ""
    lines: list[str] = []
    if prefs:
        lines.append("【关于用户（长期记忆，自然体现即可，别生硬复述）】")
        lines += [f"- {m.content}" for m in reversed(prefs)]
    if facts:
        lines.append("【本主题已知（记忆）】")
        lines += [f"- {m.content}" for m in reversed(facts)]
    return "\n".join(lines)


def extract_and_save(
    spec: LLMSpec, tree_id: int | None, node_id: int, question: str, answer: str
) -> None:
    """后台调用：从一轮问答提炼记忆并去重存库。失败静默。"""
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
