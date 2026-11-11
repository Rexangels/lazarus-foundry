import { cx } from "@/components/ui/cx";
import {
  describeRisk,
  describeValue,
  formatDecimal,
  formatScore,
  substitutedFormula,
  type ScoreBreakdownView,
  type ScoreTerm,
} from "@/lib/defibrillator";

/** Score(S_k) = round(100 · (α·V + β·(1 − D) + δ·(1 − R)), 1), as plain math markup. */
export function FormulaMath({ decimals = 1, className }: { decimals?: number; className?: string }) {
  const v = (name: string) => <var className="font-serif text-[1.05em] italic text-ink">{name}</var>;
  const w = (name: string) => <var className="font-serif text-[1.05em] italic text-conflict-text">{name}</var>;
  return (
    <p
      className={cx("flex flex-wrap items-baseline gap-x-1 font-mono text-sm text-ink-muted", className)}
      aria-label={`Score of S k equals round of 100 times (alpha V plus beta times (1 minus D) plus delta times (1 minus R)), to ${decimals} decimal`}
    >
      <span className="text-ink">
        Score(<var className="font-serif italic">S</var>
        <sub className="font-serif italic">k</sub>)
      </span>
      <span>=</span>
      <span>round(100 · (</span>
      {w("α")}·{v("V")}
      <span>+</span>
      {w("β")}·(1 − {v("D")})<span>+</span>
      {w("δ")}·(1 − {v("R")})<span>), {decimals})</span>
    </p>
  );
}

const TERM_BAR: Record<ScoreTerm["key"], string> = {
  value: "bg-alive",
  debt: "bg-dormant",
  risk: "bg-conflict",
};
const TERM_TEXT: Record<ScoreTerm["key"], string> = {
  value: "text-alive-text",
  debt: "text-dormant-text",
  risk: "text-conflict-text",
};

