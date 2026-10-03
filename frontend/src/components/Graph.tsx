import { useCallback, useEffect, useRef } from "react";
import { onThemeChange } from "../theme";
import type { GraphData, GraphNode } from "../types";
import { createSimulation, step, type SimNode } from "./forceLayout";

const PALETTE = ["#6aa9ff", "#f2a65a", "#7bd88f", "#d98cf0", "#f2d45a", "#ff7b7b", "#5ad6d6", "#b0b8c4"];
const SETTLED_ENERGY = 0.05;
const MIN_ALPHA = 0.02;
const MIN_SCALE = 0.2;
const MAX_SCALE = 4;
const FIT_PADDING = 120;
const FIT_EASING = 0.18;
const HIT_RADIUS_MOUSE = 12;
const HIT_RADIUS_TOUCH = 24;

interface View {
  scale: number;
  offsetX: number;
  offsetY: number;
}

interface Point {
  x: number;
  y: number;
}

export function sourceColor(source: string, sources: string[]): string {
  return PALETTE[Math.max(sources.indexOf(source), 0) % PALETTE.length];
}

const clampScale = (scale: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));

interface Props {
  data: GraphData;
  selectedId: string | null;
  onSelect: (node: GraphNode | null) => void;
  colorOf: (node: GraphNode) => string;
  islands: boolean;
}

