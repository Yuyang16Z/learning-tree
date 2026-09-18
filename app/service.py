"""Shared route helpers for model resolution, ancestor lookup, key masking and tool assembly."""

from collections.abc import Callable

from sqlmodel import Session, select

from . import mcp_client
from .db import engine
from .llm import LLMSpec, extract_memories
from .memory_preferences import begin_memory_write
from .models import KnowledgeTree, McpServer, Memory, Message, ModelConfig, Node, PreferenceProfile
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
    for name in dict.fromkeys(requested):
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
            spec = mcp_client.server_spec(srv.command, srv.args)
            try:
                for t in mcp_client.list_tools(spec):
                    oai = f"mcp_{sid}_{t['name']}"
                    definition = mcp_tool_def(oai, t)
                    definition["function"]["description"] = f"[{srv.label}] " + definition[
                        "function"
                    ].get("description", "")
                    defs.append(definition)
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
    """Best-effort extraction once per user turn; user-owned preferences are immutable.

    Claims commit before the provider runs. They contain no conversation text and
    intentionally survive failures, retries and preference deletion, so replaying
    an old turn cannot resurrect a preference the user removed.
    """
    import hashlib
    import json
    from datetime import datetime, timezone
    from uuid import uuid4

    from .models import PreferenceExtraction, PreferenceSupplement
    from .preference_learning import (
        extraction_context,
        normalize_preference,
        preference_snapshot,
        validate_preference,
    )

    if spec.api_key == "mock" or spec.base_url.startswith("mock"):
        return
    with Session(engine) as s:
        begin_memory_write(s)
        source = s.get(Node, node_id)
        if (
            not source
            or source.status != "complete"
            or source.tree_id != tree_id
            or s.get(KnowledgeTree, source.tree_id) is None
        ):
            return
        source_identity = (source.tree_id, source.created_at, source.request_id)
        # Regenerating an answer changes request_id/answer, but not the source
        # user message. Legacy multi-turn nodes still distinguish each message.
        message = s.exec(
            select(Message)
            .where(
                Message.node_id == node_id,
                Message.role == "user",
                Message.content == question,
            )
            .order_by(Message.id.desc())
        ).first()
        message_identity = (message.id, message.content) if message else None
        claim_material = [
            node_id,
            source.created_at.isoformat(),
            [message.id] if message else [question],
        ]
        claim_key = hashlib.sha256(
            json.dumps(claim_material, ensure_ascii=False).encode()
        ).hexdigest()
        if s.get(PreferenceExtraction, claim_key):
            return
        snapshot = preference_snapshot(s, tree_id)
        context = extraction_context(snapshot, question)
        profile = snapshot["profile"]
        reset_revision = profile["reset_revision"] if profile else ""
        s.add(PreferenceExtraction(key=claim_key, node_id=node_id, tree_id=tree_id))
        s.commit()
    # No database lock is held during the model request.
    try:
        result = extract_memories(spec, question, answer, context)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(result, dict):
        return
    preferences, facts = result.get("preferences", []), result.get("facts", [])
    if not isinstance(preferences, list):
        preferences = []
    if not isinstance(facts, list):
        facts = []
    if not preferences and not facts:
        return
    with Session(engine) as s:
        begin_memory_write(s)
        profile = s.get(PreferenceProfile, 1)
        if (profile.reset_revision if profile else "") != reset_revision:
            return
        source = s.get(Node, node_id)
        if (
            not source
            or source.status != "complete"
            or (source.tree_id, source.created_at, source.request_id) != source_identity
            or s.get(KnowledgeTree, source.tree_id) is None
        ):
            return
        if message_identity:
            message = s.get(Message, message_identity[0])
            if not message or (message.id, message.content) != message_identity:
                return
        current = preference_snapshot(s, tree_id)
        if snapshot["state"]["enabled"] and current == snapshot:
            allowed_ids = {row["id"] for row in context["existing"]}
            known = {
                (row["scope"], row["tree_id"], normalize_preference(row["content"]))
                for row in snapshot["supplements"]
            }
            for raw in preferences[:3]:
                pref = validate_preference(raw, question)
                if pref is None:
                    continue
                owner = tree_id if pref["scope"] == "topic" else None
                identity = (pref["scope"], owner, normalize_preference(pref["content"]))
                if identity in known:
                    continue
                if profile and identity[2] == normalize_preference(profile.content):
                    continue
                pending = pref["conflicts_manual"] or context["manual_context_incomplete"]
                target = None
                if pref["action"] == "update":
                    target = (
                        s.get(PreferenceSupplement, pref["replace_id"])
                        if pref["replace_id"] in allowed_ids
                        else None
                    )
                    safe_target = (
                        target is not None
                        and target.status == "active"
                        and not target.user_edited
                        and target.scope == pref["scope"]
                        and target.tree_id == owner
                    )
                    if not safe_target:
                        pending = True
                        target = None
                elif pref["replace_id"] is not None:
                    # Contradictory instructions from the extractor need review.
                    pending = True
                if target is not None and not pending:
                    old_identity = (
                        target.scope,
                        target.tree_id,
                        normalize_preference(target.content),
                    )
                    target.content = pref["content"]
                    target.evidence = pref["evidence"]
                    target.source_node_id = node_id
                    target.source_tree_id = tree_id
                    target.revision = uuid4().hex
                    target.updated_at = datetime.now(timezone.utc)
                    s.add(target)
                    known.discard(old_identity)
                else:
                    s.add(
                        PreferenceSupplement(
                            content=pref["content"],
                            scope=pref["scope"],
                            tree_id=owner,
                            source_node_id=node_id,
                            source_tree_id=tree_id,
                            evidence=pref["evidence"],
                            status="pending" if pending else "active",
                        )
                    )
                known.add(identity)
        # Topic facts continue even when automatic preference learning is off or
        # a concurrent preference edit invalidated the preference snapshot.
        existing = {
            memory.content
            for memory in s.exec(
                select(Memory).where(Memory.kind == "fact", Memory.source_node_id == node_id)
            ).all()
        }
        for fact in facts[:3]:
            if isinstance(fact, str) and fact.strip() and fact.strip() not in existing:
                content = fact.strip()
                s.add(Memory(kind="fact", content=content, tree_id=tree_id, source_node_id=node_id))
                existing.add(content)
        s.commit()
