"use client";

import { ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { Panel } from "@/components/ui/Panel";
import { Readout, ReadoutGrid } from "@/components/ui/Readout";
import { isInFlight } from "@/lib/progress";

import { PipelineTrack } from "./PipelineTrack";
import { useProjectSession } from "./ProjectSession";
import { TelemetryFeed } from "./TelemetryFeed";

/**
 * Live view of ingestion and genome synthesis: the ECG, the five-step track, forensic counters and
 * the event feed. Shown while a project has no genome yet; later views reuse it as a status strip.
 */
export function ResurrectionMonitor({ compact = false }: { compact?: boolean }) {
  const { project, telemetry, progress, projectId } = useProjectSession();
  const status = project?.status ?? null;
  const alive = telemetry.frames.length > 0 || isInFlight(status) || progress.current > 0;
  const genomeReady = progress.states.genome === "done";
  const strategiesReady = progress.counters.strategies > 0 || progress.states.strategy === "done";
  const { counters } = progress;

  return (
    <Panel
      eyebrow="Vital monitor"
      title={
        progress.failed
          ? "Resurrection failed"
          : genomeReady
            ? "Genome reconstructed"
            : alive
              ? "Resurrection in progress"
              : "Awaiting first heartbeat"
      }
      headingId="vital-monitor-heading"
      tone={progress.failed ? "ghost" : "alive"}
      glow={alive && !progress.failed}
    >
      <HeartbeatLine
        state={alive && !progress.failed ? "pulse" : "flat"}
        token={progress.failed ? "ghost" : "alive"}
        period={genomeReady ? 2.4 : 1.2}
        label={progress.failed ? "Flatline: the pipeline stopped" : alive ? "Heartbeat detected" : "Flatline: no telemetry yet"}
        className="h-20"
      />

      {progress.failure && (
        <p role="alert" className="mt-3 rounded border border-ghost/50 bg-ghost/10 px-3 py-2 font-mono text-xs text-ink">
          <span className="text-ghost-text">{progress.failure.stage ?? "PIPELINE"} ERROR · </span>
          {progress.failure.detail}
        </p>
      )}

      <PipelineTrack states={progress.states} className="mt-6" />

      {!compact && (
        <>
          <ReadoutGrid className="mt-6 sm:grid-cols-6">
            <Readout label="Branches" value={counters.branches} />
            <Readout label="Abandoned" value={counters.abandonedBranches} token={counters.abandonedBranches ? "dormant" : undefined} />
            <Readout label="Files parsed" value={counters.filesParsed} />
            <Readout label="Symbols" value={counters.symbols} />
            <Readout label="Features" value={counters.features} token={counters.features ? "alive" : undefined} />
            <Readout label="Conflicts" value={counters.conflicts} token={counters.conflicts ? "conflict" : undefined} />
          </ReadoutGrid>
          <TelemetryFeed frames={telemetry.frames} className="mt-6" />
          {telemetry.lastError && telemetry.connection !== "open" && (
            <p className="mt-2 font-mono text-xs text-amber-pulse">{telemetry.lastError}</p>
          )}
        </>
      )}

      {strategiesReady && (
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <ButtonLink href={`/foundry/${projectId}/defibrillator`} variant="primary">
            Open the Defibrillator
          </ButtonLink>
          <span className="text-sm text-ink-muted">
            {counters.strategies} revival strateg{counters.strategies === 1 ? "y is" : "ies are"} waiting for a human decision.
          </span>
        </div>
      )}
    </Panel>
  );
}
