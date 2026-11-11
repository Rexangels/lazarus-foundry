import { cx } from "@/components/ui/cx";
import { SOURCE_TYPE_LABEL, describeStatusReason, formatScore } from "@/lib/autopsy";
import type { CitationOut, ScoreBreakdownOut } from "@/lib/types";

function Term({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div title={title}>
      <dt className="font-mono text-[9px] uppercase tracking-[0.18em] text-ink-muted">{label}</dt>
      <dd className="font-mono text-sm tabular-nums text-ink">{value}</dd>
    </div>
  );
}

function days(value: number): string {
  return Number.isInteger(value) ? `${value} d` : `${value.toFixed(1)} d`;
}

/**
 * The explainable C(f) (forensic-invariants.md section 2): every value is the backend's
 * `score_breakdowns[feature_id]`, shown as sent. Nothing is recomputed here.
 */
export function ScoreBreakdown({
  breakdown,
  confidence,
  citations,
}: {
  breakdown: ScoreBreakdownOut | undefined;
  /** The node's C(f), to flag a mismatch with the breakdown (there should be none). */
  confidence: number;
  citations: CitationOut[];
}) {
  if (!breakdown) {
    return <p className="text-sm text-ink-muted">This response carries no score breakdown for the feature.</p>;
  }
  const citationBySignal = new Map(citations.filter((c) => c.signal_index !== null).map((c) => [c.signal_index, c]));
  const epistemicReason = describeStatusReason(breakdown.status_reason);
  const vitalReason = describeStatusReason(breakdown.vital_status_reason);
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-line bg-obsidian/70 p-3">
        <p className="font-mono text-[12px] leading-relaxed text-ink-muted">
          z = ΣA<sub>t</sub> − β<sub>0</sub> − γ·Conflict(f) ={" "}
          <span className="text-ink">{formatScore(breakdown.evidence_total)}</span> −{" "}
          <span className="text-ink">{formatScore(breakdown.prior_bias)}</span> −{" "}
          <span className={breakdown.conflict ? "text-conflict-text" : "text-ink"}>{formatScore(breakdown.conflict_penalty)}</span> ={" "}
          <span className="text-ink">{formatScore(breakdown.z)}</span>
        </p>
        <p className="mt-1 font-mono text-[12px] text-ink-muted">
          C(f) = round(σ(z), 4) = <span className="text-base font-semibold text-ink">{formatScore(breakdown.confidence)}</span>
        </p>
        {breakdown.confidence !== confidence && (
          <p role="alert" className="mt-2 font-mono text-[11px] text-dormant-text">
            The node reports C(f) {formatScore(confidence)}, but the breakdown reports {formatScore(breakdown.confidence)}.
          </p>
        )}
      </div>

      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-md border border-line p-3">
          <dt className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-muted">
            Epistemic rule · {breakdown.epistemic_status}
          </dt>
          <dd className="mt-1 text-sm text-ink">
            {breakdown.status_reason ? (
              <>
                <code className="font-mono text-[12px] text-conflict-text">{breakdown.status_reason}</code>
                {epistemicReason && epistemicReason !== breakdown.status_reason && (
                  <span className="mt-1 block text-ink-muted">{epistemicReason}</span>
                )}
              </>
            ) : (
              <span className="text-ink-muted">Not recorded in this response.</span>
            )}
          </dd>
        </div>
        <div className="rounded-md border border-line p-3">
          <dt className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-muted">Vital rule · {breakdown.vital_status}</dt>
          <dd className="mt-1 text-sm text-ink">
            {breakdown.vital_status_reason ? (
              <>
                <code className="font-mono text-[12px] text-conflict-text">{breakdown.vital_status_reason}</code>
                {vitalReason && vitalReason !== breakdown.vital_status_reason && (
                  <span className="mt-1 block text-ink-muted">{vitalReason}</span>
                )}
              </>
            ) : (
              <span className="text-ink-muted">Not recorded in this response.</span>
            )}
          </dd>
        </div>
      </dl>

      {breakdown.contributions.length === 0 ? (
        <p className="text-sm text-ink-muted">
          The per-source-type terms are not included in this response
          {breakdown.matched_evidence_count !== null ? ` (${breakdown.matched_evidence_count} matched evidence items).` : "."}
        </p>
      ) : (
        <ul className="space-y-2">
          {breakdown.contributions.map((contribution) => (
            <li key={contribution.source_type} className="rounded-md border border-line bg-slate-raised/40">
              <details>
                <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 rounded-md px-3 py-2 focus-visible:outline-2 focus-visible:outline-emerald-bio">
                  <span className="text-sm text-ink">
                    <span aria-hidden className="mr-1 text-ink-muted">▸</span>
                    {SOURCE_TYPE_LABEL[contribution.source_type] ?? contribution.source_type}
                    <span className="ml-2 font-mono text-[11px] text-ink-muted">
                      n={contribution.signal_count} · w<sub>t</sub>={formatScore(contribution.authority_weight)}
                    </span>
                  </span>
                  <span className="font-mono text-sm tabular-nums text-ink">
                    A<sub>t</sub> {formatScore(contribution.contribution)}
                    {contribution.capped && (
                      <span className="ml-2 rounded border border-dormant/50 px-1 text-[10px] uppercase text-dormant-text">
                        capped at {formatScore(contribution.cap)}
                      </span>
                    )}
                  </span>
                </summary>
                <div className="border-t border-line px-3 pb-3 pt-2">
                  <dl className="grid grid-cols-3 gap-2">
                    <Term label="Σ ρ^(k−1)·term" value={formatScore(contribution.uncapped)} />
                    <Term label="Cap κ·w_t" value={formatScore(contribution.cap)} />
                    <Term label="A_t" value={formatScore(contribution.contribution)} />
                  </dl>
                  <div className="mt-3 overflow-x-auto">
                    <table className="w-full min-w-[30rem] text-left font-mono text-[11px] tabular-nums">
                      <caption className="sr-only">
                        Terms for {SOURCE_TYPE_LABEL[contribution.source_type] ?? contribution.source_type}
                      </caption>
                      <thead className="text-ink-muted">
                        <tr>
                          <th scope="col" className="py-1 pr-2 font-normal">signal</th>
                          <th scope="col" className="py-1 pr-2 font-normal">rank</th>
                          <th scope="col" className="py-1 pr-2 font-normal">s(f,e)</th>
                          <th scope="col" className="py-1 pr-2 font-normal">Δt</th>
                          <th scope="col" className="py-1 pr-2 font-normal">e^(−λΔt)</th>
                          <th scope="col" className="py-1 pr-2 font-normal">w·s·decay</th>
                          <th scope="col" className="py-1 pr-2 font-normal">ρ^(k−1)</th>
                          <th scope="col" className="py-1 font-normal">term</th>
                        </tr>
                      </thead>
                      <tbody className="text-ink">
                        {contribution.terms.map((term) => {
                          const cited = citationBySignal.get(term.signal_index);
                          return (
                            <tr key={`${term.signal_index}-${term.rank}`} className="border-t border-line/60">
                              <td className="py-1 pr-2">
                                {cited ? (
                                  <a
                                    href={`#citation-${cited.id}`}
                                    className="text-alive-text underline decoration-dotted underline-offset-2"
                                    title={`${cited.file_path}`}
                                  >
                                    #{term.signal_index}
                                  </a>
                                ) : (
                                  `#${term.signal_index}`
                                )}
                              </td>
                              <td className="py-1 pr-2">{term.rank}</td>
                              <td className="py-1 pr-2">{formatScore(term.match_strength)}</td>
                              <td className="py-1 pr-2">{days(term.age_days)}</td>
                              <td className="py-1 pr-2">{formatScore(term.decay)}</td>
                              <td className="py-1 pr-2">{formatScore(term.base_term)}</td>
                              <td className="py-1 pr-2">{formatScore(term.rank_factor)}</td>
                              <td className={cx("py-1", "text-ink")}>{formatScore(term.term)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
