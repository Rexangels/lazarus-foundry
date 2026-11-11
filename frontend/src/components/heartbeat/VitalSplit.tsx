"use client";

/**
 * Before / after pytest: the baseline on the default branch and the verified run on the revival
 * branch (lib/heartbeat.ts pytestComparison), each with its totals, failing node ids, ECG trace
 * and the raw output colored line by line (classifyPytestLine), collapsible.
 */
import { useMemo, useState } from "react";

import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { cx } from "@/components/ui/cx";
import { classifyPytestLine, type PytestComparison, type PytestLineKind, type PytestSummary } from "@/lib/heartbeat";
import { totalsSentence, vitalTrace } from "@/lib/heartbeatView";

const LINE_CLASS: Record<PytestLineKind, string> = {
  pass: "text-alive-text",
  fail: "text-ghost-text",
  error: "text-ghost-text",
  skip: "text-dormant-text",
  "summary-pass": "bg-emerald-bio/10 font-semibold text-alive-text",
  "summary-fail": "bg-ghost/10 font-semibold text-ghost-text",
  section: "text-ink-muted",
  plain: "text-ink/80",
};

function RawOutput({ output, label }: { output: string; label: string }) {
  const [open, setOpen] = useState(false);
  const lines = useMemo(() => (output ? output.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n") : []), [output]);
  if (!lines.length) return <p className="font-mono text-[11px] text-ink-muted">No raw output was stored.</p>;
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="group rounded-md border border-line bg-obsidian">
      <summary className="cursor-pointer select-none px-3 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-ink-muted hover:text-ink">
        <span aria-hidden className="mr-2 inline-block transition-transform group-open:rotate-90">▸</span>
        Raw pytest output ({lines.length} lines)
      </summary>
      {open && (
        <div role="region" aria-label={label} tabIndex={0} className="max-h-96 overflow-auto border-t border-line py-2 font-mono text-[11px] leading-5">
          {lines.map((line, i) => (
            <div key={i} className={cx("flex gap-3 px-3", LINE_CLASS[classifyPytestLine(line)])}>
              <span aria-hidden className="w-8 shrink-0 select-none text-right text-ink-muted/50">
                {i + 1}
              </span>
              <span className="whitespace-pre-wrap break-all">{line || " "}</span>
            </div>
          ))}
        </div>
      )}
    </details>
  );
}

function Vital({ summary, fixed, targetOutcomes }: { summary: PytestSummary; fixed?: string[]; targetOutcomes?: Record<string, string> }) {
  const trace = vitalTrace(summary);
  const t = summary.totals;
  const before = summary.phase === "before";
  const healthy = summary.allPassing === true;
  const outcomes = Object.entries(targetOutcomes ?? {});
  const headingId = `vital-${summary.phase}-heading`;
  return (
    <section
      aria-labelledby={headingId}
      className={cx(
        "relative flex min-w-0 flex-col gap-4 rounded-lg border p-5",
        healthy ? "border-emerald-bio/60 bg-emerald-bio/5 shadow-glow-alive" : "border-ghost/50 bg-ghost/5",
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted">
            {before ? "Before · baseline" : "After · verified"}
          </p>
          <h3 id={headingId} className="mt-1 font-display text-base font-semibold uppercase tracking-[0.12em] text-ink">
            {before ? "Default branch" : "Revival branch"}{" "}
            {summary.branch && <code className="font-mono text-sm normal-case tracking-normal text-ink-muted">{summary.branch}</code>}
          </h3>
        </div>
        <span
          className={cx(
            "rounded border px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider",
            healthy ? "border-emerald-bio/70 text-alive-text" : "border-ghost/60 text-ghost-text",
          )}
        >
          <span aria-hidden className="mr-1">{healthy ? "✓" : "✕"}</span>
          {healthy ? "All passing" : t ? "Failing" : "No result"}
        </span>
      </header>

      <div className="flex items-end gap-3">
        <p className={cx("font-mono text-5xl font-semibold tabular-nums leading-none", healthy ? "text-alive-text" : "text-ghost-text")}>
          {t ? t.passed : "?"}
          <span className="text-2xl text-ink-muted">/{t ? t.tests : "?"}</span>
        </p>
        <p className="pb-1 text-sm text-ink-muted">{totalsSentence(t)}</p>
      </div>

      <dl className="grid grid-cols-4 gap-2 font-mono text-[11px]">
        {(
          [
            ["tests", t?.tests, "text-ink"],
            ["passed", t?.passed, "text-alive-text"],
            ["failed", t?.failed, t?.failed ? "text-ghost-text" : "text-ink-muted"],
            ["errors", t?.errors, t?.errors ? "text-ghost-text" : "text-ink-muted"],
          ] as const
        ).map(([label, value, tone]) => (
          <div key={label} className="rounded border border-line bg-obsidian/60 px-2 py-1">
            <dt className="uppercase tracking-[0.18em] text-ink-muted">{label}</dt>
            <dd className={cx("text-lg tabular-nums", tone)}>{value ?? "?"}</dd>
          </div>
        ))}
      </dl>

      <HeartbeatLine
        state={trace.state}
        token={trace.token}
        period={trace.state === "erratic" ? 1.5 : 2.4}
        className="h-14"
      />
      <p className="-mt-2 font-mono text-[11px] text-ink-muted">{trace.label}</p>

      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Failing tests</p>
        {summary.failed.length ? (
          <ul className="mt-1 space-y-1">
            {summary.failed.map((id) => (
              <li key={id} className="break-all font-mono text-xs text-ghost-text">
                <span aria-hidden className="mr-1">✕</span>
                {id}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 font-mono text-xs text-ink-muted">None</p>
        )}
      </div>

      {fixed && fixed.length > 0 && (
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Fixed by the revival</p>
          <ul className="mt-1 space-y-1">
            {fixed.map((id) => (
              <li key={id} className="break-all font-mono text-xs text-alive-text">
                <span aria-hidden className="mr-1">✓</span>
                {id}
              </li>
            ))}
          </ul>
        </div>
      )}

      {outcomes.length > 0 && (
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Target tests</p>
          <ul className="mt-1 space-y-1">
            {outcomes.map(([target, outcome]) => (
              <li key={target} className="flex flex-wrap items-baseline gap-2 font-mono text-xs">
                <span className={outcome === "PASSED" ? "text-alive-text" : "text-ghost-text"}>
                  <span aria-hidden>{outcome === "PASSED" ? "✓ " : "✕ "}</span>
                  {outcome}
                </span>
                <span className="break-all text-ink">{target}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-auto">
        <RawOutput output={summary.output} label={`${before ? "Baseline" : "Verified"} pytest output`} />
      </div>
    </section>
  );
}

export function VitalSplit({ comparison }: { comparison: PytestComparison }) {
  const { before, after } = comparison;
  const delta = before.totals && after.totals ? after.totals.tests - before.totals.tests : null;
  return (
    <div className="grid items-stretch gap-4 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
      <Vital summary={before} />
      <div aria-hidden className="flex items-center justify-center gap-2 lg:flex-col">
        <span className="font-display text-2xl text-alive-text">→</span>
        {delta !== null && (
          <span className="font-mono text-[11px] text-ink-muted">
            {delta >= 0 ? `+${delta}` : delta} tests
          </span>
        )}
        <span className="font-mono text-[11px] text-ink-muted">{comparison.fixed.length} fixed</span>
      </div>
      <Vital summary={after} fixed={comparison.fixed} targetOutcomes={comparison.targetOutcomes} />
    </div>
  );
}
