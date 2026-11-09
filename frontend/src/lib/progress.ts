/**
 * The five-step resurrection pipeline (Ingest -> Genome -> Strategy -> Foundry -> Rebirth) and how far
 * a project has got, derived from its status and the telemetry seen so far. Pure, so it is unit-tested.
 */
import type { JsonValue, ProjectStatus, TelemetryEventType, TelemetryFrame } from "./types";

export type PipelineStepId = "ingest" | "genome" | "strategy" | "foundry" | "rebirth";
export type StepState = "pending" | "active" | "done" | "failed";

export interface PipelineStep {
  id: PipelineStepId;
  index: number;
  label: string;
  system: string;
  description: string;
}

export const PIPELINE_STEPS: readonly PipelineStep[] = [
  {
    id: "ingest",
    index: 1,
    label: "Ingest",
    system: "apps.ingestion",
    description: "Clone the dead repository, map every branch and parse each file into an AST inventory.",
  },
  {
    id: "genome",
    index: 2,
    label: "Genome",
    system: "apps.genome",
    description: "Reconstruct what the software was meant to become and where the README lies about the code.",
  },
  {
    id: "strategy",
    index: 3,
    label: "Strategy",
    system: "apps.revival",
    description: "Score Restore, Modernize and Extract from 0 to 100 and write a .kiro recovery spec for each.",
  },
  {
    id: "foundry",
    index: 4,
    label: "Foundry",
    system: "apps.foundry",
    description: "After human approval, four agents repair the code and run pytest in a sandbox.",
  },
  {
    id: "rebirth",
    index: 5,
    label: "Rebirth",
    system: "apps.rebirth",
    description: "Ship the verified diff, the exported specs and a continuity roadmap.",
  },
] as const;

/** Index (0-4) of the step a status is working on, and whether that step is finished. */
const STATUS_POSITION: Record<ProjectStatus, { step: number; done: boolean }> = {
  DORMANT: { step: 0, done: false },
  INGESTING: { step: 0, done: false },
  SYNTHESIZING_GENOME: { step: 1, done: false },
  // Genome done; Strategy is active until a human approves one (it never finishes on its own).
  AUTOPSY_COMPLETE: { step: 1, done: true },
  STRATEGY_APPROVED: { step: 2, done: true },
  FOUNDRY_RUNNING: { step: 3, done: false },
  REBORN: { step: 4, done: true },
  FAILED: { step: 0, done: false },
};

const STAGE_TO_STEP: Record<string, number> = {
  INGESTION: 0,
  GENOME: 1,
  REVIVAL: 2,
  FOUNDRY: 3,
  ENVIRONMENT: 3,
  ARCHITECTURE: 3,
  ENGINEERING: 3,
  VERIFICATION: 3,
  REBIRTH: 4,
};

const EVENT_STEP: Partial<Record<TelemetryEventType, { step: number; done: boolean }>> = {
  INGESTION_STARTED: { step: 0, done: false },
  BRANCH_DISCOVERED: { step: 0, done: false },
  AST_PARSED: { step: 0, done: false },
  INGESTION_COMPLETE: { step: 0, done: true },
  GENOME_NODE_CREATED: { step: 1, done: false },
  CONFLICT_DETECTED: { step: 1, done: false },
  GENOME_COMPLETE: { step: 1, done: true },
  STRATEGY_GENERATED: { step: 2, done: false },
  STRATEGY_APPROVED: { step: 2, done: true },
  AGENT_STAGE_CHANGED: { step: 3, done: false },
  SANDBOX_LOG: { step: 3, done: false },
  TEST_PASSED: { step: 3, done: false },
  TEST_FAILED: { step: 3, done: false },
  REBIRTH_COMPLETE: { step: 4, done: true },
};

export interface PipelineProgress {
  states: Record<PipelineStepId, StepState>;
  /** Index (0-4) of the furthest step reached. */
  current: number;
  failed: boolean;
  failure: { stage: string | null; detail: string } | null;
  counters: {
    branches: number;
    abandonedBranches: number;
    filesParsed: number;
    symbols: number;
    markers: number;
    features: number;
    conflicts: number;
    strategies: number;
  };
}

