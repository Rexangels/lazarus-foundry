"use client";

/**
 * Custom React Flow edge types: `evidence` (feature -> branch, colored by the branch's token) and
 * `conflict` (feature -> the document holding the contradicted claim: Electric Violet, dashed,
 * with dashes flowing from the claim; the flow stops under prefers-reduced-motion).
 */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";
import { memo } from "react";

import { cx } from "@/components/ui/cx";
import { SOURCE_TYPE_CODE, formatLocation, type ConflictFlowEdge, type EvidenceFlowEdge } from "@/lib/autopsy";
import { COLOR_TOKEN_HEX, COLOR_TOKEN_TEXT_HEX, normalizeColorToken } from "@/lib/palette";

function EvidenceEdgeViewImpl({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<EvidenceFlowEdge>) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const token = normalizeColorToken(data?.evidence.color_token);
  const color = COLOR_TOKEN_HEX[token];
  const opacity = data?.dimmed ? 0.15 : 0.9;
  const codes = (data?.evidence.source_types ?? []).map((type) => SOURCE_TYPE_CODE[type] ?? type).join(" · ");
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        style={{
          stroke: color,
          strokeWidth: selected ? 3 : 2,
          opacity,
          filter: selected ? `drop-shadow(0 0 6px ${color})` : undefined,
        }}
      />
      <EdgeLabelRenderer>
        <div
          aria-hidden
          className="nodrag nopan pointer-events-none absolute rounded border bg-obsidian/95 px-1.5 py-0.5 font-mono text-[10px] tabular-nums"
          style={{
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            borderColor: `${color}80`,
            color: COLOR_TOKEN_TEXT_HEX[token],
            opacity: data?.dimmed ? 0.25 : 1,
          }}
        >
          {data?.evidence.citation_count ?? 0} cites{codes ? ` · ${codes}` : ""}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

function ConflictEdgeViewImpl({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<ConflictFlowEdge>) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const color = COLOR_TOKEN_HEX.conflict;
  const highlighted = Boolean(data?.highlighted || selected);
  const conflict = data?.conflict;
  const location = conflict ? formatLocation(conflict.file_path, conflict.line_start, conflict.line_end) : "conflict";
  return (
    <>
      {highlighted && (
        <path d={path} fill="none" stroke={color} strokeWidth={10} strokeOpacity={0.18} style={{ filter: "blur(3px)" }} />
      )}
      <BaseEdge
        id={id}
        path={path}
        className="motion-safe:animate-dash-flow"
        style={{
          stroke: color,
          strokeWidth: highlighted ? 3 : 2,
          strokeDasharray: "6 4",
          opacity: data?.dimmed ? 0.18 : 1,
          filter: `drop-shadow(0 0 ${highlighted ? 8 : 3}px ${color})`,
        }}
      />
      <EdgeLabelRenderer>
        <div
          aria-hidden
          className={cx(
            "nodrag nopan pointer-events-none absolute whitespace-nowrap rounded border px-1.5 py-0.5 font-mono text-[10px]",
            highlighted ? "border-conflict bg-conflict/25 text-white shadow-glow-conflict" : "border-conflict/60 bg-obsidian/95 text-conflict-text",
          )}
          style={{
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            opacity: data?.dimmed ? 0.25 : 1,
          }}
        >
          ⚠ {location}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export const EvidenceEdgeView = memo(EvidenceEdgeViewImpl);
export const ConflictEdgeView = memo(ConflictEdgeViewImpl);
