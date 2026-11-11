"use client";

/**
 * View 1, The Autopsy: the Genome Graph, the Evidence Citations drawer, the Conflict Inspector and
 * the forensic ledger, plus the loading, pending, unavailable, error and offline-demo states.
 */
import { useCallback, useMemo, useState } from "react";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Button, ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { Panel } from "@/components/ui/Panel";
import { Readout, ReadoutGrid } from "@/components/ui/Readout";
import { ProjectStatusBadge } from "@/components/ui/StatusBadge";
import {
  EMPTY_FOCUS,
  allFilters,
  countFeatures,
  defibrillatorCta,
  featureNodes,
  findFeature,
  focusForConflict,
  isFeatureVisible,
  revealFeature,
  toggleValue,
  type GenomeFilters,
  type GraphFocus,
} from "@/lib/autopsy";
import { MOCK_PROJECT_ID } from "@/lib/api";
import type { GenomeGraphOut, ProjectStatus } from "@/lib/types";

import { ConflictInspector } from "./ConflictInspector";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { FeatureLedger } from "./FeatureLedger";
import { GenomeGraph } from "./GenomeGraph";
import { GraphFilters, GraphLegend } from "./GraphControls";
import { PendingGenome } from "./PendingGenome";
import { useGenome } from "./useGenome";

const METAPHOR =
  "What was this software meant to become, what still works, what died mid-flight, and where the README lies about the code.";

/** The charge button is the one big call to action on the screen; elsewhere the link is secondary. */
function DefibrillatorCta({ projectId, status, prominent = true }: { projectId: string; status: ProjectStatus | null; prominent?: boolean }) {
  const cta = defibrillatorCta(status);
  if (!cta) return null;
  const href = `/foundry/${projectId}/defibrillator`;
  return cta === "charge" && prominent ? (
    <ButtonLink href={href} variant="charge" size="lg">
      Proceed to the Defibrillator <span aria-hidden>→</span>
    </ButtonLink>
  ) : (
    <ButtonLink href={href} variant="secondary" size="md">
      Open the Defibrillator <span aria-hidden>→</span>
    </ButtonLink>
  );
}

function SynthesisStrip({ genome }: { genome: GenomeGraphOut }) {
  const synthesis = genome.synthesis;
  const counts = countFeatures(genome);
  const validation = synthesis?.validation;
  const dropped = validation
    ? validation.dropped_citation_count +
      validation.unsupported_citation_count +
      validation.rejected_feature_count +
      validation.dropped_conflict_count
    : 0;
  return (
    <Panel eyebrow="Post-mortem summary" title={genome.project.name} headingId="autopsy-summary-heading" tone="alive">
      <ReadoutGrid className="sm:grid-cols-6">
        <Readout label="Features" value={counts.total} />
        <Readout label="Verified" value={counts.vital.VERIFIED_WORKING} token="alive" />
        <Readout label="Abandoned" value={counts.vital.ABANDONED_IN_PROGRESS} token="dormant" />
        <Readout label="Ghosts" value={counts.vital.GHOST_FEATURE} token="ghost" />
        <Readout label="Conflicts" value={genome.conflicts.length} token={genome.conflicts.length ? "conflict" : undefined} />
        <Readout label="Branches" value={genome.branches.length} />
      </ReadoutGrid>
      <p className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-ink-muted">
        <ProjectStatusBadge status={genome.project.status} size="sm" />
        {synthesis ? (
          <>
            <span>synthesis {synthesis.mode}</span>
            {synthesis.model_id && <span>model {synthesis.model_id}</span>}
            <span>digest {synthesis.input_digest_prefix}</span>
            <span>catalog {synthesis.catalog_size} items</span>
            <span>default branch {genome.project.default_branch || "?"}</span>
          </>
        ) : (
          <span>No synthesis record.</span>
        )}
      </p>
      {(dropped > 0 || validation?.fallback_reason) && (
        <p className="mt-3 rounded border border-dormant/50 bg-dormant/10 px-3 py-2 text-xs text-ink">
          <span className="font-mono text-dormant-text">Validation · </span>
          {dropped > 0 && `${dropped} model outputs were dropped for lacking valid evidence. `}
          {validation?.fallback_reason && `Fell back to deterministic synthesis: ${validation.fallback_reason}`}
        </p>
      )}
    </Panel>
  );
}

