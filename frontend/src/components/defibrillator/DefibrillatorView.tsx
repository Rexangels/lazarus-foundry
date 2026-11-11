"use client";

/**
 * View 2, The Defibrillator: the Restore / Modernize / Extract deck with each Score(S_k) breakdown,
 * the side-by-side comparison, the work order, the `.kiro` spec preview and the Human Approval Gate,
 * plus the loading, pending, unavailable, error and offline-demo states.
 */
import Link from "next/link";
import { useCallback, useMemo, useReducer, useState } from "react";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Button, ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { Panel } from "@/components/ui/Panel";
import { ProjectStatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { MOCK_PROJECT_ID } from "@/lib/api";
import {
  GATE_IDLE,
  STRATEGY_META,
  defaultSelection,
  featureTitles,
  formatScore,
  gateAvailability,
  gateReducer,
  generatedFromFrames,
  highestScoreIds,
  isPastApproval,
  readScoreBreakdown,
  sequenceStep,
  type GatePhase,
} from "@/lib/defibrillator";
import type { ApprovalOut, ProjectStatus, StrategiesOut } from "@/lib/types";

import { ApprovalGate } from "./ApprovalGate";
import { ComparisonMatrix } from "./ComparisonMatrix";
import { ScoreFormula } from "./ScoreFormula";
import { SpecPreview } from "./SpecPreview";
import { StrategyCard } from "./StrategyCard";
import { WorkOrder } from "./WorkOrder";
import { useStrategies } from "./useStrategies";

const METAPHOR =
  "Three ways to restart the heart: Restore, Modernize or Extract, each scored from 0 to 100. Charge, clear, shock: nothing changes until a human approves one.";

const SEQUENCE = [
  { label: "Charge", detail: "Review the strategies and their evidence" },
  { label: "Clear", detail: "Arm the approval gate" },
  { label: "Shock", detail: "Approve and start the Foundry" },
] as const;

/** The charge → clear → shock sequence, lit up to the current step. */
function ChargeSequence({ step }: { step: 0 | 1 | 2 | 3 }) {
  return (
    <ol aria-label="Defibrillation sequence" className="grid gap-2 sm:grid-cols-3">
      {SEQUENCE.map((s, i) => {
        const done = step > i;
        const current = step === i;
        return (
          <li
            key={s.label}
            aria-current={current ? "step" : undefined}
            className={cx(
              "flex items-center gap-3 rounded-md border px-3 py-2",
              done ? "border-emerald-bio/60 bg-emerald-bio/10" : current ? "border-dormant/60 bg-dormant/10" : "border-line bg-slate-core/70",
            )}
          >
            <span
              aria-hidden
              className={cx(
                "flex h-7 w-7 shrink-0 items-center justify-center rounded-full border font-mono text-xs",
                done ? "border-emerald-bio text-alive-text" : current ? "border-dormant text-dormant-text motion-safe:animate-vital-pulse" : "border-line text-ink-muted",
              )}
            >
              {done ? "✓" : `0${i + 1}`}
            </span>
            <span className="min-w-0">
              <span className={cx("block font-display text-sm font-semibold uppercase tracking-[0.2em]", done ? "text-alive-text" : current ? "text-dormant-text" : "text-ink-muted")}>
                {s.label}
              </span>
              <span className="block text-[11px] text-ink-muted">{s.detail}</span>
            </span>
            <span className="sr-only">{done ? "(done)" : current ? "(current)" : ""}</span>
          </li>
        );
      })}
    </ol>
  );
}

function SynthesisLine({ data }: { data: StrategiesOut }) {
  const s = data.synthesis;
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-ink-muted">
      <ProjectStatusBadge status={data.project.status} size="sm" />
      <span>{data.project.name}</span>
      {s ? (
        <>
          <span>spec prose {s.mode}</span>
          {s.model_id && <span>model {s.model_id}</span>}
          {s.validation.fallback_reason && <span className="text-dormant-text">fallback: {s.validation.fallback_reason}</span>}
          {s.preserved_strategy_types.length > 0 && <span>preserved {s.preserved_strategy_types.join(", ")}</span>}
        </>
      ) : (
        <span>No synthesis record.</span>
      )}
      <span>default branch {data.project.default_branch || "?"}</span>
    </p>
  );
}