/** The three contributions as one stacked bar out of 100 points. */
export function TermBar({ view, className }: { view: ScoreBreakdownView; className?: string }) {
  return (
    <div className={cx("space-y-1", className)}>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-obsidian ring-1 ring-line" aria-hidden>
        {view.terms.map((term) => (
          <span
            key={term.key}
            className={cx("h-full first:rounded-l-full", TERM_BAR[term.key])}
            style={{ width: `${Math.max(0, Math.min(100, term.points ?? 0))}%`, opacity: 0.85 }}
          />
        ))}
      </div>
      <p className="flex flex-wrap gap-x-3 font-mono text-[10px] text-ink-muted">
        {view.terms.map((term) => (
          <span key={term.key}>
            <span className={TERM_TEXT[term.key]}>{term.symbol}</span> {formatDecimal(term.points, 3)} pts
          </span>
        ))}
      </p>
    </div>
  );
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

/** The full explainable breakdown of one strategy's Score(S_k) (PlanScore.as_dict). */
export function ScoreFormula({ view, featureTitles }: { view: ScoreBreakdownView; featureTitles: Map<string, string> }) {
  const name = (id: string) => featureTitles.get(id) ?? shortId(id);
  return (
    <div className="space-y-5">
      <div className="rounded-md border border-line bg-obsidian/70 p-4">
        <FormulaMath decimals={view.scoreDecimals} />
        <p className="mt-2 break-words font-mono text-xs text-ink-muted">
          <span className="sr-only">With this strategy&apos;s numbers: </span>
          {substitutedFormula(view)} = <span className="text-base font-semibold text-ink">{formatScore(view.score, view.scoreDecimals)}</span>
        </p>
        <p className="mt-1 font-mono text-[10px] text-ink-muted/80">backend formula: {view.formula}</p>
      </div>

      <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
        <caption className="sr-only">Each term of Score(S_k) in score points</caption>
        <thead>
          <tr className="border-b border-line text-[10px] uppercase tracking-[0.15em] text-ink-muted">
            <th scope="col" className="py-1.5 pr-2 font-normal">Term</th>
            <th scope="col" className="py-1.5 pr-2 font-normal">Weight</th>
            <th scope="col" className="py-1.5 pr-2 font-normal">Input</th>
            <th scope="col" className="py-1.5 pr-2 text-right font-normal">Points</th>
            <th scope="col" className="w-1/3 py-1.5 font-normal">
              <span className="sr-only">Share of 100</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {view.terms.map((term) => (
            <tr key={term.key} className="border-b border-line/60">
              <th scope="row" className="py-2 pr-2 font-normal text-ink">
                <span className={cx("font-semibold", TERM_TEXT[term.key])}>{term.weightSymbol}·{term.inverted ? `(1 − ${term.symbol})` : term.symbol}</span>
                <span className="ml-2 font-sans text-[11px] text-ink-muted">{term.label}</span>
              </th>
              <td className="py-2 pr-2 text-ink">{formatDecimal(term.weight)}</td>
              <td className="py-2 pr-2 text-ink">
                {term.inverted ? `1 − ${formatDecimal(term.raw)} = ${formatDecimal(term.input)}` : formatDecimal(term.input)}
              </td>
              <td className="py-2 pr-2 text-right text-ink">{formatDecimal(term.points)}</td>
              <td className="py-2">
                <div className="h-1.5 w-full rounded-full bg-obsidian ring-1 ring-line" aria-hidden>
                  <div className={cx("h-full rounded-full", TERM_BAR[term.key])} style={{ width: `${Math.max(0, Math.min(100, (term.points ?? 0) / Math.max(1, (term.weight ?? 0) * 100) * 100))}%` }} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={3} className="pt-2 pr-2 text-right font-normal text-ink-muted">
              Σ points{view.pointsTotal !== null && ` ${formatDecimal(view.pointsTotal)}`} → rounded to {view.scoreDecimals} decimal
            </th>
            <td className="pt-2 pr-2 text-right text-base font-semibold text-ink">{formatScore(view.score, view.scoreDecimals)}</td>
            <td />
          </tr>
        </tfoot>
      </table>

      <dl className="grid gap-3 md:grid-cols-3">
        <div className="rounded-md border border-alive/30 bg-alive/5 p-3">
          <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-alive-text">
            V = {formatDecimal(view.terms[0]?.raw)} · value
          </dt>
          <dd className="mt-1.5 space-y-2 text-xs text-ink-muted">
            <p>{describeValue(view)}</p>
            {view.value && (
              <>
                <p className="font-mono text-[10px] uppercase tracking-wider">mode {view.value.mode || "?"}</p>
                {view.value.verifiedFeatureIds.length > 0 && (
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Verified working</p>
                    <ul className="mt-1 space-y-0.5">
                      {view.value.verifiedFeatureIds.map((id) => (
                        <li key={id} className="flex gap-1.5 text-ink">
                          <span aria-hidden className="text-alive-text">●</span>
                          <span className="min-w-0">
                            {name(id)}
                            {view.value?.extractedFeatureIds.includes(id) && (
                              <span className="ml-1 font-mono text-[10px] text-alive-text">extracted</span>
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {view.value.targets.length > 0 && (
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Targets × feasibility</p>
                    <ul className="mt-1 space-y-0.5">
                      {view.value.targets.map((t) => (
                        <li key={t.featureId} className="flex gap-1.5 text-ink">
                          <span aria-hidden className="text-dormant-text">◐</span>
                          <span className="min-w-0 flex-1">{t.title || name(t.featureId)}</span>
                          <span className="font-mono tabular-nums text-dormant-text">{formatDecimal(t.feasibility)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {view.value.unrounded !== null && (
                  <p className="font-mono text-[10px]">unrounded {formatDecimal(view.value.unrounded, 6)}</p>
                )}
              </>
            )}
          </dd>
        </div>

        <div className="rounded-md border border-dormant/30 bg-dormant/5 p-3">
          <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-dormant-text">
            D = {formatDecimal(view.terms[1]?.raw)} · debt cost
          </dt>
          <dd className="mt-1.5 space-y-2 text-xs text-ink-muted">
            {view.debt ? (
              <>
                <p>
                  {view.debt.fileCount ?? "?"} file(s) against a budget of {formatDecimal(view.debt.fileBudget)}
                  {view.debt.unrounded !== null && ` = ${formatDecimal(view.debt.unrounded, 6)}`}
                </p>
                <ul className="space-y-0.5 font-mono text-[11px] text-ink">
                  {view.debt.files.map((file) => (
                    <li key={file} className="break-all">
                      {file}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p>No debt derivation was recorded.</p>
            )}
          </dd>
        </div>

        <div className="rounded-md border border-conflict/30 bg-conflict/5 p-3">
          <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-conflict-text">
            R = {formatDecimal(view.terms[2]?.raw)} · risk
          </dt>
          <dd className="mt-1.5 space-y-2 text-xs text-ink-muted">
            <p className="font-mono text-[11px] text-ink">{describeRisk(view)}</p>
            {view.risk && view.risk.factors.length > 0 && (
              <ul className="space-y-1">
                {view.risk.factors.map((f) => (
                  <li key={f.factor}>
                    <span className="font-mono text-[11px] text-conflict-text">{f.factor}</span>: {f.count ?? "?"} ×{" "}
                    {formatDecimal(f.perUnit)}, capped at {formatDecimal(f.cap)} → +{formatDecimal(f.adjustment)}
                  </li>
                ))}
              </ul>
            )}
            {view.risk && view.risk.factors.length === 0 && <p>Base risk only; no factor applied.</p>}
          </dd>
        </div>
      </dl>
    </div>
  );
}