export function Graph({ data, selectedId, onSelect, colorOf, islands }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const simRef = useRef<Map<string, SimNode>>(new Map());
  const viewRef = useRef<View>({ scale: 1, offsetX: 0, offsetY: 0 });
  const alphaRef = useRef(1);
  const frameRef = useRef(0);
  const autoFitRef = useRef(true);
  const pointersRef = useRef<Map<number, Point>>(new Map());
  const pinchRef = useRef<{ distance: number; mid: Point } | null>(null);
  const dragRef = useRef<{ node: SimNode | null; panning: boolean; moved: boolean; lastX: number; lastY: number; touch: boolean } | null>(null);
  const pullRef = useRef<Map<string, { x: number; y: number }> | undefined>(undefined);
  const propsRef = useRef({ data, selectedId, onSelect, colorOf });
  propsRef.current = { data, selectedId, onSelect, colorOf };

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const { data: graph, selectedId: selected } = propsRef.current;
    const styles = getComputedStyle(canvas);
    const { scale, offsetX, offsetY } = viewRef.current;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    ctx.translate(canvas.clientWidth / 2 + offsetX, canvas.clientHeight / 2 + offsetY);
    ctx.scale(scale, scale);

    for (const edge of graph.edges) {
      const a = simRef.current.get(edge.source);
      const b = simRef.current.get(edge.target);
      if (!a || !b) continue;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.setLineDash(edge.type === "domain" ? [4, 4] : []);
      ctx.strokeStyle = edge.type === "tag" ? styles.getPropertyValue("--accent") : styles.getPropertyValue("--edge");
      ctx.lineWidth = (edge.type === "tag" ? 1.8 : 1) / scale;
      ctx.stroke();
    }
    ctx.setLineDash([]);

    ctx.lineJoin = "round";
    for (const node of graph.nodes) {
      const pos = simRef.current.get(node.id);
      if (!pos) continue;
      const radius = node.type === "doc" ? 8 : 4;
      ctx.beginPath();
      ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = propsRef.current.colorOf(node);
      ctx.fill();
      if (node.id === selected) {
        ctx.lineWidth = 2.5 / scale;
        ctx.strokeStyle = styles.getPropertyValue("--text");
        ctx.stroke();
      }
      if (node.type === "doc" && scale > 0.6) {
        const label = node.label.slice(0, 28);
        ctx.font = `${12 / scale}px system-ui, sans-serif`;
        ctx.lineWidth = 3 / scale;
        ctx.strokeStyle = styles.getPropertyValue("--graph-bg");
        ctx.strokeText(label, pos.x + radius + 3, pos.y + 4);
        ctx.fillStyle = styles.getPropertyValue("--text");
        ctx.fillText(label, pos.x + radius + 3, pos.y + 4);
      }
    }
  }, []);

  const fitTarget = useCallback((): View | null => {
    const canvas = canvasRef.current;
    const nodes = [...simRef.current.values()];
    if (!canvas || nodes.length === 0 || canvas.clientWidth === 0) return null;
    const xs = nodes.map((n) => n.x);
    const ys = nodes.map((n) => n.y);
    const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
    const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];
    const scale = clampScale(Math.min(
      canvas.clientWidth / (maxX - minX + FIT_PADDING),
      canvas.clientHeight / (maxY - minY + FIT_PADDING),
      1.6,
    ));
    return { scale, offsetX: -((minX + maxX) / 2) * scale, offsetY: -((minY + maxY) / 2) * scale };
  }, []);

  const easeTowardFit = useCallback((snap: boolean) => {
    const target = fitTarget();
    if (!target) return;
    const view = viewRef.current;
    const k = snap ? 1 : FIT_EASING;
    view.scale += (target.scale - view.scale) * k;
    view.offsetX += (target.offsetX - view.offsetX) * k;
    view.offsetY += (target.offsetY - view.offsetY) * k;
  }, [fitTarget]);

  const loop = useCallback(() => {
    const { data: graph } = propsRef.current;
    const energy = step(simRef.current, graph.edges, alphaRef.current, pullRef.current);
    alphaRef.current = Math.max(MIN_ALPHA, alphaRef.current * 0.985);
    const dragging = dragRef.current?.node != null;
    const keepRunning = energy > SETTLED_ENERGY || dragging || alphaRef.current > MIN_ALPHA * 4;
    if (autoFitRef.current) easeTowardFit(!keepRunning);
    draw();
    frameRef.current = keepRunning ? requestAnimationFrame(loop) : 0;
  }, [draw, easeTowardFit]);

  const wake = useCallback(() => {
    alphaRef.current = Math.max(alphaRef.current, 0.5);
    if (!frameRef.current) frameRef.current = requestAnimationFrame(loop);
  }, [loop]);

  useEffect(() => {
    simRef.current = createSimulation(data.nodes.map((n) => n.id), simRef.current);
    if (islands) {  // one island per area, on a circle: nodes of the same area drift together (no edges are created)
      const groups = [...new Set(data.nodes.map((n) => n.area))].sort();
      const radius = 60 + groups.length * 28;
      const centers = new Map(groups.map((g, i) => [g, { x: Math.cos((i / groups.length) * Math.PI * 2) * radius,
                                                          y: Math.sin((i / groups.length) * Math.PI * 2) * radius }]));
      pullRef.current = new Map(data.nodes.map((n) => [n.id, centers.get(n.area)!]));
    } else {
      pullRef.current = undefined;
    }
    alphaRef.current = 1;
    wake();
  }, [data, islands, wake]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = canvas.clientWidth * dpr;
      canvas.height = canvas.clientHeight * dpr;
      if (autoFitRef.current) easeTowardFit(true);
      draw();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
    };
  }, [draw, easeTowardFit]);

  useEffect(() => draw(), [selectedId, draw]);

  // 20261003 ++ RG #theme_toggle: canvas colors come from CSS variables, so repaint when the theme flips
  useEffect(() => onThemeChange(draw), [draw]);

  const zoomAt = useCallback((clientX: number, clientY: number, factor: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const view = viewRef.current;
    const cx = clientX - rect.left - rect.width / 2;
    const cy = clientY - rect.top - rect.height / 2;
    const scale = clampScale(view.scale * factor);
    const ratio = scale / view.scale;
    view.offsetX = cx - (cx - view.offsetX) * ratio;
    view.offsetY = cy - (cy - view.offsetY) * ratio;
    view.scale = scale;
    draw();
  }, [draw]);

  const zoomFromCenter = (factor: number) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    autoFitRef.current = false;
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  };

  const refit = () => {
    autoFitRef.current = true;
    wake();
  };

  // 20261003 ** RG #page_scroll_while_zooming: React wheel handlers are passive, so preventDefault needs a native listener
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      autoFitRef.current = false;
      const delta = Math.max(-100, Math.min(100, e.deltaY));
      zoomAt(e.clientX, e.clientY, Math.exp(-delta * 0.0015));
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const toWorld = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const { scale, offsetX, offsetY } = viewRef.current;
    return {
      x: (clientX - rect.left - rect.width / 2 - offsetX) / scale,
      y: (clientY - rect.top - rect.height / 2 - offsetY) / scale,
    };
  };

  const hit = (clientX: number, clientY: number, touch: boolean): SimNode | null => {
    const { x, y } = toWorld(clientX, clientY);
    const radius = (touch ? HIT_RADIUS_TOUCH : HIT_RADIUS_MOUSE) / viewRef.current.scale;
    let best: SimNode | null = null;
    let bestDist = radius * radius;
    for (const node of simRef.current.values()) {
      const d2 = (node.x - x) ** 2 + (node.y - y) ** 2;
      if (d2 <= bestDist) {
        best = node;
        bestDist = d2;
      }
    }
    return best;
  };

  const pinchMetrics = () => {
    const [a, b] = [...pointersRef.current.values()];
    return { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } };
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    autoFitRef.current = false;
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointersRef.current.size === 2) {
      if (dragRef.current?.node) dragRef.current.node.fixed = false;
      dragRef.current = null;
      pinchRef.current = pinchMetrics();
      return;
    }
    const touch = e.pointerType === "touch";
    const node = hit(e.clientX, e.clientY, touch);
    if (node) node.fixed = true;
    dragRef.current = { node, panning: !node, moved: false, lastX: e.clientX, lastY: e.clientY, touch };
    if (node) wake();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (pointersRef.current.has(e.pointerId)) pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const pinch = pinchRef.current;
    if (pinch && pointersRef.current.size === 2) {
      const next = pinchMetrics();
      zoomAt(next.mid.x, next.mid.y, next.distance / pinch.distance);
      viewRef.current.offsetX += next.mid.x - pinch.mid.x;
      viewRef.current.offsetY += next.mid.y - pinch.mid.y;
      pinchRef.current = next;
      draw();
      return;
    }
    const drag = dragRef.current;
    if (!drag) return;
    drag.moved ||= Math.abs(e.clientX - drag.lastX) + Math.abs(e.clientY - drag.lastY) > 3;
    if (drag.node) {
      const { x, y } = toWorld(e.clientX, e.clientY);
      drag.node.x = x;
      drag.node.y = y;
      wake();
    } else if (drag.panning) {
      viewRef.current.offsetX += e.clientX - drag.lastX;
      viewRef.current.offsetY += e.clientY - drag.lastY;
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      draw();
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    pointersRef.current.delete(e.pointerId);
    if (pinchRef.current) {
      pinchRef.current = null;
      const [rest] = [...pointersRef.current.values()];
      dragRef.current = rest ? { node: null, panning: true, moved: true, lastX: rest.x, lastY: rest.y, touch: true } : null;
      return;
    }
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag) return;
    if (drag.node) drag.node.fixed = false;
    if (!drag.moved) {
      const found = drag.node ? propsRef.current.data.nodes.find((n) => n.id === drag.node!.id) ?? null : null;
      propsRef.current.onSelect(found);
    }
  };

  return (
    <div className="graph-shell">
      <canvas
        ref={canvasRef}
        className="graph"
        role="img"
        aria-label="Grafo dei documenti"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      />
      <div className="graph-zoom" role="group" aria-label="Zoom del grafo">
        <button onClick={() => zoomFromCenter(1.3)} aria-label="Ingrandisci">+</button>
        <button onClick={() => zoomFromCenter(1 / 1.3)} aria-label="Riduci">−</button>
        <button onClick={refit} aria-label="Adatta alla vista" title="Adatta alla vista">⤢</button>
      </div>
    </div>
  );
}
