"use client";

import { useMemo } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { Panel } from "@/components/ui/Panel";
import { EpistemicBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { formatScore, materializingNodes } from "@/lib/autopsy";
import { VITAL_STATUS_TOKEN, colorTokenClasses } from "@/lib/palette";

/**
 * While the genome is pending (409 GENOME_PENDING), the features the reconciler has announced over
 * telemetry (GENOME_NODE_CREATED) materialize here, one card per node.
 */
export function PendingGenome({ detail }: { detail: string }) {
  const { telemetry } = useProjectSession();
  const { nodes, conflicts } = useMemo(() => materializingNodes(telemetry.frames), [telemetry.frames]);
  return (
    <Panel
      eyebrow="Genome synthesis"
      title={nodes.length ? `Genome materializing · ${nodes.length} feature${nodes.length === 1 ? "" : "s"}` : "Waiting for the genome"}
      headingId="genome-pending-heading"
      tone="conflict"
      glow
    >
      <p className="text-sm text-ink-muted">{detail}</p>
      <div className="relative mt-4 overflow-hidden rounded-md border border-line bg-obsidian/70 p-4" aria-busy="true">
        <span aria-hidden className="motion-scan pointer-events-none absolute inset-x-0 top-0 h-16 bg-linear-to-b from-conflict/15 to-transparent motion-safe:animate-scan" />
        {nodes.length === 0 ? (
          <p className="font-mono text-xs text-ink-muted">
            <span aria-hidden className="mr-2 text-conflict-text">▍</span>
            No feature reconstructed yet. Telemetry will announce each one as it is created.
          </p>
        ) : (
          <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-live="polite">
            {nodes.map((node) => {
              const classes = node.vitalStatus ? colorTokenClasses(VITAL_STATUS_TOKEN[node.vitalStatus]) : colorTokenClasses("neutral");
              return (
                <li
                  key={node.nodeId}
                  className={cx("rounded-md border bg-slate-core/90 p-3 motion-safe:animate-feed-in", classes.border)}
                >
                  <div className="flex flex-wrap gap-1.5">
                    {node.vitalStatus && <VitalBadge status={node.vitalStatus} size="sm" />}
                    {node.epistemicStatus && <EpistemicBadge status={node.epistemicStatus} size="sm" />}
                  </div>
                  <p className="mt-2 font-display text-sm font-semibold tracking-wide text-ink">{node.title}</p>
                  <p className="mt-1 font-mono text-[11px] tabular-nums text-ink-muted">
                    C(f) <span className={classes.text}>{formatScore(node.confidence)}</span>
                    {node.citationCount !== null && ` · ${node.citationCount} cites`}
                  </p>
                </li>
              );
            })}
          </ol>
        )}
        {conflicts > 0 && (
          <p className="mt-3 font-mono text-xs text-conflict-text">
            <span aria-hidden>⚠ </span>
            {conflicts} README claim{conflicts === 1 ? "" : "s"} contradicted so far
          </p>
        )}
      </div>
      <p className="mt-3 text-xs text-ink-muted">
        Evidence citations open once the genome is complete; until then these are announcements, not findings.
      </p>
    </Panel>
  );
}