function ReadyDeck({
  data,
  status,
  reload,
  approved,
}: {
  data: StrategiesOut;
  status: ProjectStatus | null;
  reload: () => void;
  approved: (result: ApprovalOut) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(() => defaultSelection(data));
  const [gate, dispatch] = useReducer(gateReducer, GATE_IDLE as GatePhase);
  const [lastResult, setLastResult] = useState<ApprovalOut | null>(null);

  const strategies = data.strategies;
  const selected = strategies.find((s) => s.id === selectedId) ?? strategies.find((s) => s.id === defaultSelection(data)) ?? strategies[0];
  const highest = useMemo(() => highestScoreIds(strategies), [strategies]);
  const titles = useMemo(() => featureTitles(strategies), [strategies]);
  const approvedStrategy = strategies.find((s) => s.approval.approved_by_human) ?? null;

  const select = useCallback(
    (id: string) => {
      if (id === selected?.id) return;
      if (gate.phase === "shocking") return; // never switch while the request is in flight
      setSelectedId(id);
      dispatch({ type: "reset" });
    },
    [gate.phase, selected?.id],
  );

  const onApproved = useCallback(
    (result: ApprovalOut) => {
      setLastResult(result);
      approved(result);
    },
    [approved],
  );

  if (!selected) {
    return (
      <Panel eyebrow="Strategies" title="No strategies" headingId="strategies-empty-heading" tone="ghost">
        <p className="text-ink">The backend returned an empty strategy list.</p>
      </Panel>
    );
  }

  const availability = gateAvailability(data, selected, status);
  const view = readScoreBreakdown(selected);
  const step = sequenceStep(gate, Boolean(approvedStrategy));

  return (
    <>
      <ChargeSequence step={step} />

      <section aria-labelledby="deck-heading" className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted">Strategy matrix</p>
            <h2 id="deck-heading" className="mt-1 font-display text-lg font-semibold tracking-wide text-ink">
              Revival strategies
            </h2>
          </div>
          <SynthesisLine data={data} />
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          {strategies.map((strategy) => (
            <StrategyCard
              key={strategy.id}
              strategy={strategy}
              selected={strategy.id === selected.id}
              highest={highest.has(strategy.id)}
              availability={gateAvailability(data, strategy, status)}
              onSelect={select}
            />
          ))}
        </div>
      </section>

      <Panel eyebrow="Side by side" title="Compare the terms" headingId="compare-heading">
        <ComparisonMatrix strategies={strategies} selectedId={selected.id} onSelect={select} />
      </Panel>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="space-y-6">
          <Panel
            eyebrow={`${selected.strategy_type} · explainable score`}
            title={
              <span>
                Score(S<sub>k</sub>) = <span className="font-mono">{formatScore(view.score, view.scoreDecimals)}</span>
              </span>
            }
            headingId="breakdown-heading"
            tone="alive"
          >
            <ScoreFormula view={view} featureTitles={titles} />
          </Panel>
          <Panel eyebrow={`${selected.strategy_type} · work order`} title={`${STRATEGY_META[selected.strategy_type].verb}: what the Foundry would do`} headingId="work-order-heading">
            <WorkOrder strategy={selected} />
          </Panel>
        </div>
        <div className="xl:sticky xl:top-20 xl:self-start">
          <SpecPreview key={selected.id} strategy={selected} />
        </div>
      </div>

      <ApprovalGate
        strategy={selected}
        availability={availability}
        gate={gate}
        dispatch={dispatch}
        lastResult={lastResult}
        onApproved={onApproved}
        onReload={reload}
        onSelect={select}
      />
    </>
  );
}

function PendingDeck({ detail }: { detail: string }) {
  const { telemetry } = useProjectSession();
  const generated = generatedFromFrames(telemetry.frames);
  return (
    <Panel eyebrow="Strategy matrix" title="Charging the capacitors…" headingId="strategies-pending-heading" tone="conflict">
      <div aria-live="polite" className="space-y-4">
        <p className="text-sm text-ink-muted">{detail}</p>
        <HeartbeatLine state="flat" token="conflict" label="No strategies yet" className="h-10" />
        <ul className="grid gap-3 sm:grid-cols-3">
          {(["RESTORE", "MODERNIZE", "EXTRACT"] as const).map((type) => {
            const frame = generated.find((g) => g.strategyType === type);
            return (
              <li
                key={type}
                className={cx(
                  "rounded-md border p-3 transition-colors",
                  frame ? "border-emerald-bio/50 bg-emerald-bio/5 motion-safe:animate-feed-in" : "border-dashed border-line",
                )}
              >
                <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted">
                  <span aria-hidden className="mr-1 text-ink">{STRATEGY_META[type].glyph}</span>
                  {type}
                </p>
                {frame ? (
                  <>
                    <p className="mt-1 text-sm text-ink">{frame.title}</p>
                    <p className="mt-1 font-mono text-lg tabular-nums text-alive-text">{formatScore(frame.score)}</p>
                  </>
                ) : (
                  <p className="mt-1 text-sm text-ink-muted">Waiting for STRATEGY_GENERATED…</p>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </Panel>
  );
}

export function DefibrillatorView() {
  const { api, projectId, project } = useProjectSession();
  const { state, reload, approved } = useStrategies();
  const status = project?.status ?? (state.phase === "ready" ? state.data.project.status : null);
  const approvedReady = state.phase === "ready" && state.data.strategies.some((s) => s.approval.approved_by_human);

  return (
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">
      <ViewHeader index="02" title="The Defibrillator" metaphor={METAPHOR}>
        {(approvedReady || isPastApproval(status)) && (
          <ButtonLink href={`/foundry/${projectId}/heartbeat`} variant="secondary" size="md">
            Open the Heartbeat <span aria-hidden>→</span>
          </ButtonLink>
        )}
      </ViewHeader>

      {api.mode === "mock" && (
        <p className="flex flex-wrap items-center gap-2 rounded-md border border-conflict/40 bg-conflict/10 px-4 py-2 text-sm text-ink">
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-conflict-text">Offline demo</span>
          The recorded PayPulse strategies: real backend output (plans, Score(S_k) breakdowns, spec bundles and approval
          tokens). Approving here is kept in this browser tab only.
        </p>
      )}

      {state.phase === "loading" && (
        <Panel eyebrow="Strategies" title="Charging…" headingId="strategies-loading-heading">
          <div aria-busy="true" aria-live="polite">
            <HeartbeatLine state="flat" label="Loading the strategies" className="h-12" />
            <p className="mt-2 font-mono text-xs text-ink-muted">Requesting the revival strategies from the backend.</p>
          </div>
        </Panel>
      )}

      {state.phase === "pending" && (
        <>
          <ResurrectionMonitor defibrillatorLink={false} />
          <PendingDeck detail={state.notReady.detail} />
          {state.notReady.code === "STRATEGIES_PENDING" && (
            <p className="text-sm text-ink-muted">
              The autopsy is still running.{" "}
              <Link href={`/foundry/${projectId}/autopsy`} className="text-ink underline decoration-line underline-offset-4 hover:text-white">
                Watch the genome materialize
              </Link>
              .
            </p>
          )}
        </>
      )}

      {state.phase === "unavailable" && (
        <>
          <Panel eyebrow="Strategies" title="No strategies to show" headingId="strategies-unavailable-heading" tone="ghost">
            <div className="space-y-3">
              <p className="flex flex-wrap items-center gap-2 text-ink">
                <ProjectStatusBadge status={state.notReady.status} />
                {state.notReady.detail}
              </p>
              {state.notReady.status_detail && (
                <p className="rounded border border-line bg-obsidian/70 px-3 py-2 font-mono text-xs text-ink-muted">
                  {state.notReady.status_detail}
                </p>
              )}
              <p className="text-sm text-ink-muted">
                The genome is intact. Strategy generation runs again when the genome is re-synthesized.
              </p>
              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" size="sm" onClick={reload}>
                  Check again
                </Button>
                <ButtonLink href={`/foundry/${projectId}/autopsy`} variant="ghost" size="sm">
                  Back to the Autopsy
                </ButtonLink>
              </div>
            </div>
          </Panel>
          <ResurrectionMonitor compact defibrillatorLink={false} />
        </>
      )}

      {state.phase === "error" && (
        <Panel eyebrow="Strategies" title="The strategies could not be read" headingId="strategies-error-heading" tone="ghost">
          <div role="alert" className="space-y-3">
            <p className="text-ink">{state.message}</p>
            <div className="flex flex-wrap gap-3">
              <Button variant="secondary" size="sm" onClick={reload}>
                Retry
              </Button>
              {state.offline && api.mode === "http" && (
                <ButtonLink href={`/foundry/${MOCK_PROJECT_ID}/defibrillator`} variant="ghost" size="sm">
                  Open the offline demo
                </ButtonLink>
              )}
            </div>
          </div>
        </Panel>
      )}

      {state.phase === "ready" && (
        <>
          <ResurrectionMonitor compact defibrillatorLink={false} />
          <ReadyDeck data={state.data} status={status} reload={reload} approved={approved} />
        </>
      )}
    </div>
  );
}
