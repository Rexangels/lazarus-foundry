"use client";

/**
 * Custom React Flow node types for the Genome Graph (design.md, "Genome API contract"):
 * `feature`, `branch` and `document`. Status is never color alone: vital badges carry ● ◐ ○,
 * epistemic badges ✓ ≈ ⚠ ?, and ghost features have a dashed outline.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { memo, type CSSProperties } from "react";

import { EpistemicBadge, TokenBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import {
  HANDLE,
  formatScore,
  shortSha,
  type BranchFlowNode,
  type DocumentFlowNode,
  type FeatureFlowNode,
} from "@/lib/autopsy";
import { COLOR_TOKEN_HEX, COLOR_TOKEN_TEXT_HEX, PALETTE, colorTokenClasses, normalizeColorToken } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

/** Handles anchor edges only; nothing can be connected by hand. */
const HIDDEN_HANDLE: CSSProperties = { opacity: 0, width: 6, height: 6, minWidth: 0, minHeight: 0, border: 0, pointerEvents: "none" };

/** C(f) as a ring: the arc is the score, the label is the exact backend value. */
export function ConfidenceRing({ value, token, size = 58 }: { value: number; token: ColorToken; size?: number }) {
  const radius = size / 2 - 4;
  const circumference = 2 * Math.PI * radius;
  const fraction = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`C(f) ${formatScore(value)}`}>
      <circle cx={size / 2} cy={size / 2} r={radius} fill={PALETTE.obsidian} stroke="#222C3A" strokeWidth={4} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={COLOR_TOKEN_HEX[token]}
        strokeWidth={4}
        strokeLinecap="round"
        strokeDasharray={`${fraction * circumference} ${circumference}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ filter: `drop-shadow(0 0 4px ${COLOR_TOKEN_HEX[token]}88)` }}
      />
      <text
        x="50%"
        y="44%"
        textAnchor="middle"
        dominantBaseline="middle"
        fontSize={7}
        letterSpacing="0.15em"
        fill={PALETTE.inkMuted}
        style={{ fontFamily: "var(--font-mono)" }}
      >
        C(f)
      </text>
      <text
        x="50%"
        y="62%"
        textAnchor="middle"
        dominantBaseline="middle"
        fontSize={11}
        fontWeight={600}
        fill={COLOR_TOKEN_TEXT_HEX[token]}
        style={{ fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums" }}
      >
        {formatScore(value)}
      </text>
    </svg>
  );
}

function FeatureNodeViewImpl({ data, selected }: NodeProps<FeatureFlowNode>) {
  const feature = data.feature;
  const token = normalizeColorToken(feature.color_token);
  const classes = colorTokenClasses(token);
  const ghost = feature.vital_status === "GHOST_FEATURE";
  return (
    <div
      className={cx(
        "group relative w-[268px] cursor-pointer rounded-[10px] border bg-slate-core/95 p-3 text-left transition-[opacity,box-shadow,border-color] duration-200",
        classes.border,
        ghost && cx("border-dashed", classes.bg),
        selected ? classes.glow : "shadow-[0_8px_24px_-12px_rgb(0_0_0/0.8)]",
        data.dimmed && "opacity-30",
      )}
    >
      <Handle type="source" id={HANDLE.claim} position={Position.Left} isConnectable={false} style={HIDDEN_HANDLE} />
      <Handle type="source" id={HANDLE.evidence} position={Position.Right} isConnectable={false} style={HIDDEN_HANDLE} />
      <span aria-hidden className={cx("absolute inset-x-4 top-0 h-px opacity-80", classes.fill)} />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <VitalBadge status={feature.vital_status} size="sm" />
          <p className="mt-2 font-mono text-[9px] uppercase tracking-[0.2em] text-ink-muted">
            {feature.category.replace(/_/g, " ")}
          </p>
          <p className="mt-0.5 line-clamp-2 font-display text-[13px] font-semibold leading-snug tracking-wide text-ink">
            {feature.title}
          </p>
        </div>
        <ConfidenceRing value={feature.confidence_score} token={token} />
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <EpistemicBadge status={feature.epistemic_status} size="sm" />
        <span className="font-mono text-[10px] tabular-nums text-ink-muted">
          {feature.citation_count} cites
          {feature.conflict_count > 0 && (
            <span className="text-conflict-text">
              {" "}· <span aria-hidden>⚠</span> {feature.conflict_count} conflict{feature.conflict_count === 1 ? "" : "s"}
            </span>
          )}
        </span>
      </div>
    </div>
  );
}

function BranchNodeViewImpl({ data, selected }: NodeProps<BranchFlowNode>) {
  const branch = data.branch;
  const token = normalizeColorToken(branch.color_token);
  const classes = colorTokenClasses(token);
  return (
    <div
      className={cx(
        "relative w-[252px] cursor-pointer rounded-[10px] border bg-slate-core/95 p-3 transition-[opacity,box-shadow] duration-200",
        classes.border,
        !branch.is_merged && !branch.is_abandoned && "border-dashed",
        selected && classes.glow,
        data.dimmed && "opacity-30",
      )}
    >
      <Handle type="target" id={HANDLE.target} position={Position.Left} isConnectable={false} style={HIDDEN_HANDLE} />
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-ink-muted">
          <span aria-hidden>⎇ </span>Branch
        </span>
        {branch.is_abandoned ? (
          <TokenBadge token="dormant" label="Abandoned" glyph="◐" size="sm" />
        ) : branch.is_merged ? (
          <TokenBadge token="neutral" label="Merged" glyph="●" size="sm" />
        ) : (
          <TokenBadge token="neutral" label="Unmerged" glyph="◌" size="sm" dashed />
        )}
      </div>
      <p className={cx("mt-2 break-all font-mono text-[12px] font-medium", classes.text)}>{branch.name}</p>
      <dl className="mt-2 grid grid-cols-3 gap-1 font-mono tabular-nums">
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-ink-muted">Dormant</dt>
          <dd className="text-[13px] text-ink">{branch.dormancy_days} d</dd>
        </div>
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-ink-muted">Ahead</dt>
          <dd className="text-[13px] text-alive-text">+{branch.commits_ahead}</dd>
        </div>
        <div>
          <dt className="text-[9px] uppercase tracking-wider text-ink-muted">Behind</dt>
          <dd className="text-[13px] text-ghost-text">−{branch.commits_behind}</dd>
        </div>
      </dl>
      <p className="mt-2 font-mono text-[10px] text-ink-muted">
        head {shortSha(branch.head_sha)} · {branch.citation_count} cites
      </p>
    </div>
  );
}

function DocumentNodeViewImpl({ data, selected }: NodeProps<DocumentFlowNode>) {
  const doc = data.document;
  const classes = colorTokenClasses(normalizeColorToken(doc.color_token));
  return (
    <div
      className={cx(
        "relative w-[236px] cursor-pointer rounded-[10px] border bg-slate-core/95 p-3 transition-[opacity,box-shadow] duration-200",
        classes.border,
        selected ? classes.glow : "shadow-[0_0_24px_-10px_rgb(139_92_246/0.6)]",
        data.dimmed && "opacity-30",
      )}
    >
      <Handle type="target" id={HANDLE.target} position={Position.Right} isConnectable={false} style={HIDDEN_HANDLE} />
      <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-ink-muted">
        <span aria-hidden>¶ </span>Document
      </span>
      <p className="mt-1 break-all font-mono text-[14px] font-medium text-ink">{doc.file_path}</p>
      <p className={cx("mt-2 font-mono text-[11px]", classes.text)}>
        <span aria-hidden>⚠ </span>
        {doc.conflict_count} conflicting claim{doc.conflict_count === 1 ? "" : "s"}
      </p>
      <p className="mt-0.5 font-mono text-[10px] text-ink-muted">
        claims → {doc.feature_ids.length} feature{doc.feature_ids.length === 1 ? "" : "s"}
      </p>
    </div>
  );
}

export const FeatureNodeView = memo(FeatureNodeViewImpl);
export const BranchNodeView = memo(BranchNodeViewImpl);
export const DocumentNodeView = memo(DocumentNodeViewImpl);
