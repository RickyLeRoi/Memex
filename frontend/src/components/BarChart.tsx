import type { DayStat } from "../types";

const WIDTH = 640;
const HEIGHT = 140;
const PAD = 20;

export function BarChart({ days }: { days: DayStat[] }) {
  if (days.length === 0) return <p className="muted">Nessun import negli ultimi 30 giorni.</p>;
  const max = Math.max(...days.map((d) => d.total), 1);
  const slot = (WIDTH - PAD * 2) / days.length;
  const barWidth = Math.max(Math.min(slot - 4, 28), 4);
  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Documenti importati per giorno" className="chart">
      {days.map((d, i) => {
        const h = ((HEIGHT - PAD * 2) * d.total) / max;
        const x = PAD + i * slot + (slot - barWidth) / 2;
        return (
          <g key={d.date}>
            <title>{`${d.date}: ${d.total}`}</title>
            <rect x={x} y={HEIGHT - PAD - h} width={barWidth} height={h} rx={3} className="bar" />
            {(i === 0 || i === days.length - 1) && (
              <text x={x + barWidth / 2} y={HEIGHT - 4} textAnchor="middle" className="axis">{d.date.slice(5)}</text>
            )}
          </g>
        );
      })}
      <text x={PAD} y={12} className="axis">max {max}</text>
    </svg>
  );
}
