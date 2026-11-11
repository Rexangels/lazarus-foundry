"use client";

import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { EpistemicBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { claimCitation, findFeature, formatLocation } from "@/lib/autopsy";
import type { ConflictOut, GenomeGraphOut } from "@/lib/types";

import { SnippetBlock } from "./Citation";

/**
 * One ConflictRecord as claim vs reality. The claim is shown with its cited snippet and lines; if
 * the claim citation cannot be resolved, the record's `claim_source` text is shown and labelled so.
 */
export function ConflictRecordView({
  genome,
  conflict,
  compact = false,
}: {
  genome: GenomeGraphOut;
  conflict: ConflictOut;
  compact?: boolean;
}) {
  const claim = claimCitation(genome, conflict);
  const claimLocation = claim
    ? formatLocation(claim.file_path, claim.line_start, claim.line_end)
    : conflict.claim_file_path
      ? formatLocation(conflict.claim_file_path, conflict.claim_line_start, conflict.claim_line_end)
      : null;
  return (
    <div className="space-y-3">
      <div className={cx("grid gap-3", !compact && "lg:grid-cols-2")}>
        <section aria-label="Claim" className="rounded-md border border-conflict/50 bg-conflict/5 p-3">
          <h4 className="flex flex-wrap items-baseline gap-2 font-mono text-[10px] uppercase tracking-[0.2em] text-conflict-text">
            <span aria-hidden>¶</span> Claim
            {claimLocation && <span className="normal-case tracking-normal text-ink">{claimLocation}</span>}
          </h4>
          {claim ? (
            <SnippetBlock
              snippet={claim.snippet}
              lineStart={claim.line_start}
              lineEnd={claim.line_end}
              filePath={claim.file_path}
              className="mt-2"
            />
          ) : (
            <>
              <p className="mt-2 break-words font-mono text-[12px] text-ink">{conflict.claim_source}</p>
              <p className="mt-1 text-[11px] text-ink-muted">The claim citation is not in this response; this is the record&apos;s claim source.</p>
            </>
          )}
        </section>
        <section aria-label="Reality" className="rounded-md border border-line bg-obsidian/60 p-3">
          <h4 className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">
            <span aria-hidden>⌁</span> Reality
          </h4>
          <p className="mt-2 break-words font-mono text-[12px] leading-relaxed text-ink">{conflict.reality_source}</p>
        </section>
      </div>
      <p className="text-sm text-ink">{conflict.conflict_description}</p>
      <p className="rounded-md border border-alive/30 bg-alive/5 px-3 py-2 text-sm text-ink">
        <span className="mr-2 font-mono text-[10px] uppercase tracking-[0.2em] text-alive-text">Resolution</span>
        {conflict.resolution_recommendation}
      </p>
    </div>
  );
}

/** Every ConflictRecord of the genome. Inspecting one selects its feature and highlights its edge. */
export function ConflictInspector({
  genome,
  activeConflictId,
  onInspect,
}: {
  genome: GenomeGraphOut;
  activeConflictId: string | null;
  onInspect: (conflictId: string) => void;
}) {
  const conflicts = genome.conflicts;
  return (
    <Panel
      eyebrow="Conflict inspector"
      title={`Claims vs reality · ${conflicts.length}`}
      headingId="conflict-inspector-heading"
      tone="conflict"
      glow={conflicts.length > 0}
    >
      {conflicts.length === 0 ? (
        <p className="text-ink-muted">No documentation claim contradicts the code.</p>
      ) : (
        <ol className="space-y-4">
          {conflicts.map((conflict, index) => {
            const feature = findFeature(genome, conflict.feature_id);
            const active = conflict.id === activeConflictId;
            return (
              <li
                key={conflict.id}
                className={cx(
                  "rounded-lg border p-4 transition-shadow",
                  active ? "border-conflict shadow-glow-conflict" : "border-line bg-slate-raised/30",
                )}
              >
                <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">
                      Conflict {String(index + 1).padStart(2, "0")}
                    </p>
                    <h3 className="mt-0.5 font-display text-base font-semibold tracking-wide text-ink">
                      {feature?.data.title ?? "Unknown feature"}
                    </h3>
                    {feature && (
                      <div className="mt-1.5 flex flex-wrap gap-2">
                        <VitalBadge status={feature.data.vital_status} size="sm" />
                        <EpistemicBadge status={feature.data.epistemic_status} size="sm" />
                      </div>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant={active ? "primary" : "secondary"}
                    aria-pressed={active}
                    onClick={() => onInspect(conflict.id)}
                  >
                    {active ? "Inspecting" : "Inspect"}
                  </Button>
                </div>
                <ConflictRecordView genome={genome} conflict={conflict} />
                {conflict.edge_id === null && (
                  <p className="mt-2 font-mono text-[11px] text-dormant-text">
                    No edge is drawn: the claim&apos;s location could not be resolved.
                  </p>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}
