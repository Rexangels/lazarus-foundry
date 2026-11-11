import { cx } from "@/components/ui/cx";
import { formatScore } from "@/lib/defibrillator";
import { COLOR_TOKEN_HEX, COLOR_TOKEN_TEXT_HEX } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

const SWEEP = 270; // degrees of the dial
const R = 52;
const CX = 64;
const CY = 64;

function polar(angle: number): { x: number; y: number } {
  const rad = ((angle - 90) * Math.PI) / 180;
  return { x: CX + R * Math.cos(rad), y: CY + R * Math.sin(rad) };
}

function arc(from: number, to: number): string {
  const start = polar(from);
  const end = polar(to);
  const large = to - from > 180 ? 1 : 0;
  return `M ${start.x.toFixed(3)} ${start.y.toFixed(3)} A ${R} ${R} 0 ${large} 1 ${end.x.toFixed(3)} ${end.y.toFixed(3)}`;
}

/** Charge bands of the dial: a reading, not a verdict (the formula decides). */
export function gaugeToken(score: number): ColorToken {
  if (score >= 65) return "alive";
  if (score >= 45) return "dormant";
  return "ghost";
}

/**
 * Score(S_k) as a defibrillator charge dial (0–100). The number is the backend's value, rounded as
 * the backend rounds it; the arc is decoration and the value is also exposed as a meter.
 */
export function ScoreGauge({
  score,
  decimals = 1,
  label,
  size = "lg",
  dim = false,
}: {
  score: number;
  decimals?: number;
  label: string;
  size?: "md" | "lg";
  dim?: boolean;
}) {
  const clamped = Math.max(0, Math.min(100, score));
  const start = -SWEEP / 2;
  const end = start + (SWEEP * clamped) / 100;
  const token = dim ? "neutral" : gaugeToken(clamped);
  const color = COLOR_TOKEN_HEX[token];
  const text = formatScore(score, decimals);
  const ticks = Array.from({ length: 11 }, (_, i) => start + (SWEEP * i) / 10);
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={score}
      aria-valuetext={`${text} of 100`}
      aria-label={label}
      className={cx("relative shrink-0", size === "lg" ? "h-36 w-36" : "h-24 w-24")}
    >
      <svg viewBox="0 0 128 128" className="h-full w-full overflow-visible" aria-hidden>
        <defs>
          <filter id={`gauge-glow-${token}`} x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="2.5" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        <path d={arc(start, start + SWEEP)} fill="none" stroke="#222C3A" strokeWidth={9} strokeLinecap="round" />
        {ticks.map((angle, i) => {
          const inner = { x: CX + (R - 12) * Math.cos(((angle - 90) * Math.PI) / 180), y: CY + (R - 12) * Math.sin(((angle - 90) * Math.PI) / 180) };
          const outer = { x: CX + (R - 7) * Math.cos(((angle - 90) * Math.PI) / 180), y: CY + (R - 7) * Math.sin(((angle - 90) * Math.PI) / 180) };
          return (
            <line
              key={i}
              x1={inner.x}
              y1={inner.y}
              x2={outer.x}
              y2={outer.y}
              stroke="#94A3B8"
              strokeOpacity={i % 5 === 0 ? 0.6 : 0.25}
              strokeWidth={i % 5 === 0 ? 1.5 : 1}
            />
          );
        })}
        {clamped > 0 && (
          <path
            d={arc(start, end)}
            fill="none"
            stroke={color}
            strokeWidth={9}
            strokeLinecap="round"
            filter={dim ? undefined : `url(#gauge-glow-${token})`}
          />
        )}
        <text
          x={CX}
          y={CY + 4}
          textAnchor="middle"
          className="font-mono"
          fontSize={size === "lg" ? 26 : 28}
          fontWeight={600}
          fill={dim ? "#94A3B8" : "#E6EDF3"}
          style={{ fontVariantNumeric: "tabular-nums" }}
        >
          {text}
        </text>
        <text x={CX} y={CY + 22} textAnchor="middle" className="font-mono" fontSize={9} fill={COLOR_TOKEN_TEXT_HEX[token]} letterSpacing={1.5}>
          / 100
        </text>
        <text x={CX} y={122} textAnchor="middle" className="font-mono" fontSize={8.5} fill="#94A3B8" letterSpacing={2}>
          SCORE(S_k)
        </text>
      </svg>
    </div>
  );
}