function num(value: JsonValue | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function str(value: JsonValue | undefined): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * Combine the polled project status (authoritative but coarse) with the telemetry frames (fine-grained,
 * but frames sent before the socket connected are missed). The furthest point either source shows wins.
 */
export function derivePipelineProgress(
  status: ProjectStatus | null,
  frames: readonly TelemetryFrame[],
  statusDetail: string = "",
): PipelineProgress {
  let reached = { step: -1, done: false };
  const advance = (candidate: { step: number; done: boolean }) => {
    if (candidate.step > reached.step || (candidate.step === reached.step && candidate.done && !reached.done)) {
      reached = candidate;
    }
  };
  if (status && status !== "FAILED") advance(STATUS_POSITION[status]);

  const counters = {
    branches: 0,
    abandonedBranches: 0,
    filesParsed: 0,
    symbols: 0,
    markers: 0,
    features: 0,
    conflicts: 0,
    strategies: 0,
  };
  let failure: PipelineProgress["failure"] = null;
  const nodeIds = new Set<string>();

  for (const frame of frames) {
    const position = EVENT_STEP[frame.event_type];
    if (position) advance(position);
    const payload = frame.payload;
    switch (frame.event_type) {
      case "INGESTION_STARTED":
        // A re-ingestion starts the counters over.
        Object.assign(counters, { branches: 0, abandonedBranches: 0, filesParsed: 0, symbols: 0, markers: 0 });
        failure = null;
        break;
      case "BRANCH_DISCOVERED":
        counters.branches += 1;
        if (payload.is_abandoned === true) counters.abandonedBranches += 1;
        break;
      case "AST_PARSED": {
        const files = Array.isArray(payload.files) ? payload.files : [];
        counters.filesParsed += files.length;
        for (const file of files) {
          if (file && typeof file === "object" && !Array.isArray(file)) {
            counters.symbols += num(file.symbols);
            counters.markers += num(file.markers);
          }
        }
        break;
      }
      case "INGESTION_COMPLETE":
        counters.branches = Math.max(counters.branches, num(payload.branch_count));
        counters.abandonedBranches = Math.max(counters.abandonedBranches, num(payload.abandoned_branch_count));
        break;
      case "GENOME_NODE_CREATED": {
        const id = str(payload.node_id);
        if (id) nodeIds.add(id);
        counters.features = nodeIds.size;
        break;
      }
      case "CONFLICT_DETECTED":
        counters.conflicts += 1;
        break;
      case "GENOME_COMPLETE":
        counters.features = Math.max(counters.features, num(payload.node_count));
        counters.conflicts = Math.max(counters.conflicts, num(payload.conflict_count));
        break;
      case "STRATEGY_GENERATED":
        counters.strategies += 1;
        break;
      case "ERROR": {
        const stage = str(payload.stage);
        failure = { stage, detail: str(payload.detail) ?? "The pipeline reported an error." };
        // REVIVAL errors leave the project AUTOPSY_COMPLETE (design.md), so they mark the step, not the run.
        advance({ step: stage && stage in STAGE_TO_STEP ? (STAGE_TO_STEP[stage] ?? 0) : reached.step, done: false });
        break;
      }
      default:
        break;
    }
  }

  if (status === "FAILED" && !failure) failure = { stage: null, detail: statusDetail || "The resurrection failed." };
  if (reached.step < 0) reached = { step: 0, done: false };

  const states = {} as Record<PipelineStepId, StepState>;
  PIPELINE_STEPS.forEach((step, index) => {
    states[step.id] = index < reached.step || (index === reached.step && reached.done) ? "done" : "pending";
  });
  const working = reached.done ? reached.step + 1 : reached.step;
  const workingStep = PIPELINE_STEPS[working];
  if (workingStep) states[workingStep.id] = "active";

  // An ERROR marks the step it names (or the current one). A REVIVAL error leaves the project
  // AUTOPSY_COMPLETE (design.md), so only a FAILED status means the whole run stopped.
  let failedIndex: number | null = null;
  if (failure) {
    const stage = failure.stage;
    failedIndex = stage !== null && stage in STAGE_TO_STEP ? (STAGE_TO_STEP[stage] ?? working) : working;
  }
  const failedStep = failedIndex === null ? undefined : PIPELINE_STEPS[failedIndex];
  if (failedIndex !== null && failedStep && states[failedStep.id] !== "done") {
    PIPELINE_STEPS.forEach((step, index) => {
      if (index > failedIndex) states[step.id] = "pending";
    });
    states[failedStep.id] = "failed";
  }

  return { states, current: Math.min(working, PIPELINE_STEPS.length - 1), failed: status === "FAILED", failure, counters };
}

/** Statuses where the pipeline is still moving on its own, so the client keeps polling. */
export function isInFlight(status: ProjectStatus | null): boolean {
  return (
    status === "DORMANT" ||
    status === "INGESTING" ||
    status === "SYNTHESIZING_GENOME" ||
    status === "STRATEGY_APPROVED" ||
    status === "FOUNDRY_RUNNING"
  );
}

/** One short human line per event, for the forensic feed. */
export function describeFrame(frame: TelemetryFrame): string {
  const p = frame.payload;
  switch (frame.event_type) {
    case "INGESTION_STARTED":
      return `Ingestion started: ${str(p.name) ?? "repository"} (${str(p.source) ?? "?"})`;
    case "BRANCH_DISCOVERED": {
      const flags = [
        p.is_default === true ? "default" : null,
        p.is_abandoned === true ? "ABANDONED" : null,
        p.is_merged === false && p.is_default !== true ? "unmerged" : null,
      ].filter(Boolean);
      return `Branch ${str(p.name) ?? "?"} @ ${(str(p.head_sha) ?? "").slice(0, 7)}  +${num(p.commits_ahead)}/-${num(p.commits_behind)}${flags.length ? `  [${flags.join(", ")}]` : ""}`;
    }
    case "AST_PARSED": {
      const files = Array.isArray(p.files) ? p.files.length : 0;
      return `AST parsed: ${files} file${files === 1 ? "" : "s"} on ${str(p.branch) ?? "?"} (${str(p.scope) ?? "?"}, batch ${num(p.batch)})`;
    }
    case "INGESTION_COMPLETE":
      return `Ingestion complete: ${num(p.branch_count)} branches, ${num(p.abandoned_branch_count)} abandoned`;
    case "GENOME_NODE_CREATED":
      return `Genome node: ${str(p.title) ?? "?"}  ${str(p.vital_status) ?? ""}/${str(p.epistemic_status) ?? ""}  C(f)=${num(p.confidence).toFixed(4)}`;
    case "CONFLICT_DETECTED":
      return `Conflict: ${str(p.claim_source) ?? "?"}`;
    case "GENOME_COMPLETE":
      return `Genome complete: ${num(p.node_count)} features, ${num(p.conflict_count)} conflicts (${str(p.mode) ?? "?"})`;
    case "STRATEGY_GENERATED":
      return `Strategy ${str(p.strategy_type) ?? "?"}: Score ${num(p.feasibility_score).toFixed(1)}`;
    case "STRATEGY_APPROVED":
      return `Strategy approved: ${str(p.strategy_type) ?? "?"} by ${str(p.approved_by) || "a human"}`;
    case "AGENT_STAGE_CHANGED":
      return `Agent stage: ${str(p.agent_stage) ?? str(p.stage) ?? "?"}`;
    case "SANDBOX_LOG":
      return `Sandbox: ${str(p.line) ?? str(p.message) ?? "log output"}`;
    case "TEST_PASSED":
      return `Tests passed${typeof p.passed === "number" ? `: ${p.passed}` : ""}`;
    case "TEST_FAILED":
      return `Tests failed${typeof p.failed === "number" ? `: ${p.failed}` : ""}`;
    case "REBIRTH_COMPLETE":
      return `Rebirth complete: ${str(p.pr_title) ?? ""}`;
    case "ERROR":
      return `Error in ${str(p.stage) ?? "pipeline"}: ${str(p.detail) ?? "unknown"}`;
  }
}
