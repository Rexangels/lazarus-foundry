import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { STRATEGY_META, formatDecimal, readScoreBreakdown, type GateAvailability } from "@/lib/defibrillator";
import type { StrategyOut } from "@/lib/types";

import { ScoreGauge } from "./ScoreGauge";
import { TermBar } from "./ScoreFormula";

export interface StrategyCardProps {
  strategy: StrategyOut;
  selected: boolean;
  highest: boolean;
  availability: GateAvailability;
  onSelect: (id: string) => void;
}

/** One strategy of the deck: what it does, its Score(S_k) dial and its V/D/R at a glance. */
export function StrategyCard({ strategy, selected, highest, availability, onSelect }: StrategyCardProps) {
  const view = readScoreBreakdown(strategy);
  const meta = STRATEGY_META[strategy.strategy_type];
  const approved = availability.kind === "approved";
  const locked = availability.kind === "locked";
  const headingId = `strategy-${strategy.id}-title`;
  return (
    <article
      aria-labelledby={headingId}
      className={cx(
        "relative flex flex-col rounded-lg border bg-slate-core/90 p-5 transition-[box-shadow,border-color,opacity] duration-300",
        selected ? "border-emerald-bio/70 shadow-glow-alive" : "border-line hover:border-ink-muted/40",
        approved && "border-emerald-bio shadow-glow-alive",
        locked && "opacity-60",
      )}
    >
      {selected && (
        <span aria-hidden className="pointer-events-none absolute inset-x-6 -top-px h-px bg-linear-to-r from-transparent via-emerald-bio to-transparent" />
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[11px] uppercase tracking-[0.25em] text-ink-muted">
          <span aria-hidden className="mr-1 text-base text-ink">{meta.glyph}</span>
          {strategy.strategy_type}
        </span>
        <span className="ml-auto flex flex-wrap justify-end gap-1">
          {strategy.is_recommended && <TokenBadge token="alive" label="Recommended" glyph="★" size="sm" title="Highest score among actionable strategies (backend)" />}
          {highest && !strategy.is_recommended && <TokenBadge token="dormant" label="Highest score" glyph="▲" size="sm" />}
          {highest && strategy.is_recommended && <TokenBadge token="alive" label="Top score" glyph="▲" appearance="outlined" size="sm" />}
          {!strategy.is_actionable && <TokenBadge token="ghost" label="Not actionable" glyph="✕" size="sm" />}
          {approved && <TokenBadge token="alive" label="Approved" glyph="🔒" pulse size="sm" />}
          {locked && <TokenBadge token="neutral" label="Locked" glyph="🔒" appearance="outlined" size="sm" />}
        </span>
      </div>

      <div className="mt-4 flex items-center gap-4">
        <ScoreGauge score={view.score} decimals={view.scoreDecimals} label={`${strategy.strategy_type} Score(S_k)`} dim={locked} />
        <div className="min-w-0 flex-1">
          <h3 id={headingId} className="font-display text-base font-semibold leading-snug tracking-wide text-ink">
            {strategy.title}
          </h3>
          <p className="mt-1 text-xs text-ink-muted">{meta.blurb}</p>
        </div>
      </div>

      <p className="mt-3 text-sm text-ink/85">{strategy.summary}</p>

      <dl className="mt-4 grid grid-cols-3 gap-2 font-mono tabular-nums">
        {view.terms.map((term) => (
          <div key={term.key} className="rounded border border-line bg-obsidian/60 px-2 py-1.5">
            <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">{term.symbol}</dt>
            <dd className="text-sm text-ink">{formatDecimal(term.raw)}</dd>
          </div>
        ))}
      </dl>
      <TermBar view={view} className="mt-3" />

      <p className="mt-3 font-mono text-[11px] text-ink-muted">
        {strategy.action_plan.items.length} item(s) · {strategy.action_plan.files.length} file(s) ·{" "}
        {strategy.action_plan.deferred.length} deferred
      </p>

      <div className="mt-auto pt-4">
        <button
          type="button"
          aria-pressed={selected}
          aria-describedby={headingId}
          onClick={() => onSelect(strategy.id)}
          className={cx(
            "w-full rounded-md border px-3 py-2 font-display text-xs font-semibold uppercase tracking-[0.15em] transition-colors",
            selected
              ? "border-emerald-bio/70 bg-emerald-bio/15 text-white"
              : "border-line bg-slate-raised text-ink-muted hover:border-emerald-bio/50 hover:text-ink",
          )}
        >
          {selected ? "● Under review" : `Review ${meta.verb}`}
        </button>
      </div>
    </article>
  );
}
