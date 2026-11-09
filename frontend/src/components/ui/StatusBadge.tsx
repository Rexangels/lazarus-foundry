import {
  EPISTEMIC_STATUS_STYLE,
  VITAL_STATUS_GLYPH,
  VITAL_STATUS_LABEL,
  VITAL_STATUS_TOKEN,
  colorTokenClasses,
} from "@/lib/palette";
import type { ColorToken, EpistemicStatus, ProjectStatus, VitalStatus } from "@/lib/types";

import { cx } from "./cx";

type Size = "sm" | "md";

const SIZE: Record<Size, string> = {
  sm: "h-5 px-1.5 text-[10px] gap-1",
  md: "h-6 px-2 text-[11px] gap-1.5",
};

export interface TokenBadgeProps {
  token: ColorToken;
  label: string;
  glyph?: string;
  /** Filled chips are vital (life) signals; outlined chips are knowledge signals. */
  appearance?: "filled" | "outlined";
  dashed?: boolean;
  pulse?: boolean;
  size?: Size;
  title?: string;
  className?: string;
}

export function TokenBadge({
  token,
  label,
  glyph,
  appearance = "filled",
  dashed = false,
  pulse = false,
  size = "md",
  title,
  className,
}: TokenBadgeProps) {
  const classes = colorTokenClasses(token);
  return (
    <span
      title={title}
      className={cx(
        "inline-flex shrink-0 items-center whitespace-nowrap rounded font-mono font-medium uppercase tracking-wider",
        SIZE[size],
        classes.text,
        appearance === "filled" ? cx(classes.bg, "border", classes.border) : cx("border bg-transparent", classes.border),
        dashed && "border-dashed",
        className,
      )}
    >
      {glyph && (
        <span aria-hidden className={cx("leading-none", pulse && "motion-safe:animate-vital-pulse")}>
          {glyph}
        </span>
      )}
      {label}
    </span>
  );
}

export function VitalBadge({ status, size, className }: { status: VitalStatus; size?: Size; className?: string }) {
  return (
    <TokenBadge
      token={VITAL_STATUS_TOKEN[status]}
      label={VITAL_STATUS_LABEL[status]}
      glyph={VITAL_STATUS_GLYPH[status]}
      pulse={status === "VERIFIED_WORKING"}
      size={size}
      title={`Vital status: ${status}`}
      className={className}
    />
  );
}

export function EpistemicBadge({ status, size, className }: { status: EpistemicStatus; size?: Size; className?: string }) {
  const style = EPISTEMIC_STATUS_STYLE[status];
  return (
    <TokenBadge
      token={style.token}
      label={style.label}
      glyph={style.glyph}
      appearance="outlined"
      dashed={style.dashed}
      size={size}
      title={`Epistemic status: ${status}`}
      className={className}
    />
  );
}

const PROJECT_STATUS_STYLE: Record<ProjectStatus, { token: ColorToken; label: string; live: boolean }> = {
  DORMANT: { token: "neutral", label: "Dormant", live: false },
  INGESTING: { token: "dormant", label: "Ingesting", live: true },
  SYNTHESIZING_GENOME: { token: "conflict", label: "Synthesizing genome", live: true },
  AUTOPSY_COMPLETE: { token: "alive", label: "Autopsy complete", live: false },
  STRATEGY_APPROVED: { token: "alive", label: "Strategy approved", live: true },
  FOUNDRY_RUNNING: { token: "conflict", label: "Foundry running", live: true },
  REBORN: { token: "alive", label: "Reborn", live: false },
  FAILED: { token: "ghost", label: "Failed", live: false },
};

export function ProjectStatusBadge({ status, size, className }: { status: ProjectStatus; size?: Size; className?: string }) {
  const style = PROJECT_STATUS_STYLE[status] ?? PROJECT_STATUS_STYLE.DORMANT;
  return (
    <TokenBadge
      token={style.token}
      label={style.label}
      glyph={style.live ? "◉" : status === "FAILED" ? "✕" : "●"}
      pulse={style.live}
      size={size}
      title={`Project status: ${status}`}
      className={className}
    />
  );
}
