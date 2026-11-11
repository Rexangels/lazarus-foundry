"use client";

import { Panel } from "@/components/ui/Panel";
import { EpistemicBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { formatScore } from "@/lib/autopsy";
import { colorTokenClasses, normalizeColorToken } from "@/lib/palette";
import type { FeatureNode } from "@/lib/types";

/**
 * The forensic ledger: every visible feature with its statuses, C(f), and evidence counts, as a
 * table. It is also the keyboard- and screen-reader-friendly way into the Evidence drawer.
 */
export function FeatureLedger({
  features,
  selectedId,
  onOpen,
}: {
  features: FeatureNode[];
  selectedId: string | null;
  onOpen: (featureId: string) => void;
}) {
  return (
    <Panel eyebrow="Forensic ledger" title="Knowns, inferences and conflicts" headingId="feature-ledger-heading">
      {features.length === 0 ? (
        <p className="text-ink-muted">No feature matches the current filters.</p>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[34rem] text-left text-sm">
            <caption className="sr-only">Reconstructed features with confidence scores</caption>
            <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-muted">
              <tr>
                <th scope="col" className="px-1 py-2 font-normal">Feature</th>
                <th scope="col" className="px-1 py-2 font-normal">Status</th>
                <th scope="col" className="px-1 py-2 font-normal">C(f)</th>
                <th scope="col" className="px-1 py-2 text-right font-normal">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {features.map((node) => {
                const data = node.data;
                const classes = colorTokenClasses(normalizeColorToken(data.color_token));
                const selected = node.id === selectedId;
                const percent = Math.min(100, Math.max(0, data.confidence_score * 100));
                return (
                  <tr key={node.id} className={cx("border-t border-line/70 align-top", selected && "bg-slate-raised/70")}>
                    <th scope="row" className="px-1 py-2.5 font-normal">
                      <button
                        type="button"
                        onClick={() => onOpen(node.id)}
                        aria-current={selected ? "true" : undefined}
                        className="text-left font-display text-[13px] font-semibold tracking-wide text-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-bio"
                      >
                        {data.title}
                      </button>
                    </th>
                    <td className="px-1 py-2.5">
                      <div className="flex flex-col items-start gap-1">
                        <VitalBadge status={data.vital_status} size="sm" />
                        <EpistemicBadge status={data.epistemic_status} size="sm" />
                      </div>
                    </td>
                    <td className="px-1 py-2.5">
                      <span className={cx("font-mono text-sm tabular-nums", classes.text)}>{formatScore(data.confidence_score)}</span>
                      <span aria-hidden className="mt-1 block h-1 w-20 overflow-hidden rounded-full bg-line">
                        <span className={cx("block h-full rounded-full", classes.fill)} style={{ width: `${percent}%` }} />
                      </span>
                    </td>
                    <td className="px-1 py-2.5 text-right font-mono text-[11px] tabular-nums text-ink-muted">
                      {data.citation_count} cites
                      {data.conflict_count > 0 && (
                        <span className="block text-conflict-text">
                          <span aria-hidden>⚠ </span>
                          {data.conflict_count} conflict{data.conflict_count === 1 ? "" : "s"}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
