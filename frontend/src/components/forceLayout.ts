export interface SimNode {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  fixed: boolean;
}

export interface SimEdge {
  source: string;
  target: string;
}

const REPULSION = 2600;
const SPRING_LENGTH = 70;
const SPRING_STRENGTH = 0.04;
const GRAVITY = 0.012;
const DAMPING = 0.82;
const MAX_SPEED = 12;
const ISLAND_PULL = 0.08;

export function createSimulation(ids: string[], previous?: Map<string, SimNode>): Map<string, SimNode> {
  const nodes = new Map<string, SimNode>();
  const radius = 40 + Math.sqrt(ids.length) * 14;
  ids.forEach((id, i) => {
    const old = previous?.get(id);
    const angle = (i / Math.max(ids.length, 1)) * Math.PI * 2;
    nodes.set(id, old ?? {
      id,
      x: Math.cos(angle) * radius + (Math.random() - 0.5) * 8,
      y: Math.sin(angle) * radius + (Math.random() - 0.5) * 8,
      vx: 0,
      vy: 0,
      fixed: false,
    });
  });
  return nodes;
}

/** One integration step. Returns the total kinetic energy so the caller can stop when settled. */
export function step(nodes: Map<string, SimNode>, edges: SimEdge[], alpha: number,
                     pull?: Map<string, { x: number; y: number }>): number {
  const list = [...nodes.values()];
  for (let i = 0; i < list.length; i++) {
    const a = list[i];
    for (let j = i + 1; j < list.length; j++) {
      const b = list[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 0.01) {
        dx = Math.random() - 0.5;
        dy = Math.random() - 0.5;
        d2 = dx * dx + dy * dy + 0.01;
      }
      const force = (REPULSION * alpha) / d2;
      const dist = Math.sqrt(d2);
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    const target = pull?.get(a.id);
    // 20261004 ** RG #islands_hold the island pull must not fade with alpha, and global gravity would undo it
    if (target) {
      a.vx += (target.x - a.x) * ISLAND_PULL;
      a.vy += (target.y - a.y) * ISLAND_PULL;
    } else {
      a.vx -= a.x * GRAVITY;
      a.vy -= a.y * GRAVITY;
    }
  }
  for (const edge of edges) {
    const a = nodes.get(edge.source);
    const b = nodes.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const pull = (dist - SPRING_LENGTH) * SPRING_STRENGTH;
    const fx = (dx / dist) * pull;
    const fy = (dy / dist) * pull;
    a.vx += fx;
    a.vy += fy;
    b.vx -= fx;
    b.vy -= fy;
  }
  let energy = 0;
  for (const n of list) {
    if (n.fixed) {
      n.vx = 0;
      n.vy = 0;
      continue;
    }
    n.vx = Math.max(-MAX_SPEED, Math.min(MAX_SPEED, n.vx * DAMPING));
    n.vy = Math.max(-MAX_SPEED, Math.min(MAX_SPEED, n.vy * DAMPING));
    n.x += n.vx;
    n.y += n.vy;
    energy += n.vx * n.vx + n.vy * n.vy;
  }
  return energy;
}