function ReadyAutopsy({ genome, status }: { genome: GenomeGraphOut; status: ProjectStatus | null }) {
  const { projectId } = useProjectSession();
  const [filters, setFilters] = useState<GenomeFilters>(allFilters);
  const [focus, setFocus] = useState<GraphFocus>(EMPTY_FOCUS);

  const counts = useMemo(() => countFeatures(genome), [genome]);
  const visibleFeatures = useMemo(
    () => featureNodes(genome).filter((node) => isFeatureVisible(node.data, filters)),
    [genome, filters],
  );

  const reveal = useCallback(
    (featureId: string | null) => {
      const feature = featureId ? findFeature(genome, featureId) : undefined;
      if (feature) setFilters((current) => revealFeature(current, feature.data));
    },
    [genome],
  );

  const onFocus = useCallback(
    (next: GraphFocus) => {
      if (next.selection?.kind === "feature") reveal(next.selection.id);
      setFocus(next);
    },
    [reveal],
  );
  const onClear = useCallback(() => setFocus(EMPTY_FOCUS), []);
  const onInspect = useCallback(
    (conflictId: string) => {
      const next = focusForConflict(genome, conflictId);
      if (next) onFocus(next);
    },
    [genome, onFocus],
  );
  const onOpenFeature = useCallback(
    (featureId: string) => onFocus({ selection: { kind: "feature", id: featureId }, conflictId: null }),
    [onFocus],
  );

  const effectiveStatus = status ?? genome.project.status;

  return (
    <>
      {effectiveStatus === "FAILED" && (
        <p role="alert" className="rounded-md border border-ghost/50 bg-ghost/10 px-4 py-3 text-sm text-ink">
          <span className="font-mono text-ghost-text">Re-synthesis failed · </span>
          The project is FAILED; this is the last genome that was stored.
          {genome.project.status_detail && ` ${genome.project.status_detail}`}
        </p>
      )}

      <SynthesisStrip genome={genome} />

      <Panel
        eyebrow="Genome graph"
        title="Project Genome"
        headingId="genome-graph-heading"
        actions={<span className="hidden font-mono text-[11px] text-ink-muted sm:inline">React Flow · backend layout</span>}
      >
        <GraphFilters
          filters={filters}
          counts={counts}
          visibleCount={visibleFeatures.length}
          onToggleVital={(value) => setFilters((current) => ({ ...current, vital: toggleValue(current.vital, value) }))}
          onToggleEpistemic={(value) =>
            setFilters((current) => ({ ...current, epistemic: toggleValue(current.epistemic, value) }))
          }
          onReset={() => setFilters(allFilters())}
        />
        <div className="mt-4">
          <GenomeGraph genome={genome} filters={filters} focus={focus} onFocus={onFocus} onClear={onClear} />
        </div>
        <GraphLegend className="mt-4" />
      </Panel>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <ConflictInspector genome={genome} activeConflictId={focus.conflictId} onInspect={onInspect} />
        <FeatureLedger
          features={visibleFeatures}
          selectedId={focus.selection?.kind === "feature" ? focus.selection.id : null}
          onOpen={onOpenFeature}
        />
      </div>

      {defibrillatorCta(effectiveStatus) && (
        <Panel tone="alive" glow className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-6">
            <div className="min-w-0 flex-1">
              <p className="font-mono text-[11px] uppercase tracking-[0.25em] text-emerald-bio">Autopsy complete</p>
              <p className="mt-1 font-display text-xl font-semibold uppercase tracking-[0.1em] text-ink">
                The patient can be revived.
              </p>
              <p className="mt-1 max-w-xl text-sm text-ink-muted">
                Every finding above cites its evidence. Next, compare the Restore, Modernize and Extract strategies and approve one.
              </p>
              <HeartbeatLine state="pulse" className="mt-3 h-10 max-w-md" />
            </div>
            <DefibrillatorCta projectId={projectId} status={effectiveStatus} />
          </div>
        </Panel>
      )}

      <EvidenceDrawer genome={genome} focus={focus} onClose={onClear} onFocus={onFocus} />
    </>
  );
}

export function AutopsyView() {
  const { api, projectId, project } = useProjectSession();
  const { state, reload } = useGenome();
  const status = project?.status ?? (state.phase === "ready" ? state.genome.project.status : null);

  return (
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">
      <ViewHeader index="01" title="The Autopsy" metaphor={METAPHOR}>
        {state.phase === "ready" && <DefibrillatorCta projectId={projectId} status={status} prominent={false} />}
      </ViewHeader>

      {api.mode === "mock" && (
        <p className="flex flex-wrap items-center gap-2 rounded-md border border-conflict/40 bg-conflict/10 px-4 py-2 text-sm text-ink">
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-conflict-text">Offline demo</span>
          The recorded PayPulse run (no backend). Scores, conflicts and lines are the backend&apos;s deterministic results;
          citation lists are an abridged sample and say so.
        </p>
      )}

      {state.phase === "loading" && (
        <Panel eyebrow="Genome" title="Opening the body…" headingId="genome-loading-heading">
          <div aria-busy="true" aria-live="polite">
            <HeartbeatLine state="flat" label="Loading the genome" className="h-12" />
            <p className="mt-2 font-mono text-xs text-ink-muted">Requesting the Project Genome from the backend.</p>
          </div>
        </Panel>
      )}

      {state.phase === "pending" && (
        <>
          <ResurrectionMonitor />
          <PendingGenome detail={state.notReady.detail} />
        </>
      )}

      {state.phase === "unavailable" && (
        <>
          <Panel eyebrow="Genome" title="No genome to show" headingId="genome-unavailable-heading" tone="ghost">
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
              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" size="sm" onClick={reload}>
                  Check again
                </Button>
                <ButtonLink href="/" variant="ghost" size="sm">
                  Start a new resurrection
                </ButtonLink>
              </div>
            </div>
          </Panel>
          <ResurrectionMonitor compact />
        </>
      )}

      {state.phase === "error" && (
        <Panel eyebrow="Genome" title="The genome could not be read" headingId="genome-error-heading" tone="ghost">
          <div role="alert" className="space-y-3">
            <p className="text-ink">{state.message}</p>
            <div className="flex flex-wrap gap-3">
              <Button variant="secondary" size="sm" onClick={reload}>
                Retry
              </Button>
              {state.offline && api.mode === "http" && (
                <ButtonLink href={`/foundry/${MOCK_PROJECT_ID}/autopsy`} variant="ghost" size="sm">
                  Open the offline demo
                </ButtonLink>
              )}
            </div>
          </div>
        </Panel>
      )}

      {state.phase === "ready" && (
        <>
          <ResurrectionMonitor compact />
          <ReadyAutopsy genome={state.genome} status={status} />
        </>
      )}
    </div>
  );
}
