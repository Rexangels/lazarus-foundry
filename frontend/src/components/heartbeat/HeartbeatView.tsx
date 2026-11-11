"use client";

/**
 * View 3, The Heartbeat: the Foundry's four agents and the live sandbox terminal while the patient
 * is worked on, then the rebirth: the before/after pytest split, the PR, the unified diff and the
 * Continuity Memory. States: not approved yet, queued or running, REBORN, failed, unavailable,
 * error, and the offline-demo banner. Every number on screen comes from the backend's frames or
 * the stored artifact.
 */
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Button, ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { Panel } from "@/components/ui/Panel";
import { ProjectStatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { MOCK_PROJECT_ID } from "@/lib/api";
import { shortSha } from "@/lib/autopsy";
import { pipelineState, pytestComparison, readContinuityMemory, rebirthFileTotals, type PipelineState } from "@/lib/heartbeat";
import { heartbeatPhase, totalsSentence, type HeartbeatPhase } from "@/lib/heartbeatView";
import type { ProjectStatus, RebirthOut, TelemetryFrame } from "@/lib/types";

import { ContinuityMemoryPanel } from "./ContinuityMemoryPanel";
import { DiffViewer } from "./DiffViewer";
import { LiveTerminal } from "./LiveTerminal";
import { PipelineProgress } from "./PipelineProgress";
import { PrSummary } from "./PrSummary";
import { VitalSplit } from "./VitalSplit";
import { useRebirth } from "./useRebirth";

const METAPHOR =
  "Four agents work on the patient in a sandbox until pytest comes back green. Then the flatline becomes a heartbeat, and the verified diff, the PR and the continuity roadmap are handed over.";

const STAGE_WORD: Record<string, string> = {
  ENVIRONMENT: "Environment",
  ARCHITECTURE: "Architecture",
  ENGINEERING: "Engineering",
  VERIFICATION: "Verification",
};

/** One polite sentence per phase or stage change, for screen readers. */
function announcement(phase: HeartbeatPhase, pipeline: PipelineState, rebirth: RebirthOut | null): string {
  switch (phase) {
    case "reborn": {
      const after = readContinuityMemory(rebirth?.continuity_memory).verification?.after ?? pipeline.verification?.totals ?? null;
      return `Reborn. ${totalsSentence(after)}.`;
    }
    case "live":
      if (pipeline.outcome === "verified" || pipeline.outcome === "reborn") return "Verification passed. Writing the rebirth artifact.";
      if (pipeline.healing && pipeline.current === "ENGINEERING") return `Self-healing iteration ${pipeline.healing.iteration}.`;
      return pipeline.current ? `Foundry running: ${STAGE_WORD[pipeline.current]} agent.` : "Foundry queued.";
    case "failed":
      return "The Foundry run failed.";
    default:
      return "";
  }
}

/** frames of one Foundry run (and frames that name no run). */
function framesOfRun(frames: readonly TelemetryFrame[], runId: string | null): TelemetryFrame[] {
  if (!runId) return frames as TelemetryFrame[];
  return frames.filter((f) => {
    const id = f.payload.foundry_run_id;
    return typeof id !== "string" || !id || id === runId;
  });
}

// ---------------------------------------------------------------------------
// Live: the monitor next to the terminal
// ---------------------------------------------------------------------------
function LiveMonitor({ pipeline, detail }: { pipeline: PipelineState; detail: string }) {
  const baseline = pipeline.baseline;
  const verification = pipeline.verification;
  const verified = pipeline.outcome === "verified" || pipeline.outcome === "reborn" || (verification?.passed ?? false);
  const baselineFailing = baseline ? !baseline.passed : false;
  const trace: { state: "flat" | "erratic" | "pulse"; token: "alive" | "ghost" | "neutral" | "dormant"; label: string } = verified
    ? { state: "pulse", token: "alive", label: "Steady rhythm: verification passed" }
    : pipeline.healing
      ? { state: "erratic", token: "dormant", label: "Arrhythmia: self-healing in progress" }
      : baselineFailing
        ? { state: "erratic", token: "ghost", label: "Arrhythmia: the baseline has failing tests" }
        : { state: "flat", token: "neutral", label: "Flatline: no verified run yet" };
  return (
    <Panel eyebrow="Vital signs" title={verified ? "Pulse detected" : "On the table"} headingId="live-vitals-heading" tone={verified ? "alive" : "conflict"} glow={verified}>
      <div className="space-y-4">
        <HeartbeatLine state={trace.state} token={trace.token} period={trace.state === "erratic" ? 1.4 : 2.4} className="h-20" />
        <p className="font-mono text-[11px] text-ink-muted">{trace.label}</p>
        <dl className="space-y-3">
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Baseline (default branch)</dt>
            <dd className={cx("mt-0.5 font-mono text-sm", baseline ? (baseline.passed ? "text-alive-text" : "text-ghost-text") : "text-ink-muted")}>
              {baseline ? totalsSentence(baseline.totals) : "Not run yet"}
            </dd>
            {baseline && baseline.failed.length > 0 && (
              <dd className="mt-1 break-all font-mono text-[11px] text-ghost-text">
                {baseline.failed.map((id) => (
                  <span key={id} className="block">
                    <span aria-hidden>✕ </span>
                    {id}
                  </span>
                ))}
              </dd>
            )}
          </div>
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">
              Verification{pipeline.verificationAttempts > 1 ? ` (attempt ${pipeline.verificationAttempts})` : ""}
            </dt>
            <dd className={cx("mt-0.5 font-mono text-sm", verification ? (verification.passed ? "text-alive-text" : "text-ghost-text") : "text-ink-muted")}>
              {verification ? totalsSentence(verification.totals) : "Not run yet"}
            </dd>
          </div>
        </dl>
        <p aria-live="polite" className="rounded border border-line bg-obsidian/70 px-3 py-2 text-xs text-ink-muted">
          {pipeline.outcome === "verified" || pipeline.outcome === "reborn"
            ? "Verification passed. Committing the revival branch and writing the rebirth artifact…"
            : detail || "The rebirth artifact is written after verification passes."}
        </p>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// REBORN
// ---------------------------------------------------------------------------
function RebornHero({ rebirth, celebrate, headingRef }: { rebirth: RebirthOut; celebrate: boolean; headingRef: RefObject<HTMLHeadingElement | null> }) {
  const memory = useMemo(() => readContinuityMemory(rebirth.continuity_memory), [rebirth.continuity_memory]);
  const comparison = useMemo(() => pytestComparison(rebirth), [rebirth]);
  const totals = useMemo(() => rebirthFileTotals(rebirth.files), [rebirth.files]);
  const passed = rebirth.tests_passed && comparison.after.allPassing !== false;
  const before = comparison.before.totals;
  const after = comparison.after.totals;
  const name = memory.project?.name || rebirth.project.name;
  const v = memory.verification;

  return (
    <section
      aria-labelledby="reborn-heading"
      className={cx(
        "relative overflow-hidden rounded-xl border px-6 py-8 sm:px-10 sm:py-10",
        passed ? "border-emerald-bio/70 bg-slate-core shadow-glow-alive" : "border-ghost/60 bg-slate-core",
      )}
    >
      <div aria-hidden className={cx("pointer-events-none absolute inset-0", passed && "bio-glow")} />
      <div aria-hidden className="scanlines pointer-events-none absolute inset-0" />
      {celebrate && passed && <div aria-hidden className="pointer-events-none absolute inset-0 bg-emerald-bio opacity-0 motion-safe:animate-shock-flash" />}

      <div className="relative space-y-6">
        <p className="font-mono text-[11px] uppercase tracking-[0.3em] text-ink-muted">
          {name} · {rebirth.strategy_type} · rebirth {shortSha(rebirth.rebirth_commit_sha)}
        </p>
        <h2
          id="reborn-heading"
          ref={headingRef}
          tabIndex={-1}
          className={cx(
            "font-display text-6xl font-bold uppercase leading-none tracking-[0.3em] outline-none sm:text-8xl",
            passed ? "text-alive-text [text-shadow:0_0_32px_rgb(16_185_129/0.55)]" : "text-ghost-text",
            celebrate && "motion-safe:animate-reborn-in",
          )}
        >
          {passed ? "Reborn" : "Rebirth stored"}
        </h2>
        <HeartbeatLine
          state={passed ? "pulse" : "erratic"}
          token={passed ? "alive" : "ghost"}
          period={1.6}
          className="h-24 sm:h-28"
          label={passed ? `Steady heartbeat: ${totalsSentence(after)}` : `Irregular: ${totalsSentence(after)}`}
        />
        <p className="max-w-3xl text-base text-ink sm:text-lg">
          {passed ? (
            <>
              <strong className="font-semibold text-alive-text">{totalsSentence(after)}</strong> on{" "}
              <code className="font-mono text-sm text-ink">{rebirth.revival_branch}</code>. Before the revival:{" "}
              <span className="text-ghost-text">{totalsSentence(before)}</span>.
            </>
          ) : (
            <>The artifact was stored, but its verification does not show a passing suite: {totalsSentence(after)}.</>
          )}
        </p>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Tests</dt>
            <dd className="mt-1 font-mono text-2xl tabular-nums">
              <span className="text-ghost-text">{before ? `${before.passed}/${before.tests}` : "?"}</span>
              <span aria-hidden className="mx-2 text-ink-muted">→</span>
              <span className="sr-only"> to </span>
              <span className="text-alive-text">{after ? `${after.passed}/${after.tests}` : "?"}</span>
            </dd>
          </div>
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Diff</dt>
            <dd className="mt-1 font-mono text-2xl tabular-nums">
              {totals.files} <span className="text-sm text-ink-muted">files</span> <span className="text-alive-text">+{totals.additions}</span>{" "}
              <span className="text-ghost-text">−{totals.deletions}</span>
            </dd>
          </div>
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Self-healing</dt>
            <dd className="mt-1 font-mono text-2xl tabular-nums text-ink">
              {rebirth.iterations_used}
              {v && <span className="text-sm text-ink-muted"> of {v.maxHealingIterations} iterations</span>}
            </dd>
          </div>
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Tests fixed</dt>
            <dd className="mt-1 font-mono text-2xl tabular-nums text-alive-text">{comparison.fixed.length}</dd>
          </div>
        </dl>
        <p className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-ink-muted">
          <span>
            spec <code className="text-ink">{rebirth.exported_kiro_specs_path || "?"}</code>
          </span>
          <span title={rebirth.base_sha}>base {shortSha(rebirth.base_sha)}</span>
          {rebirth.preview_url && <span>preview {rebirth.preview_url} (served only while the sandbox preview runs)</span>}
          <span>stored {rebirth.created_at}</span>
        </p>
      </div>
    </section>
  );
}

function RebornScreen({
  rebirth,
  pipeline,
  frames,
  celebrate,
  headingRef,
}: {
  rebirth: RebirthOut;
  pipeline: PipelineState;
  frames: readonly TelemetryFrame[];
  celebrate: boolean;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  const comparison = useMemo(() => pytestComparison(rebirth, pipeline), [rebirth, pipeline]);
  return (
    <>
      <RebornHero rebirth={rebirth} celebrate={celebrate} headingRef={headingRef} />

      <Panel eyebrow="Vital signs · pytest" title="Before and after" headingId="vital-split-heading" tone="alive">
        <VitalSplit comparison={comparison} />
      </Panel>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <Panel eyebrow="Pull request" title="What Lazarus hands over" headingId="pr-heading" tone="conflict" className="xl:self-start">
          <PrSummary rebirth={rebirth} />
        </Panel>
        <Panel eyebrow="Unified diff" title={`${rebirth.revival_branch} vs ${shortSha(rebirth.base_sha)}`} headingId="diff-heading">
          <DiffViewer patch={rebirth.unified_diff_patch} files={rebirth.files} />
        </Panel>
      </div>

      <Panel eyebrow="Continuity Memory" title="Roadmap for the next caretaker" headingId="memory-heading" tone="dormant">
        <ContinuityMemoryPanel memory={rebirth.continuity_memory} />
      </Panel>

      <Panel eyebrow="Foundry record" title="The four agents" headingId="foundry-record-heading">
        <div className="space-y-5">
          <PipelineProgress pipeline={pipeline} />
          <LiveTerminal frames={frames} live={false} />
        </div>
      </Panel>
    </>
  );
}

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------
export function HeartbeatView() {
  const { api, projectId, project, telemetry } = useProjectSession();
  const { state, reload } = useRebirth();
  const notReady = state.phase === "pending" || state.phase === "unavailable" ? state.notReady : null;
  const rebirth = state.phase === "ready" ? state.data : null;
  const status: ProjectStatus | null = project?.status ?? notReady?.status ?? rebirth?.project.status ?? null;
  const statusDetail = project?.status_detail || notReady?.status_detail || "";
  const maxIterations = rebirth ? readContinuityMemory(rebirth.continuity_memory).verification?.maxHealingIterations ?? null : null;

  const pipeline = useMemo(
    () =>
      pipelineState({
        frames: telemetry.frames,
        status,
        statusDetail,
        foundryRunId: rebirth?.foundry_run_id,
        fallbackMaxIterations: maxIterations,
      }),
    [telemetry.frames, status, statusDetail, rebirth?.foundry_run_id, maxIterations],
  );
  const runFrames = useMemo(() => framesOfRun(telemetry.frames, pipeline.foundryRunId), [telemetry.frames, pipeline.foundryRunId]);
  const phase = heartbeatPhase({ rebirth: state.phase, status, outcome: pipeline.outcome });

  // The REBORN moment plays (and takes focus) only when this page saw the patient on the table.
  const sawLive = useRef(false);
  const [celebrate, setCelebrate] = useState(false);
  const rebornHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (phase === "live") sawLive.current = true;
    if (phase === "reborn" && sawLive.current) {
      sawLive.current = false;
      setCelebrate(true);
      requestAnimationFrame(() => rebornHeading.current?.focus({ preventScroll: false }));
    }
  }, [phase]);

  const live = phase === "live";
  const said = announcement(phase, pipeline, rebirth);

  return (
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">
      <ViewHeader index="03" title="The Heartbeat" metaphor={METAPHOR}>
        {status && <ProjectStatusBadge status={status} />}
      </ViewHeader>

      <p role="status" className="sr-only">
        {said}
      </p>

      {api.mode === "mock" && (
        <p className="flex flex-wrap items-center gap-2 rounded-md border border-conflict/40 bg-conflict/10 px-4 py-2 text-sm text-ink">
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-conflict-text">Offline demo</span>
          A replay of the recorded RESTORE run on PayPulse: real backend telemetry (compressed to about 30 seconds) and the
          stored rebirth artifact. Nothing runs in this browser.
        </p>
      )}

      {phase === "loading" && (
        <Panel eyebrow="Heartbeat" title="Listening…" headingId="heartbeat-loading-heading">
          <div aria-busy="true">
            <HeartbeatLine state="flat" className="h-12" />
            <p className="mt-2 font-mono text-xs text-ink-muted">Requesting the rebirth artifact from the backend.</p>
          </div>
        </Panel>
      )}

      {phase === "not-approved" && (
        <>
          <Panel eyebrow="Foundry" title="No strategy approved yet" headingId="heartbeat-idle-heading" tone="neutral">
            <div className="space-y-4">
              <HeartbeatLine state="flat" token="neutral" label="Flatline: the Foundry has not started" className="h-16" />
              <p className="max-w-2xl text-ink">
                The Foundry starts only after a human approves a revival strategy. Nothing has been changed in the
                repository.
              </p>
              {status && status !== "AUTOPSY_COMPLETE" && (
                <p className="text-sm text-ink-muted">The autopsy is still running; the strategies follow it.</p>
              )}
              <ButtonLink href={`/foundry/${projectId}/defibrillator`} variant="charge" size="lg">
                <span aria-hidden>⚡</span> Go to the Defibrillator
              </ButtonLink>
            </div>
          </Panel>
          <ResurrectionMonitor compact defibrillatorLink={false} />
        </>
      )}

      {phase === "live" && (
        <>
          <Panel
            eyebrow="Foundry · 4 agents"
            title={pipeline.outcome === "queued" ? "Queued for the Foundry" : pipeline.outcome === "verified" || pipeline.outcome === "reborn" ? "Verified" : "Operating"}
            headingId="pipeline-heading"
            tone="conflict"
            glow
          >
            <PipelineProgress pipeline={pipeline} />
          </Panel>
          <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <section aria-label="Sandbox terminal">
              <LiveTerminal frames={runFrames} live />
            </section>
            <LiveMonitor pipeline={pipeline} detail={notReady?.detail ?? ""} />
          </div>
        </>
      )}

      {phase === "reborn" && rebirth && (
        <RebornScreen rebirth={rebirth} pipeline={pipeline} frames={runFrames} celebrate={celebrate} headingRef={rebornHeading} />
      )}

      {phase === "failed" && (
        <>
          <Panel eyebrow="Foundry · 4 agents" title="Flatline: the Foundry stopped" headingId="pipeline-failed-heading" tone="ghost">
            <div className="space-y-4">
              <HeartbeatLine state="flat" token="ghost" label="Flatline: the run failed" className="h-14" />
              <PipelineProgress pipeline={pipeline} />
              {statusDetail && (
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Project status detail</p>
                  <p className="mt-1 whitespace-pre-wrap break-words rounded border border-line bg-obsidian/70 px-3 py-2 font-mono text-xs text-ink">
                    {statusDetail}
                  </p>
                </div>
              )}
              {notReady && notReady.code === "REBIRTH_UNAVAILABLE" && <p className="text-sm text-ink-muted">{notReady.detail}</p>}
              <p className="text-sm text-ink-muted">
                The default branch was not changed. The revival branch and the sandbox output below show how far the run got.
              </p>
              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" size="sm" onClick={reload}>
                  Check again
                </Button>
                <ButtonLink href={`/foundry/${projectId}/defibrillator`} variant="ghost" size="sm">
                  Back to the Defibrillator
                </ButtonLink>
              </div>
            </div>
          </Panel>
          <LiveTerminal frames={runFrames} live={false} />
        </>
      )}

      {phase === "unavailable" && notReady && (
        <>
          <Panel eyebrow="Rebirth" title="No rebirth to show" headingId="rebirth-unavailable-heading" tone="ghost">
            <div className="space-y-3">
              <p className="flex flex-wrap items-center gap-2 text-ink">
                <ProjectStatusBadge status={notReady.status} />
                {notReady.detail}
              </p>
              {statusDetail && (
                <p className="whitespace-pre-wrap break-words rounded border border-line bg-obsidian/70 px-3 py-2 font-mono text-xs text-ink-muted">
                  {statusDetail}
                </p>
              )}
              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" size="sm" onClick={reload}>
                  Check again
                </Button>
                <ButtonLink href={`/foundry/${projectId}/defibrillator`} variant="ghost" size="sm">
                  Back to the Defibrillator
                </ButtonLink>
              </div>
            </div>
          </Panel>
          <ResurrectionMonitor compact defibrillatorLink={false} />
        </>
      )}

      {phase === "error" && state.phase === "error" && (
        <Panel eyebrow="Rebirth" title="The rebirth could not be read" headingId="rebirth-error-heading" tone="ghost">
          <div role="alert" className="space-y-3">
            <p className="text-ink">{state.message}</p>
            <div className="flex flex-wrap gap-3">
              <Button variant="secondary" size="sm" onClick={reload}>
                Retry
              </Button>
              {state.offline && api.mode === "http" && (
                <ButtonLink href={`/foundry/${MOCK_PROJECT_ID}/heartbeat`} variant="ghost" size="sm">
                  Open the offline demo
                </ButtonLink>
              )}
            </div>
          </div>
        </Panel>
      )}

      {live && pipeline.fromStatusFallback && telemetry.connection !== "open" && telemetry.connection !== "replay" && (
        <p className="text-xs text-ink-muted">The telemetry socket is {telemetry.connection}; the stages follow the polled project status.</p>
      )}
    </div>
  );
}
