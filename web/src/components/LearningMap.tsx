import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import type { TreeNode } from "../types";
import { groupAncestorIds, groupTopics, layoutTopics, TOPIC_CARD } from "../lib/topicLayout";
import { navigateMapView } from "../lib/mapCamera";
import type { Camera, MapViewAction, ReturnView } from "../lib/mapCamera";
import "./LearningMap.css";
import { useI18n } from "../i18n";

interface Props {
  nodes: TreeNode[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
  onCollapse: () => void;
  width: number;
  treeKey?: string | number;
}

const MIN_ZOOM = 0.1;
const clampZoom = (zoom: number) => Math.max(MIN_ZOOM, Math.min(1.8, zoom));
const toggleSet = (set: Set<number>, id: number) => {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
};

function Icon({ name }: { name: "search" | "close" | "collapse" | "focus" | "fit" | "return" }) {
  const paths = {
    search: <><circle cx="10" cy="10" r="5.5" /><path d="m14.5 14.5 4 4" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    collapse: <path d="m9 5 7 7-7 7" />,
    focus: <><circle cx="12" cy="12" r="4" /><path d="M12 2v3m0 14v3M2 12h3m14 0h3" /></>,
    fit: <path d="M8 4H4v4m12-4h4v4M4 16v4h4m12-4v4h-4" />,
    return: <path d="m8 5-5 5 5 5M3 10h10a6 6 0 0 1 0 12" transform="translate(0 -2)" />,
  };
  return <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

export function LearningMap({ nodes, activeId, onSelect, onCollapse, width, treeKey }: Props) {
  const { locale, t } = useI18n();
  const identity = treeKey ?? nodes[0]?.tree_id ?? "empty";
  const tree = useMemo(() => groupTopics(nodes, t("新问题", "New question")), [nodes, locale]);
  const ancestors = useMemo(() => groupAncestorIds(tree, activeId), [tree, activeId]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const visibleCollapsed = useMemo(() => new Set([...collapsed].filter((id) => !ancestors.has(id))), [collapsed, ancestors]);
  const layout = useMemo(() => layoutTopics(tree, visibleCollapsed, expanded), [tree, visibleCollapsed, expanded]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [camera, setCamera] = useState<Camera>({ x: 0, y: 0, zoom: 1 });
  const [returnView, setReturnView] = useState<ReturnView | null>(null);
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [dragging, setDragging] = useState(false);
  const canvasRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef(camera);
  const returnViewRef = useRef(returnView);
  const layoutRef = useRef(layout);
  const viewportRef = useRef(viewport);
  const activeRef = useRef(activeId);
  const treeRef = useRef(tree);
  const expandedRef = useRef(expanded);
  const fittedTree = useRef<string | number | null>(null);
  const lastSelected = useRef<number | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  cameraRef.current = camera;
  returnViewRef.current = returnView;
  layoutRef.current = layout;
  viewportRef.current = viewport;
  activeRef.current = activeId;
  treeRef.current = tree;
  expandedRef.current = expanded;

  useEffect(() => {
    setCollapsed(new Set());
    setExpanded(new Set());
    setQuery("");
    setSearchOpen(false);
    setReturnView(null);
  }, [identity]);

  useLayoutEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setViewport({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const zoomAt = useCallback((factor: number, x?: number, y?: number) => {
    const point = { x: x ?? viewportRef.current.width / 2, y: y ?? viewportRef.current.height / 2 };
    setCamera((old) => {
      const zoom = clampZoom(old.zoom * factor);
      const ratio = zoom / old.zoom;
      return { zoom, x: point.x - (point.x - old.x) * ratio, y: point.y - (point.y - old.y) * ratio };
    });
  }, []);

  const focusedCamera = useCallback((): Camera | null => {
    const id = activeRef.current;
    const groupId = id == null ? undefined : treeRef.current.nodeToGroup.get(id);
    const item = groupId == null ? undefined : layoutRef.current.byId.get(groupId);
    if (!item) return null;
    const size = viewportRef.current;
    const zoom = Math.max(0.85, Math.min(1.1, cameraRef.current.zoom));
    const roundIndex = item.group.nodes.findIndex((node) => node.id === id);
    const y = item.y + (expandedRef.current.has(item.group.id) && item.group.nodes.length > 1 ? TOPIC_CARD.height + roundIndex * TOPIC_CARD.roundHeight + 20 : TOPIC_CARD.height / 2);
    return { zoom, x: size.width / 2 - (item.x + TOPIC_CARD.width / 2) * zoom, y: size.height / 2 - y * zoom };
  }, []);

  const focusSelected = useCallback(() => {
    const target = focusedCamera();
    if (target) setCamera(target);
    setReturnView(null);
  }, [focusedCamera]);

  const visitView = useCallback((action: MapViewAction, target: Camera) => {
    const next = navigateMapView(cameraRef.current, target, action, returnViewRef.current);
    setCamera(next.camera);
    setReturnView(next.returnView);
  }, []);

  const toggleFocus = useCallback(() => {
    const target = focusedCamera();
    if (target) visitView("focus", target);
  }, [focusedCamera, visitView]);

  const returnPreviousView = useCallback(() => {
    const previous = returnViewRef.current;
    if (!previous) return;
    setCamera(previous.camera);
    setReturnView(null);
  }, []);

  const fitAll = useCallback(() => {
    const current = layoutRef.current;
    const size = viewportRef.current;
    if (!current.items.length) return;
    // Overview may go below the comfortable manual zoom range; focusing a card restores readability.
    const zoom = Math.max(Number.EPSILON, Math.min(1, (size.width - 24) / current.width, (size.height - 32) / current.height));
    visitView("overview", { zoom, x: (size.width - current.width * zoom) / 2, y: Math.max(12, (size.height - current.height * zoom) / 2) });
  }, [visitView]);

  useLayoutEffect(() => {
    if (!viewport.width || !viewport.height || !layout.items.length) return;
    if (fittedTree.current !== identity) {
      fittedTree.current = identity;
      lastSelected.current = activeId;
      const zoom = Math.max(0.8, Math.min(1, (viewport.width - 24) / layout.width));
      const initial = { zoom, x: (viewport.width - layout.width * zoom) / 2, y: 4 };
      const groupId = activeId == null ? undefined : tree.nodeToGroup.get(activeId);
      const selected = groupId == null ? undefined : layout.byId.get(groupId);
      const visible = selected && selected.x * zoom + initial.x >= 12 &&
        (selected.x + TOPIC_CARD.width) * zoom + initial.x <= viewport.width - 12 &&
        selected.y * zoom + initial.y >= 12 &&
        (selected.y + TOPIC_CARD.height) * zoom + initial.y <= viewport.height - 12;
      // Restore the user's current place once, without moving their camera on later answer/status refreshes.
      if (selected && !visible) focusSelected();
      else setCamera(initial);
    } else if (lastSelected.current !== activeId) {
      lastSelected.current = activeId;
      focusSelected();
    }
  }, [identity, layout, viewport, activeId, tree, focusSelected]);

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const rect = element.getBoundingClientRect();
        zoomAt(Math.exp(-event.deltaY * 0.008), event.clientX - rect.left, event.clientY - rect.top);
      } else {
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1;
        setCamera((old) => ({ ...old, x: old.x - event.deltaX * unit, y: old.y - event.deltaY * unit }));
      }
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button, input, a")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    setDragging(true);
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const before = pointers.current.get(event.pointerId);
    if (!before) return;
    const after = { x: event.clientX, y: event.clientY };
    const other = [...pointers.current.entries()].find(([id]) => id !== event.pointerId)?.[1];
    if (other) {
      const oldDistance = Math.hypot(before.x - other.x, before.y - other.y);
      const newDistance = Math.hypot(after.x - other.x, after.y - other.y);
      const rect = event.currentTarget.getBoundingClientRect();
      if (oldDistance > 0) zoomAt(newDistance / oldDistance, (after.x + other.x) / 2 - rect.left, (after.y + other.y) / 2 - rect.top);
    } else {
      setCamera((old) => ({ ...old, x: old.x + after.x - before.x, y: old.y + after.y - before.y }));
    }
    pointers.current.set(event.pointerId, after);
  };
  const stopPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    if (!pointers.current.size) setDragging(false);
  };
  const selectResult = (node: TreeNode) => {
    const groupId = tree.nodeToGroup.get(node.id);
    if (groupId != null) setExpanded((old) => new Set(old).add(groupId));
    setQuery("");
    setSearchOpen(false);
    onSelect(node.id);
    if (node.id === activeId) requestAnimationFrame(focusSelected);
  };
  const results = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return needle ? nodes.filter((node) => `${node.title}\n${node.seed_text ?? ""}`.toLocaleLowerCase().includes(needle)) : [];
  }, [nodes, query]);
  const activeGroupId = activeId == null ? undefined : tree.nodeToGroup.get(activeId);
  const nodeStyle = (x: number, y: number, height: number): CSSProperties => ({ left: x, top: y, width: TOPIC_CARD.width, height });

  return (
    <aside className="right learning-map" style={{ width }} aria-label={t("学习树", "LearningTree")} onKeyDown={(event) => {
      if (event.key === "Escape" && returnView && !(event.target as HTMLElement).closest("input")) {
        event.preventDefault();
        returnPreviousView();
      }
    }}>
      <div className="lm-header">
        <span className="lm-heading">{t("学习树", "LearningTree")} <span>{tree.groups.length}</span></span>
        <div className="lm-header-actions">
          <button type="button" className={searchOpen ? "lm-icon active" : "lm-icon"} aria-label={searchOpen ? t("关闭节点搜索", "Close node search") : t("搜索节点", "Search nodes")} aria-expanded={searchOpen} title={t("搜索节点", "Search nodes")} onClick={() => { setSearchOpen((old) => !old); setQuery(""); }}><Icon name="search" /></button>
          <button type="button" className="lm-icon" onClick={onCollapse} title={t("收起学习树", "Hide learning tree")} aria-label={t("收起学习树", "Hide learning tree")}><Icon name="collapse" /></button>
        </div>
      </div>
      {searchOpen && <div className="lm-search">
        <div className="lm-search-field"><Icon name="search" /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") { setSearchOpen(false); setQuery(""); } }} placeholder={t("找一个问题", "Find a question")} aria-label={t("搜索问题或引用", "Search questions or quotes")} />{query && <button type="button" className="lm-icon" aria-label={t("清空搜索", "Clear search")} onClick={() => setQuery("")}><Icon name="close" /></button>}</div>
        {query.trim() && <div className="lm-results" aria-label={t("搜索结果", "Search results")}><span className="lm-result-count" role="status">{results.length ? t("{count} 个结果", "{count} results", { count: results.length }) : t("没有找到相关问题", "No matching questions")}</span>{results.slice(0, 30).map((node) => <button type="button" key={node.id} onClick={() => selectResult(node)}><span>{node.title || node.seed_text || t("新问题", "New question")}</span></button>)}</div>}
      </div>}
      <div className={`lm-viewport${dragging ? " dragging" : ""}`} ref={canvasRef} tabIndex={0} role="region" aria-label={t("学习树画布，方向键平移，加减键缩放，Home 键总览，Escape 键返回原视图", "Learning tree canvas. Arrow keys pan; plus and minus zoom; Home shows the overview; Escape restores the previous view.")} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={stopPointer} onPointerCancel={stopPointer} onLostPointerCapture={stopPointer} onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        const moves: Record<string, [number, number]> = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
        if (moves[event.key]) { event.preventDefault(); const [x, y] = moves[event.key]; setCamera((old) => ({ ...old, x: old.x + x, y: old.y + y })); }
        else if (event.key === "+" || event.key === "=") { event.preventDefault(); zoomAt(1.15); }
        else if (event.key === "-") { event.preventDefault(); zoomAt(1 / 1.15); }
        else if (event.key === "Home") { event.preventDefault(); fitAll(); }
      }}>
        {!layout.items.length ? <div className="lm-empty">{t("从一个问题开始。", "Start with a question.")}</div> : <div className="lm-world" style={{ width: layout.width, height: layout.height, transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})` }}>
          <svg className="lm-connections" width={layout.width} height={layout.height} aria-hidden="true">
            {layout.edges.map(({ from, to }) => {
              const x = from.x + 12;
              const endY = to.y + 32;
              const onPath = (ancestors.has(from.group.id) || from.group.id === activeGroupId) && (ancestors.has(to.group.id) || to.group.id === activeGroupId);
              return <path key={to.group.id} className={onPath ? "current" : ""} d={`M ${x} ${from.y + from.height} V ${endY - 7} Q ${x} ${endY} ${x + 7} ${endY} H ${to.x}`} />;
            })}
          </svg>
          {layout.items.map(({ group, x, y, height }) => {
            const current = group.id === activeGroupId;
            const isExpanded = expanded.has(group.id);
            const activeRound = group.nodes.findIndex((node) => node.id === activeId);
            const last = group.nodes[group.nodes.length - 1];
            const failing = group.nodes.some((node) => ["error", "failed", "interrupted"].includes((node as TreeNode & { status?: string }).status ?? ""));
            const continuing = group.nodes.some((node) => ["pending", "streaming"].includes((node as TreeNode & { status?: string }).status ?? ""));
            return <section key={group.id} className={`lm-card${current ? " current" : ancestors.has(group.id) ? " ancestor" : ""}`} style={nodeStyle(x, y, height)} aria-label={group.title}>
              <button type="button" className="lm-topic" onClick={() => onSelect(current && activeId != null ? activeId : last.id)} aria-current={current ? "location" : undefined} title={group.title}>
                <span className="lm-node-mark" aria-hidden="true" />
                <span className="lm-title">{group.title}</span>
              </button>
              <div className="lm-card-meta">
                {group.nodes.length > 1 ? <button type="button" className="lm-round-toggle" onClick={() => setExpanded((old) => toggleSet(old, group.id))} aria-expanded={isExpanded} aria-label={t("{action}{title}的 {count} 轮问答", "{action} {count} turns in {title}", { action: isExpanded ? t("收起", "Collapse") : t("展开", "Expand"), title: group.title, count: group.nodes.length })}>{current && activeRound >= 0 && !isExpanded ? t("{current} / {count} 轮", "{current} / {count} turns", { current: activeRound + 1, count: group.nodes.length }) : t("{count} 轮", "{count} turns", { count: group.nodes.length })} <span aria-hidden="true">{isExpanded ? "⌃" : "⌄"}</span></button> : <span>{(group.nodes[0] as TreeNode & { kind?: string }).kind === "revision" ? t("修改版本", "Revision") : group.nodes[0].seed_text ? t("追问", "Branch") : t("1 轮", "1 turn")}</span>}
                <span className="lm-card-state">{continuing ? <span className="lm-pending" title={t("正在回答", "Responding")} aria-label={t("正在回答", "Responding")}>···</span> : failing ? <span className="lm-error-dot" title={t("有回答需要重试", "A response needs retrying")} aria-label={t("有回答需要重试", "A response needs retrying")} /> : current ? <span className="lm-current-label">{t("当前", "Current")}</span> : null}</span>
                {group.children.length > 0 && <button type="button" className="lm-fold" onClick={() => setCollapsed((old) => toggleSet(old, group.id))} disabled={ancestors.has(group.id)} aria-label={ancestors.has(group.id) ? t("当前路径保持展开", "The current path stays expanded") : visibleCollapsed.has(group.id) ? t("展开子分支", "Expand child branches") : t("收起子分支", "Collapse child branches")} aria-expanded={!visibleCollapsed.has(group.id)} title={ancestors.has(group.id) ? t("当前路径保持展开", "The current path stays expanded") : visibleCollapsed.has(group.id) ? t("展开子分支", "Expand child branches") : t("收起子分支", "Collapse child branches")}>{visibleCollapsed.has(group.id) ? `+${group.children.length}` : "−"}</button>}
              </div>
              {isExpanded && group.nodes.length > 1 && <div className="lm-rounds">{group.nodes.map((node, index) => <button type="button" key={node.id} className={node.id === activeId ? "selected" : ""} onClick={() => onSelect(node.id)} aria-current={node.id === activeId ? "location" : undefined} title={node.title}><span>{index + 1}</span><span>{node.title || t("新问题", "New question")}</span></button>)}</div>}
            </section>;
          })}
        </div>}
      </div>
      <div className="lm-toolbar">
        <div className="lm-zoom"><button type="button" onClick={() => zoomAt(1 / 1.15)} disabled={camera.zoom <= MIN_ZOOM} aria-label={t("缩小学习树", "Zoom out learning tree")} title={t("缩小", "Zoom out")}>−</button><button type="button" className="lm-percent" onClick={() => zoomAt(1 / camera.zoom)} aria-label={t("恢复到百分之百缩放", "Reset zoom to 100 percent")} title={t("恢复到 100%", "Reset to 100%")}>{camera.zoom < 0.01 ? (camera.zoom * 100).toFixed(1) : Math.round(camera.zoom * 100)}%</button><button type="button" onClick={() => zoomAt(1.15)} disabled={camera.zoom >= 1.8} aria-label={t("放大学习树", "Zoom in learning tree")} title={t("放大", "Zoom in")}>＋</button></div>
        <div className="lm-tools"><button type="button" className={`lm-icon${returnView?.action === "focus" ? " active" : ""}`} onClick={toggleFocus} disabled={activeId == null} aria-label={returnView?.action === "focus" ? t("返回定位前的视图", "Return to the view before focusing") : t("定位当前问题", "Focus current question")} title={returnView?.action === "focus" ? t("返回原视图（Esc）", "Return to previous view (Esc)") : t("定位当前问题", "Focus current question")}><Icon name={returnView?.action === "focus" ? "return" : "focus"} /></button><button type="button" className={`lm-icon${returnView?.action === "overview" ? " active" : ""}`} onClick={fitAll} disabled={!nodes.length} aria-label={returnView?.action === "overview" ? t("返回总览前的视图", "Return to the view before overview") : t("查看整棵学习树", "View the whole learning tree")} title={returnView?.action === "overview" ? t("返回原视图（Esc）", "Return to previous view (Esc)") : t("查看全貌", "Overview")}><Icon name={returnView?.action === "overview" ? "return" : "fit"} /></button></div>
      </div>
    </aside>
  );
}
