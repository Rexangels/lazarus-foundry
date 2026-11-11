"use client";

/**
 * The Continuity Memory (`lazarus-continuity-v1`, apps/rebirth/builder.py): what was revived, how it
 * was verified, what was merged and completed, and the roadmap of deferred steps with their
 * reasons. The raw JSON can be shown and downloaded (a Blob URL, revoked after the click).
 */
import { useCallback, useMemo, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { TokenBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { shortSha } from "@/lib/autopsy";
import { DEFERRAL_REASON_LABEL } from "@/lib/defibrillator";
import { continuityMemoryJson, readContinuityMemory } from "@/lib/heartbeat";
import { memoryFileName, totalsSentence } from "@/lib/heartbeatView";
import type { VitalStatus } from "@/lib/types";

const VITAL_STATUSES: ReadonlySet<string> = new Set(["VERIFIED_WORKING", "ABANDONED_IN_PROGRESS", "GHOST_FEATURE"]);

function Vital({ status }: { status: string }) {
  return VITAL_STATUSES.has(status) ? (
    <VitalBadge status={status as VitalStatus} size="sm" />
  ) : status ? (
    <span className="font-mono text-[10px] text-ink-muted">{status}</span>
  ) : null;
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-2">
      <h3 className="flex items-baseline gap-2 font-display text-sm font-semibold uppercase tracking-[0.16em] text-ink">
        {title}
        {count !== undefined && <span className="font-mono text-[11px] font-normal text-ink-muted">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

function Fact({ label, children, tone }: { label: string; children: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">{label}</dt>
      <dd className={cx("mt-0.5 break-words font-mono text-xs", tone ?? "text-ink")}>{children}</dd>
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="font-mono text-xs text-ink-muted">{children}</p>;
}

export function ContinuityMemoryPanel({ memory }: { memory: Record<string, unknown> }) {
  const m = useMemo(() => readContinuityMemory(memory), [memory]);
  const json = useMemo(() => continuityMemoryJson(memory), [memory]);
  const [showRaw, setShowRaw] = useState(false);
  const fileName = memoryFileName(m);
  const r = m.revival;
  const v = m.verification;

  const download = useCallback(() => {
    const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  }, [json, fileName]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="font-mono text-[11px] text-ink-muted">
          {m.version || "unversioned"}
          {m.generatedAt && ` · generated ${m.generatedAt}`}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" aria-pressed={showRaw} onClick={() => setShowRaw((value) => !value)}>
            {showRaw ? "Hide raw JSON" : "Raw JSON"}
          </Button>
          <Button variant="primary" size="sm" onClick={download} icon="↓">
            Download JSON
          </Button>
        </div>
      </div>

      {showRaw && (
        <pre
          tabIndex={0}
          aria-label="Continuity Memory JSON"
          className="max-h-[28rem] overflow-auto rounded-md border border-line bg-obsidian p-3 font-mono text-[11px] leading-5 text-ink/90"
        >
          {json}
        </pre>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Section title="Revival">
          {r ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
              <Fact label="Strategy">
                {r.strategyType} · {r.title}
              </Fact>
              <Fact label="Feasibility">{r.feasibilityScore === null ? "?" : `${r.feasibilityScore} / 100`}</Fact>
              <Fact label="Approved by">
                {r.approvedBy || "?"}
                {r.approvedAt && <span className="text-ink-muted"> at {r.approvedAt}</span>}
              </Fact>
              <Fact label="Revival branch">{r.revivalBranch}</Fact>
              <Fact label="Base → rebirth">
                <span title={r.baseSha}>{shortSha(r.baseSha)}</span> → <span title={r.rebirthCommitSha}>{shortSha(r.rebirthCommitSha)}</span>
              </Fact>
              <Fact label="Foundry run">{r.foundryRunId.slice(0, 8)}</Fact>
              <Fact label="Exported spec">{r.specPath}</Fact>
              <Fact label="Spec files">{r.specFiles.map((f) => f.split("/").pop()).join(", ") || "none"}</Fact>
            </dl>
          ) : (
            <Empty>No revival section.</Empty>
          )}
        </Section>

        <Section title="Verification">
          {v ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
              <Fact label="Before" tone={v.before && v.before.failed + v.before.errors > 0 ? "text-ghost-text" : undefined}>
                {totalsSentence(v.before)}
              </Fact>
              <Fact label="After" tone={v.after && v.after.failed + v.after.errors === 0 && v.after.tests > 0 ? "text-alive-text" : "text-ghost-text"}>
                {totalsSentence(v.after)}
              </Fact>
              <Fact label="Tests fixed">{v.testsFixed.length}</Fact>
              <Fact label="Verification attempts">{v.verificationAttempts}</Fact>
              <Fact label="Self-healing iterations">
                {v.iterationsUsed} of {v.maxHealingIterations}
              </Fact>
              <Fact label="Python">{v.python || "?"}</Fact>
              <Fact label="Sandbox">{v.sandboxMode || "?"}</Fact>
              <Fact label="Network isolated" tone={v.networkIsolated === false ? "text-dormant-text" : undefined}>
                {v.networkIsolated === null ? "unknown" : v.networkIsolated ? "yes" : "no"}
              </Fact>
              <Fact label="Install resolution">{v.installResolution || "?"}</Fact>
              {v.targetsRunSeparately && <Fact label="Targets run separately">{totalsSentence(v.targetsRunSeparately)}</Fact>}
            </dl>
          ) : (
            <Empty>No verification section.</Empty>
          )}
        </Section>

        <Section title="Completed" count={m.completed.length}>
          {m.completed.length ? (
            <ul className="space-y-2">
              {m.completed.map((item) => (
                <li key={item.itemId || item.title} className="rounded border border-line bg-obsidian/60 px-3 py-2">
                  <p className="flex flex-wrap items-center gap-2 text-sm text-ink">
                    <span aria-hidden className="text-alive-text">✓</span>
                    {item.title}
                  </p>
                  <p className="mt-1 font-mono text-[11px] text-ink-muted">
                    {item.itemId} · {item.kind} · {item.stage}
                    {item.files.length > 0 && ` · ${item.files.join(", ")}`}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No completed plan items.</Empty>
          )}
        </Section>

        <div className="space-y-6">
          <Section title="Merged branches" count={m.mergedBranches.length}>
            {m.mergedBranches.length ? (
              <ul className="space-y-2">
                {m.mergedBranches.map((b) => (
                  <li key={b.branch} className="font-mono text-xs">
                    <span className="text-ink">{b.branch}</span>{" "}
                    <span className="text-ink-muted">
                      @ {shortSha(b.branchHead)} → merge {shortSha(b.mergeCommit)}
                      {b.files.length > 0 && ` · ${b.files.join(", ")}`}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No branches merged.</Empty>
            )}
          </Section>

          <Section title="Features revived" count={m.featuresRevived.length}>
            {m.featuresRevived.length ? (
              <ul className="space-y-2">
                {m.featuresRevived.map((f) => (
                  <li key={f.featureId ?? f.title} className="flex flex-wrap items-center gap-2 text-sm text-ink">
                    <span aria-hidden className="text-alive-text">♥</span>
                    {f.title}
                    <Vital status={f.vitalStatus} />
                    {f.partial && <TokenBadge token="dormant" label="partial" size="sm" appearance="outlined" dashed />}
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No features revived.</Empty>
            )}
          </Section>
        </div>
      </div>

      <Section title="Roadmap: deferred steps" count={m.roadmap.length}>
        {m.roadmap.length ? (
          <ol className="space-y-3">
            {m.roadmap.map((step) => (
              <li key={`${step.step}-${step.title}`} className="flex gap-3 rounded-md border border-dashed border-dormant/50 bg-dormant/5 p-3">
                <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-dormant/70 font-mono text-xs text-dormant-text">
                  {step.step}
                </span>
                <div className="min-w-0 space-y-1">
                  <p className="flex flex-wrap items-center gap-2 text-sm text-ink">
                    <span className="sr-only">Step {step.step}:</span>
                    {step.title}
                    <Vital status={step.vitalStatus} />
                    <TokenBadge token="dormant" label={step.status || "DEFERRED"} size="sm" appearance="outlined" />
                    {step.partial && <TokenBadge token="dormant" label="partial" size="sm" appearance="outlined" dashed />}
                  </p>
                  <p className="font-mono text-[11px] uppercase tracking-wider text-dormant-text">
                    {DEFERRAL_REASON_LABEL[step.reason] ?? (step.reason || "No reason given")}
                  </p>
                  {step.detail && <p className="text-xs text-ink-muted">{step.detail}</p>}
                  {step.externalRequirements.length > 0 && (
                    <p className="font-mono text-[11px] text-ink">Needs: {step.externalRequirements.join("; ")}</p>
                  )}
                  {step.wouldTouch.length > 0 && (
                    <p className="font-mono text-[11px] text-ink-muted">Would touch: {step.wouldTouch.join(", ")}</p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <Empty>Nothing was deferred.</Empty>
        )}
      </Section>

      <Section title="Open markers" count={m.openMarkers.length}>
        {m.openMarkers.length ? (
          <ul className="space-y-1">
            {m.openMarkers.map((marker) => (
              <li key={marker} className="break-words font-mono text-[11px] text-ink">
                <span aria-hidden className="mr-1 text-dormant-text">⚑</span>
                {marker}
              </li>
            ))}
          </ul>
        ) : (
          <Empty>No TODO or FIXME markers left open.</Empty>
        )}
      </Section>
    </div>
  );
}
