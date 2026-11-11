import { cx } from "@/components/ui/cx";
import { STRATEGY_META, comparisonRows } from "@/lib/defibrillator";
import type { StrategyOut } from "@/lib/types";

/** The three strategies side by side, term by term; the best value of each row is marked. */
export function ComparisonMatrix({
  strategies,
  selectedId,
  onSelect,
}: {
  strategies: readonly StrategyOut[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const rows = comparisonRows(strategies);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left font-mono text-xs tabular-nums">
        <caption className="sr-only">Score(S_k) and its terms for each strategy; ▲ marks the best value of a row</caption>
        <thead>
          <tr className="border-b border-line">
            <th scope="col" className="py-2 pr-3 text-[10px] font-normal uppercase tracking-[0.2em] text-ink-muted">
              Measure
            </th>
            {strategies.map((s) => (
              <th key={s.id} scope="col" className="py-2 pr-3 text-right font-normal">
                <button
                  type="button"
                  onClick={() => onSelect(s.id)}
                  aria-pressed={selectedId === s.id}
                  className={cx(
                    "rounded px-1.5 py-0.5 text-[11px] uppercase tracking-[0.18em]",
                    selectedId === s.id ? "bg-emerald-bio/15 text-white" : "text-ink-muted hover:text-ink",
                  )}
                >
                  <span aria-hidden className="mr-1">{STRATEGY_META[s.strategy_type].glyph}</span>
                  {s.strategy_type}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className={cx("border-b border-line/50", row.key === "score" && "text-sm")}>
              <th scope="row" className="py-1.5 pr-3 font-normal text-ink-muted">
                {row.label}
                {row.lowerIsBetter && <span className="ml-1 text-[10px] text-ink-muted/70">(lower is better)</span>}
              </th>
              {strategies.map((s) => {
                const best = row.best.has(s.id);
                return (
                  <td
                    key={s.id}
                    className={cx(
                      "py-1.5 pr-3 text-right",
                      selectedId === s.id && "bg-emerald-bio/5",
                      best ? "text-alive-text" : "text-ink",
                      row.key === "score" && "font-semibold",
                    )}
                  >
                    {best && (
                      <>
                        <span aria-hidden>▲ </span>
                        <span className="sr-only">best: </span>
                      </>
                    )}
                    {row.cells[s.id]}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
