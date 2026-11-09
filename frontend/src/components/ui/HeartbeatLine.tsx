import { COLOR_TOKEN_HEX } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

import { cx } from "./cx";

const WIDTH = 600;
const BASELINE = 60;

/** One P-QRS-T complex, 200 units wide, starting at x. */
function beat(x: number, amplitude: number): string {
  const a = amplitude;
  return [
    `L${x + 38} ${BASELINE}`,
    `Q${x + 48} ${BASELINE - 9 * a} ${x + 58} ${BASELINE}`,
    `L${x + 80} ${BASELINE}`,
    `L${x + 86} ${BASELINE + 7 * a}`,
    `L${x + 93} ${BASELINE - 48 * a}`,
    `L${x + 100} ${BASELINE + 16 * a}`,
    `L${x + 106} ${BASELINE}`,
    `L${x + 128} ${BASELINE}`,
    `Q${x + 145} ${BASELINE - 14 * a} ${x + 162} ${BASELINE}`,
    `L${x + 200} ${BASELINE}`,
  ].join(" ");
}

export const ECG_PATH = `M0 ${BASELINE} ${beat(0, 1)} ${beat(200, 1)} ${beat(400, 1)}`;
export const FLATLINE_PATH = `M0 ${BASELINE} L${WIDTH} ${BASELINE}`;

export interface HeartbeatLineProps {
  /** `flat`: no vital signs. `pulse`: a live, tracing ECG. */
  state: "flat" | "pulse";
  token?: ColorToken;
  className?: string;
  /** Accessible description; the line is decorative when omitted. */
  label?: string;
  /** Seconds per sweep while pulsing. */
  period?: number;
}

/**
 * The signature ECG trace. It stretches to its container's width. With reduced motion the
 * trace stops, and the dim full waveform still shows the state.
 */
export function HeartbeatLine({ state, token = "alive", className, label, period = 2.4 }: HeartbeatLineProps) {
  const color = COLOR_TOKEN_HEX[token];
  const path = state === "pulse" ? ECG_PATH : FLATLINE_PATH;
  return (
    <svg
      viewBox={`0 0 ${WIDTH} 100`}
      preserveAspectRatio="none"
      className={cx("block h-16 w-full overflow-visible", className)}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <defs>
        <filter id={`ecg-glow-${token}`} x="-10%" y="-50%" width="120%" height="200%">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeOpacity={state === "pulse" ? 0.45 : 0.6}
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
        strokeLinejoin="round"
        className={state === "flat" ? "animate-flatline-blink" : undefined}
      />
      {state === "pulse" && (
        <path
          d={path}
          fill="none"
          stroke={color}
          strokeWidth={2.5}
          vectorEffect="non-scaling-stroke"
          strokeLinejoin="round"
          strokeLinecap="round"
          pathLength={1200}
          strokeDasharray="160 1040"
          filter={`url(#ecg-glow-${token})`}
          className="animate-ecg-trace"
          style={{ animationDuration: `${period}s` }}
        />
      )}
    </svg>
  );
}
