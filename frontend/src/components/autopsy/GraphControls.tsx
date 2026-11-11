"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { cx } from "@/components/ui/cx";
import {
  EPISTEMIC_STATUSES,
  VITAL_STATUSES,
  isUnfiltered,
  type FeatureCounts,
  type GenomeFilters,
} from "@/lib/autopsy";
import {
  COLOR_TOKEN_HEX,
  EPISTEMIC_STATUS_STYLE,
  VITAL_STATUS_GLYPH,
  VITAL_STATUS_LABEL,
  VITAL_STATUS_TOKEN,
  colorTokenClasses,
} from "@/lib/palette";
import type { ColorToken, EpistemicStatus, VitalStatus } from "@/lib/types";

function FilterChip({
  pressed,
  token,
  glyph,
  label,
  count,
  dashed,
  onClick,
}: {
  pressed: boolean;
  token: ColorToken;
  glyph: string;
  label: string;
  count: number;
  dashed?: boolean;
  onClick: () => void;
}) {
  const classes = colorTokenClasses(token);
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cx(
        "inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 font-mono text-[11px] uppercase tracking-wider transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-bio",
        dashed && "border-dashed",
        pressed ? cx(classes.border, classes.bg, classes.text) : "border-line bg-transparent text-ink-muted line-through decoration-ink-muted/60 hover:text-ink",
      )}
    >
      <span aria-hidden>{glyph}</span>
      {label}
      <span className="tabular-nums opacity-80">{count}</span>
    </button>
  );
}

/** Toggle features in and out of the graph and ledger by vital and epistemic status. */
export function GraphFilters({
  filters,
  counts,
  visibleCount,
  onToggleVital,
  onToggleEpistemic,
  onReset,
}: {
  filters: GenomeFilters;
  counts: FeatureCounts;
  visibleCount: number;
  onToggleVital: (status: VitalStatus) => void;
  onToggleEpistemic: (status: EpistemicStatus) => void;
  onReset: () => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
      <fieldset>
        <legend className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Vital status</legend>
        <div className="flex flex-wrap gap-2">
          {VITAL_STATUSES.map((status) => (
            <FilterChip
              key={status}
              pressed={filters.vital.has(status)}
              token={VITAL_STATUS_TOKEN[status]}
              glyph={VITAL_STATUS_GLYPH[status]}
              label={VITAL_STATUS_LABEL[status]}
              count={counts.vital[status]}
              onClick={() => onToggleVital(status)}
            />
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Epistemic status</legend>
        <div className="flex flex-wrap gap-2">
          {EPISTEMIC_STATUSES.map((status) => {
            const style = EPISTEMIC_STATUS_STYLE[status];
            return (
              <FilterChip
                key={status}
                pressed={filters.epistemic.has(status)}
                token={style.token}
                glyph={style.glyph}
                label={style.label}
                count={counts.epistemic[status]}
                dashed={style.dashed}
                onClick={() => onToggleEpistemic(status)}
              />
            );
          })}
        </div>
      </fieldset>
      <div className="ml-auto flex items-center gap-3">
        <p className="font-mono text-[11px] tabular-nums text-ink-muted" aria-live="polite">
          {visibleCount} of {counts.total} features shown
        </p>
        {!isUnfiltered(filters) && (
          <Button size="sm" variant="ghost" onClick={onReset}>
            Show all
          </Button>
        )}
      </div>
    </div>
  );
}

function Swatch({ token, dashed = false, shape = "card" }: { token: ColorToken; dashed?: boolean; shape?: "card" | "line" }) {
  const color = COLOR_TOKEN_HEX[token];
  if (shape === "line") {
    return (
      <svg aria-hidden width="34" height="8" viewBox="0 0 34 8">
        <line x1="1" y1="4" x2="33" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dashed ? "6 4" : undefined} />
      </svg>
    );
  }
  return (
    <span
      aria-hidden
      className={cx("inline-block h-3.5 w-5 rounded-[3px] border bg-slate-core", dashed && "border-dashed")}
      style={{ borderColor: color }}
    />
  );
}

/** How to read the graph. */
export function GraphLegend({ className }: { className?: string }) {
  const items: { key: string; swatch: ReactNode; label: string }[] = [
    { key: "alive", swatch: <Swatch token="alive" />, label: "● Verified working" },
    { key: "dormant", swatch: <Swatch token="dormant" />, label: "◐ Abandoned in progress / abandoned branch" },
    { key: "ghost", swatch: <Swatch token="ghost" dashed />, label: "○ Ghost feature (claimed, no code)" },
    { key: "doc", swatch: <Swatch token="conflict" />, label: "¶ Document holding contradicted claims" },
    { key: "evidence", swatch: <Swatch token="dormant" shape="line" />, label: "Evidence edge (citations on that branch)" },
    { key: "conflict", swatch: <Swatch token="conflict" shape="line" dashed />, label: "Conflict edge (claim vs code)" },
  ];
  return (
    <div className={cx("flex flex-wrap items-center gap-x-5 gap-y-2", className)}>
      <h3 className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Legend</h3>
      <ul className="flex flex-wrap items-center gap-x-5 gap-y-2">
        {items.map((item) => (
          <li key={item.key} className="flex items-center gap-2 text-xs text-ink-muted">
            {item.swatch}
            {item.label}
          </li>
        ))}
        <li className="flex items-center gap-2 text-xs text-ink-muted">
          <span aria-hidden className="inline-block h-3.5 w-3.5 rounded-full border-2 border-alive" />
          Ring = confidence C(f), exact backend value
        </li>
      </ul>
    </div>
  );
}
