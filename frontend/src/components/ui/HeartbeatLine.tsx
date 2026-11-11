import { COLOR_TOKEN_HEX } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

import { cx } from "./cx";

const WIDTH = 600;
const BASELINE = 60;

/** One P-QRS-T complex, `width` units wide (200 by default), starting at x. */
function beat(x: number, amplitude: number, width = 200): string {
  const a = amplitude;
  const s = width / 200;
  const at = (dx: number) => Math.round((x + dx * s) * 10) / 10;
  return [
    `L${at(38)} ${BASELINE}`,
    `Q${at(48)} ${BASELINE - 9 * a} ${at(58)} ${BASELINE}`,
    `L${at(80)} ${BASELINE}`,
    `L${at(86)} ${BASELINE + 7 * a}`,
    `L${at(93)} ${BASELINE - 48 * a}`,
    `L${at(100)} ${BASELINE + 16 * a}`,
    `L${at(106)} ${BASELINE}`,
    `L${at(128)} ${BASELINE}`,
    `Q${at(145)} ${BASELINE - 14 * a} ${at(162)} ${BASELINE}`,
    `L${at(200)} ${BASELINE}`,
  ].join(" ");
}

export const ECG_PATH = `M0 ${BASELINE} ${beat(0, 1)} ${beat(200, 1)} ${beat(400, 1)}`;
export const FLATLINE_PATH = `M0 ${BASELINE} L${WIDTH} ${BASELINE}`;
/** An irregular rhythm: uneven spacing, a weak beat, a pause and an oversized one (a failing suite). */
export const ERRATIC_PATH = [
  `M0 ${BASELINE}`,
  beat(0, 0.9, 150),
  beat(150, 0.35, 80),
  `L262 ${BASELINE} L268 ${BASELINE - 4} L274 ${BASELINE + 3} L300 ${BASELINE}`,
  beat(300, 1.3, 110),
  beat(410, 0.5, 70),
  beat(480, 0.8, 120),
].join(" ");

const PATHS = { flat: FLATLINE_PATH, pulse: ECG_PATH, erratic: ERRATIC_PATH } as const;

export interface HeartbeatLineProps {
  /** `flat`: no vital signs. `pulse`: a live, tracing ECG. `erratic`: an irregular, failing rhythm. */
  state: "flat" | "pulse" | "erratic";
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
  const path = PATHS[state];
  const tracing = state !== "flat";
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
        strokeOpacity={tracing ? 0.45 : 0.6}
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
        strokeLinejoin="round"
        className={state === "flat" ? "animate-flatline-blink" : undefined}
      />
      {tracing && (
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
