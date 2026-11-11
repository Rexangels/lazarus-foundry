/**
 * The Heartbeat's pure logic (Task 8.4): the Foundry's 4-agent pipeline state, the live sandbox
 * terminal, the before/after pytest comparison, a unified diff parser and the Continuity Memory
 * readers. No React and no I/O; components/heartbeat/ renders what these functions return.
 *
 * Backend sources:
 *   apps/foundry/agents.py   AGENT_STAGE_CHANGED {foundry_run_id, stage, status: STARTED | COMPLETED |
 *                            SELF_HEALING | FAILED | COMPLETE, agent?, iteration?, max_iterations?,
 *                            repair_status?, files?, code?, tests_passed?, iterations_used?},
 *                            SANDBOX_LOG {foundry_run_id, stage, command, lines: [{stream, line}]},
 *                            TEST_PASSED / TEST_FAILED {foundry_run_id, phase: BASELINE | VERIFICATION,
 *                            iteration, totals, failed, ...}; VERIFICATION nests the full run as
 *                            full: {totals, failed, duration_seconds} next to target_outcomes and reasons
 *                            ERROR {stage: "FOUNDRY" | "REBIRTH", agent_stage?, code?, detail}
 *   apps/rebirth/services.py REBIRTH_COMPLETE (rebirth_payload)
 *   apps/rebirth/builder.py  continuity_memory(), unified_diff() and DIFF_TRUNCATED_NOTE
 * Every reader tolerates missing or mistyped fields: frames can be missed and older runs can
 * carry fewer keys.
 */
import type { AgentStage, JsonValue, ProjectStatus, RebirthFileOut, RebirthOut, TelemetryFrame } from "./types";

// ---------------------------------------------------------------------------
// Small readers
// ---------------------------------------------------------------------------
type Obj = Record<string, unknown>;

function obj(value: unknown): Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Obj) : {};
}
function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}
function strOrNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}
function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function int(value: unknown, fallback = 0): number {
  const n = num(value);
  return n === null ? fallback : Math.trunc(n);
}
function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
function strings(value: unknown): string[] {
  return arr(value).filter((v): v is string => typeof v === "string");
}

// ---------------------------------------------------------------------------
// Pipeline state
// ---------------------------------------------------------------------------
export const PIPELINE_STAGES = ["ENVIRONMENT", "ARCHITECTURE", "ENGINEERING", "VERIFICATION"] as const;
export type PipelineStage = (typeof PIPELINE_STAGES)[number];

export const STAGE_AGENTS: Record<PipelineStage, string> = {
  ENVIRONMENT: "EnvironmentAgent",
  ARCHITECTURE: "ArchitectureAgent",
  ENGINEERING: "EngineeringAgent",
  VERIFICATION: "VerificationAgent",
};

export type StageStatus = "pending" | "running" | "healing" | "done" | "failed";

export interface StageState {
  stage: PipelineStage;
  status: StageStatus;
  agent: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface TestTotals {
  tests: number;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
}

/** One TEST_PASSED / TEST_FAILED frame. */
export interface TestRunEvent {
  phase: "BASELINE" | "VERIFICATION";
  passed: boolean;
  iteration: number | null;
  totals: TestTotals | null;
  failed: string[];
  targetOutcomes: Record<string, string>;
  reasons: string[];
  durationSeconds: number | null;
  timestamp: string;
}

export interface HealingState {
  iteration: number;
  max: number | null;
  repairStatus: string | null;
  files: string[];
}

export interface PipelineFailure {
  /** FOUNDRY: an agent failed; REBIRTH: verification passed but the rebirth could not be built; STATUS: only the project status says so. */
  scope: "FOUNDRY" | "REBIRTH" | "STATUS";
  stage: PipelineStage | null;
  code: string | null;
  detail: string;
}

/** The REBIRTH_COMPLETE payload (apps/rebirth/services.py rebirth_payload). */
export interface RebirthCompleteSummary {
  rebirthArtifactId: string | null;
  prTitle: string;
  revivalBranch: string;
  baseSha: string;
  rebirthCommitSha: string;
  filesChanged: number;
  additions: number;
  deletions: number;
  diffTruncated: boolean;
  exportedKiroSpecsPath: string;
  specFiles: string[];
  before: TestTotals | null;
  after: TestTotals | null;
  previewUrl: string;
}

export type PipelineOutcome =
  /** Before a strategy is approved. */
  | "idle"
  /** Approved, no stage has started yet. */
  | "queued"
  | "running"
  /** Verification passed (Foundry COMPLETE); the rebirth artifact may still be being written. */
  | "verified"
  /** REBIRTH_COMPLETE or project REBORN. */
  | "reborn"
  | "failed";

export interface PipelineState {
  outcome: PipelineOutcome;
  stages: StageState[];
  /** The running (or healing, or failed) stage. */
  current: PipelineStage | null;
  foundryRunId: string | null;
  /** The latest SELF_HEALING frame; null while no repair was attempted. */
  healing: HealingState | null;
  /** `max_iterations` from a SELF_HEALING frame, else from `fallbackMaxIterations`. */
  maxIterations: number | null;
  /** From the final frame (COMPLETE / FAILED). */
  iterationsUsed: number | null;
  testsPassed: boolean | null;
  failure: PipelineFailure | null;
  baseline: TestRunEvent | null;
  /** The last verification attempt. */
  verification: TestRunEvent | null;
  verificationAttempts: number;
  rebirth: RebirthCompleteSummary | null;
  /** 0..1 for the progress bar: each finished stage counts 1, a running one 0.5. */
  progress: number;
  /** True when the project status filled in what the frames did not show (frames were missed). */
  fromStatusFallback: boolean;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface PipelineInput {
  frames: readonly TelemetryFrame[];
  status: ProjectStatus | null;
  statusDetail?: string;
  /** Only frames of this run; by default the run of the newest frame that names one. */
  foundryRunId?: string | null;
  /** The configured self-healing budget when no SELF_HEALING frame says it (FOUNDRY_MAX_HEALING_ITERATIONS, default 3). */
  fallbackMaxIterations?: number | null;
}

const FOUNDRY_EVENTS = new Set<string>([
  "AGENT_STAGE_CHANGED",
  "SANDBOX_LOG",
  "TEST_PASSED",
  "TEST_FAILED",
  "REBIRTH_COMPLETE",
  "ERROR",
  "STRATEGY_APPROVED",
]);

export function isPipelineStage(value: unknown): value is PipelineStage {
  return typeof value === "string" && (PIPELINE_STAGES as readonly string[]).includes(value);
}

export function readTotals(value: unknown): TestTotals | null {
  const t = obj(value);
  if (num(t.tests) === null) return null;
  return { tests: int(t.tests), passed: int(t.passed), failed: int(t.failed), errors: int(t.errors), skipped: int(t.skipped) };
}

function readTestEvent(frame: TelemetryFrame): TestRunEvent {
  const p = frame.payload;
  // A VERIFICATION frame nests the full-suite run under `full`; a BASELINE frame is the run itself.
  const run = obj(p.full);
  const outcomes = obj(p.target_outcomes);
  return {
    phase: p.phase === "BASELINE" ? "BASELINE" : "VERIFICATION",
    passed: frame.event_type === "TEST_PASSED",
    iteration: num(p.iteration),
    totals: readTotals(p.totals) ?? readTotals(run.totals),
    failed: Array.isArray(p.failed) ? strings(p.failed) : strings(run.failed),
    targetOutcomes: Object.fromEntries(Object.entries(outcomes).filter(([, v]) => typeof v === "string")) as Record<string, string>,
    reasons: strings(p.reasons),
    durationSeconds: num(p.duration_seconds) ?? num(run.duration_seconds),
    timestamp: frame.timestamp,
  };
}

export function readRebirthComplete(payload: Record<string, JsonValue> | Obj): RebirthCompleteSummary {
  const p = payload as Obj;
  return {
    rebirthArtifactId: strOrNull(p.rebirth_artifact_id),
    prTitle: str(p.pr_title),
    revivalBranch: str(p.revival_branch),
    baseSha: str(p.base_sha),
    rebirthCommitSha: str(p.rebirth_commit_sha),
    filesChanged: int(p.files_changed),
    additions: int(p.additions),
    deletions: int(p.deletions),
    diffTruncated: p.diff_truncated === true,
    exportedKiroSpecsPath: str(p.exported_kiro_specs_path),
    specFiles: strings(p.spec_files),
    before: readTotals(p.before),
    after: readTotals(p.after),
    previewUrl: str(p.preview_url),
  };
}

/** The run id of the newest frame that names one (frames are ordered by timestamp). */
export function latestFoundryRunId(frames: readonly TelemetryFrame[]): string | null {
  for (let i = frames.length - 1; i >= 0; i -= 1) {
    const frame = frames[i] as TelemetryFrame;
    if (!FOUNDRY_EVENTS.has(frame.event_type)) continue;
    const id = strOrNull(frame.payload.foundry_run_id);
    if (id) return id;
  }
  return null;
}

const STAGE_INDEX: Record<PipelineStage, number> = { ENVIRONMENT: 0, ARCHITECTURE: 1, ENGINEERING: 2, VERIFICATION: 3 };

/** The Foundry's state from its frames, with the project status as a fallback for missed frames. */
export function pipelineState(input: PipelineInput): PipelineState {
  const { frames, status } = input;
  const runId = input.foundryRunId ?? latestFoundryRunId(frames);
  const stages: StageState[] = PIPELINE_STAGES.map((stage) => ({
    stage,
    status: "pending",
    agent: STAGE_AGENTS[stage],
    startedAt: null,
    finishedAt: null,
  }));
  const state: PipelineState = {
    outcome: "idle",
    stages,
    current: null,
    foundryRunId: runId,
    healing: null,
    maxIterations: input.fallbackMaxIterations ?? null,
    iterationsUsed: null,
    testsPassed: null,
    failure: null,
    baseline: null,
    verification: null,
    verificationAttempts: 0,
    rebirth: null,
    progress: 0,
    fromStatusFallback: false,
    startedAt: null,
    finishedAt: null,
  };
  const byStage = (stage: PipelineStage) => stages[STAGE_INDEX[stage]] as StageState;
  let approved = false;
  let final: "COMPLETE" | "FAILED" | null = null;

  for (const frame of frames) {
    if (!FOUNDRY_EVENTS.has(frame.event_type)) continue;
    const p = frame.payload;
    const frameRun = strOrNull(p.foundry_run_id);
    if (runId && frameRun && frameRun !== runId) continue;
    switch (frame.event_type) {
      case "STRATEGY_APPROVED":
        approved = true;
        break;
      case "AGENT_STAGE_CHANGED": {
        const stage = p.stage;
        const stageStatus = str(p.status);
        state.startedAt ??= frame.timestamp;
        if (stage === "COMPLETE" || stage === "FAILED") {
          final = stage;
          state.finishedAt = frame.timestamp;
          state.testsPassed = bool(p.tests_passed);
          state.iterationsUsed = num(p.iterations_used);
          break;
        }
        if (!isPipelineStage(stage)) break;
        const entry = byStage(stage);
        // A stage that starts implies every earlier stage finished (their frames may have been missed).
        if (stageStatus === "STARTED" || stageStatus === "SELF_HEALING") {
          stages.slice(0, STAGE_INDEX[stage]).forEach((earlier) => {
            if (earlier.status === "pending" || earlier.status === "running") earlier.status = "done";
          });
        }
        if (stageStatus === "STARTED") {
          entry.status = "running";
          entry.startedAt = frame.timestamp;
        } else if (stageStatus === "COMPLETED") {
          entry.status = "done";
          entry.finishedAt = frame.timestamp;
        } else if (stageStatus === "SELF_HEALING") {
          entry.status = "healing";
          entry.startedAt ??= frame.timestamp;
          const max = num(p.max_iterations);
          state.healing = { iteration: int(p.iteration, 1), max, repairStatus: strOrNull(p.repair_status), files: strings(p.files) };
          if (max !== null) state.maxIterations = max;
        } else if (stageStatus === "FAILED") {
          entry.status = "failed";
          entry.finishedAt = frame.timestamp;
          state.failure ??= { scope: "FOUNDRY", stage, code: strOrNull(p.code), detail: "" };
          if (state.failure.stage === null) state.failure.stage = stage;
          state.failure.code ??= strOrNull(p.code);
        }
        if (typeof p.agent === "string" && p.agent) entry.agent = p.agent;
        break;
      }
      case "TEST_PASSED":
      case "TEST_FAILED": {
        const event = readTestEvent(frame);
        if (event.phase === "BASELINE") state.baseline = event;
        else {
          state.verification = event;
          state.verificationAttempts += 1;
        }
        break;
      }
      case "ERROR": {
        const scope = p.stage === "REBIRTH" ? "REBIRTH" : p.stage === "FOUNDRY" || frameRun ? "FOUNDRY" : null;
        if (!scope) break; // an ingestion or genome error is not the Foundry's
        const agentStage = isPipelineStage(p.agent_stage) ? p.agent_stage : null;
        state.failure = {
          scope,
          stage: agentStage ?? state.failure?.stage ?? null,
          code: strOrNull(p.code) ?? state.failure?.code ?? null,
          detail: str(p.detail),
        };
        if (agentStage) {
          const entry = byStage(agentStage);
          entry.status = "failed";
          entry.finishedAt ??= frame.timestamp;
        }
        break;
      }
      case "REBIRTH_COMPLETE":
        state.rebirth = readRebirthComplete(p);
        break;
    }
  }

  // Outcome from the frames.
  const anyStarted = stages.some((s) => s.status !== "pending");
  if (state.failure) state.outcome = "failed";
  else if (state.rebirth) state.outcome = "reborn";
  else if (final === "COMPLETE") state.outcome = "verified";
  else if (final === "FAILED") state.outcome = "failed";
  else if (anyStarted) state.outcome = "running";
  else if (approved) state.outcome = "queued";

  // Project status fallback (missed frames, a reload after the run, a status-only failure).
  const fallback = (outcome: PipelineOutcome) => {
    if (state.outcome !== outcome) state.fromStatusFallback = true;
    state.outcome = outcome;
  };
  if (status === "REBORN" && state.outcome !== "failed") fallback("reborn");
  else if (status === "FAILED" && state.outcome !== "failed" && (anyStarted || approved || final !== null)) {
    fallback("failed");
    const running = stages.find((s) => s.status === "running" || s.status === "healing");
    state.failure = { scope: "STATUS", stage: running?.stage ?? null, code: null, detail: input.statusDetail ?? "" };
  } else if (status === "FOUNDRY_RUNNING" && (state.outcome === "idle" || state.outcome === "queued")) fallback("running");
  else if (status === "STRATEGY_APPROVED" && state.outcome === "idle") fallback("queued");

  if (state.outcome === "reborn" || state.outcome === "verified") {
    stages.forEach((s) => {
      s.status = "done";
    });
    state.testsPassed ??= true;
  }
  if (state.outcome === "failed" && state.failure) {
    if (!state.failure.detail && input.statusDetail) state.failure.detail = input.statusDetail;
    if (state.failure.scope !== "REBIRTH") {
      const failedStage = state.failure.stage ? byStage(state.failure.stage) : stages.find((s) => s.status === "running" || s.status === "healing");
      if (failedStage) {
        failedStage.status = "failed";
        state.failure.stage ??= failedStage.stage;
      }
    } else {
      stages.forEach((s) => {
        s.status = "done";
      });
    }
  }
  if (state.outcome === "running" && !stages.some((s) => s.status === "running" || s.status === "healing")) {
    // FOUNDRY_RUNNING without a STARTED frame: the next pending stage is the one running.
    const next = stages.find((s) => s.status === "pending");
    if (next) {
      next.status = "running";
      state.fromStatusFallback = true;
    }
  }

  const active = stages.find((s) => s.status === "running" || s.status === "healing" || s.status === "failed");
  state.current = active?.stage ?? null;
  const done = stages.filter((s) => s.status === "done").length;
  const half = stages.some((s) => s.status === "running" || s.status === "healing") ? 0.5 : 0;
  state.progress = state.outcome === "reborn" || state.outcome === "verified" ? 1 : Math.min(1, (done + half) / stages.length);
  return state;
}

/** "Self-healing 1 of 3", or null while no repair was attempted. */
export function healingLabel(state: Pick<PipelineState, "healing" | "maxIterations">): string | null {
  if (!state.healing) return null;
  const max = state.healing.max ?? state.maxIterations;
  return max === null ? `Self-healing iteration ${state.healing.iteration}` : `Self-healing iteration ${state.healing.iteration} of ${max}`;
}

// ---------------------------------------------------------------------------
// Terminal
// ---------------------------------------------------------------------------
export const MAX_TERMINAL_LINES = 5000;

export type TerminalKind =
  /** A sandbox stdout line. */
  | "stdout"
  /** A sandbox stderr line. */
  | "stderr"
  /** `$ <command>` before the first line of a new sandbox command. */
  | "command"
  /** An AGENT_STAGE_CHANGED line. */
  | "stage"
  | "test-pass"
  | "test-fail"
  | "error"
  /** REBIRTH_COMPLETE. */
  | "success";

export interface TerminalLine {
  /** Stable React key: `<event_id>:<index>`. */
  id: string;
  kind: TerminalKind;
  stage: PipelineStage | null;
  text: string;
  timestamp: string;
}

export interface TerminalBuffer {
  lines: TerminalLine[];
  /** Lines dropped from the start to stay under the cap. */
  dropped: number;
}

function totalsText(totals: TestTotals | null): string {
  if (!totals) return "no totals";
  const parts = [`${totals.passed} passed`];
  if (totals.failed) parts.push(`${totals.failed} failed`);
  if (totals.errors) parts.push(`${totals.errors} errors`);
  if (totals.skipped) parts.push(`${totals.skipped} skipped`);
  return `${parts.join(", ")} of ${totals.tests}`;
}

function stageLine(p: Record<string, JsonValue>): string {
  const stage = str(p.stage);
  const status = str(p.status);
  if (stage === "COMPLETE") return `Foundry COMPLETE: tests passing, ${int(p.iterations_used)} self-healing iteration(s) used`;
  if (stage === "FAILED") return `Foundry FAILED after ${int(p.iterations_used)} self-healing iteration(s)`;
  if (status === "SELF_HEALING") {
    const max = num(p.max_iterations);
    const files = strings(p.files);
    return `${stage}: self-healing iteration ${int(p.iteration, 1)}${max === null ? "" : ` of ${max}`} (${str(p.repair_status, "?")})${files.length ? `: ${files.join(", ")}` : ""}`;
  }
  if (status === "FAILED") return `${stage} FAILED${p.code ? ` (${str(p.code)})` : ""}`;
  const agent = str(p.agent);
  return `${stage} ${status}${agent ? ` (${agent})` : ""}`;
}

/** The terminal's lines from the Foundry frames, newest last, capped at `limit` lines. */
export function terminalLines(frames: readonly TelemetryFrame[], limit: number = MAX_TERMINAL_LINES): TerminalBuffer {
  const lines: TerminalLine[] = [];
  let lastCommand: string | null = null;
  for (const frame of frames) {
    const p = frame.payload;
    const stage = isPipelineStage(p.stage) ? p.stage : isPipelineStage(p.agent_stage) ? p.agent_stage : null;
    const push = (kind: TerminalKind, text: string, index: number) =>
      lines.push({ id: `${frame.event_id}:${index}`, kind, stage, text, timestamp: frame.timestamp });
    switch (frame.event_type) {
      case "SANDBOX_LOG": {
        const command = str(p.command);
        if (command && command !== lastCommand) push("command", `$ ${command}`, -1);
        lastCommand = command || lastCommand;
        arr(p.lines).forEach((entry, index) => {
          const e = obj(entry);
          push(e.stream === "stderr" ? "stderr" : "stdout", str(e.line), index);
        });
        break;
      }
      case "AGENT_STAGE_CHANGED":
        lastCommand = null;
        push(p.stage === "FAILED" || p.status === "FAILED" ? "error" : "stage", stageLine(p), 0);
        break;
      case "TEST_PASSED":
      case "TEST_FAILED": {
        const event = readTestEvent(frame);
        const label = event.phase === "BASELINE" ? "baseline" : `verification${event.iteration ? ` ${event.iteration}` : ""}`;
        push(event.passed ? "test-pass" : "test-fail", `${event.passed ? "TEST_PASSED" : "TEST_FAILED"} ${label}: ${totalsText(event.totals)}`, 0);
        event.failed.forEach((nodeid, index) => push("test-fail", `  FAILED ${nodeid}`, index + 1));
        break;
      }
      case "ERROR":
        if (p.stage !== "FOUNDRY" && p.stage !== "REBIRTH" && !p.foundry_run_id) break;
        push("error", `ERROR ${str(p.stage, "FOUNDRY")}${p.code ? ` ${str(p.code)}` : ""}: ${str(p.detail)}`, 0);
        break;
      case "REBIRTH_COMPLETE": {
        const r = readRebirthComplete(p);
        push("success", `REBIRTH_COMPLETE ${r.revivalBranch} @ ${r.rebirthCommitSha.slice(0, 12)}: ${r.filesChanged} files, +${r.additions} -${r.deletions}`, 0);
        break;
      }
      default:
        break;
    }
  }
  const dropped = Math.max(0, lines.length - limit);
  return { lines: dropped ? lines.slice(dropped) : lines, dropped };
}

export interface TerminalFilter {
  /** "ALL" (default) or one stage; event lines without a stage stay visible. */
  stage?: PipelineStage | "ALL";
  stdout?: boolean;
  stderr?: boolean;
}

export function filterTerminal(lines: readonly TerminalLine[], filter: TerminalFilter = {}): TerminalLine[] {
  const { stage = "ALL", stdout = true, stderr = true } = filter;
  return lines.filter((line) => {
    if (line.kind === "stdout" && !stdout) return false;
    if (line.kind === "stderr" && !stderr) return false;
    return stage === "ALL" || line.stage === null || line.stage === stage;
  });
}

// ---------------------------------------------------------------------------
// Before / after pytest
// ---------------------------------------------------------------------------
export interface PytestSummary {
  phase: "before" | "after";
  /** The branch the run was on (memory project.default_branch / revival.revival_branch). */
  branch: string;
  totals: TestTotals | null;
  /** Failing node ids (failed or errored). */
  failed: string[];
  /** The raw pytest output (before_test_output / after_test_output). */
  output: string;
  /** True when every collected test passed. Null without totals. */
  allPassing: boolean | null;
}

export interface PytestComparison {
  before: PytestSummary;
  after: PytestSummary;
  /** Failed before and pass now (memory verification.tests_fixed). */
  fixed: string[];
  targetOutcomes: Record<string, string>;
}

const PYTEST_COUNT = /(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)/g;
/** pytest's final line: `=== 1 failed, 15 passed in 0.1s ===`, or bare `1 failed, 15 passed in 0.88s` under `-q`. */
const PYTEST_SUMMARY = /^(?:=+ )?(?:no tests ran|\d+ \w+.*) in [\d.]+s\b.*?(?: =+)?$/;

/** The totals of pytest's final `=== 1 failed, 15 passed in 0.1s ===` line (bordered or bare), or null. */
export function parsePytestTotals(output: string): TestTotals | null {
  const lines = output.split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const line = lines[i] as string;
    if (!PYTEST_SUMMARY.test(line.trim())) continue;
    const t: TestTotals = { tests: 0, passed: 0, failed: 0, errors: 0, skipped: 0 };
    let found = false;
    for (const match of line.matchAll(PYTEST_COUNT)) {
      const n = Number(match[1]);
      const word = match[2] as string;
      if (word === "passed" || word === "xpassed") t.passed += n;
      else if (word === "failed") t.failed += n;
      else if (word.startsWith("error")) t.errors += n;
      else if (word === "skipped" || word === "xfailed") t.skipped += n;
      else continue;
      found = true;
    }
    if (found) {
      t.tests = t.passed + t.failed + t.errors + t.skipped;
      return t;
    }
  }
  return null;
}

/** Node ids of pytest's `FAILED <id>` / `ERROR <id>` short-summary lines. */
export function parsePytestFailures(output: string): string[] {
  const ids: string[] = [];
  for (const line of output.split(/\r?\n/)) {
    const match = /^(?:FAILED|ERROR) (\S+)/.exec(line.trim());
    if (match && !ids.includes(match[1] as string)) ids.push(match[1] as string);
  }
  return ids;
}

export type PytestLineKind = "pass" | "fail" | "error" | "skip" | "summary-pass" | "summary-fail" | "section" | "plain";

/** Coloring of one pytest output line. */
export function classifyPytestLine(line: string): PytestLineKind {
  const text = line.trim();
  if (PYTEST_SUMMARY.test(text)) return /\d+ (failed|errors?)\b/.test(text) ? "summary-fail" : "summary-pass";
  if (/^=+ .* =+$/.test(text)) {
    if (/ in [\d.]+s/.test(text)) return /\d+ (failed|errors?)\b/.test(text) ? "summary-fail" : "summary-pass";
    return "section";
  }
  if (/^_{3,} .* _{3,}$/.test(text)) return "section";
  if (/^FAILED\b/.test(text) || /\sFAILED(\s|$)/.test(text)) return "fail";
  if (/^ERROR\b/.test(text) || /\sERROR(\s|$)/.test(text) || /^E\s/.test(text)) return "error";
  if (/\sPASSED(\s|$)/.test(text)) return "pass";
  if (/\sSKIPPED(\s|$)/.test(text) || /^SKIPPED\b/.test(text)) return "skip";
  return "plain";
}

function allPassing(totals: TestTotals | null): boolean | null {
  return totals ? totals.failed === 0 && totals.errors === 0 && totals.tests > 0 : null;
}

/**
 * The before (default branch, baseline) and after (revival branch, last verification) runs: totals
 * and failing ids from the Continuity Memory, else the TEST_* frames, else the pytest output itself.
 */
export function pytestComparison(rebirth: RebirthOut | null, pipeline?: Pick<PipelineState, "baseline" | "verification"> | null): PytestComparison {
  const memory = readContinuityMemory(rebirth?.continuity_memory);
  const v = memory.verification;
  const beforeOutput = rebirth?.before_test_output ?? "";
  const afterOutput = rebirth?.after_test_output ?? "";
  const beforeTotals = v?.before ?? pipeline?.baseline?.totals ?? parsePytestTotals(beforeOutput);
  const afterTotals = v?.after ?? pipeline?.verification?.totals ?? parsePytestTotals(afterOutput);
  const beforeFailed = v?.failingBefore.length ? v.failingBefore : pipeline?.baseline?.failed.length ? pipeline.baseline.failed : parsePytestFailures(beforeOutput);
  const afterFailed = pipeline?.verification?.failed.length ? pipeline.verification.failed : parsePytestFailures(afterOutput);
  return {
    before: {
      phase: "before",
      branch: memory.project?.defaultBranch || rebirth?.project.default_branch || "",
      totals: beforeTotals,
      failed: beforeFailed,
      output: beforeOutput,
      allPassing: allPassing(beforeTotals),
    },
    after: {
      phase: "after",
      branch: memory.revival?.revivalBranch || rebirth?.revival_branch || "",
      totals: afterTotals,
      failed: afterFailed,
      output: afterOutput,
      allPassing: allPassing(afterTotals),
    },
    fixed: v?.testsFixed ?? beforeFailed.filter((id) => !afterFailed.includes(id)),
    targetOutcomes: v?.targetOutcomes ?? pipeline?.verification?.targetOutcomes ?? {},
  };
}

// ---------------------------------------------------------------------------
// Unified diff
// ---------------------------------------------------------------------------
export const SPEC_PATH_PREFIX = ".kiro/specs/";
const TRUNCATED_NOTE = /^\[lazarus\] diff truncated\b/;

export type DiffLineKind = "add" | "del" | "context" | "hunk" | "meta" | "note";

export interface DiffLine {
  kind: DiffLineKind;
  /** The line without its `+`/`-`/` ` prefix (hunk and meta lines are kept whole). */
  text: string;
  oldLine: number | null;
  newLine: number | null;
}

export interface DiffHunk {
  header: string;
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  /** The function context after the second `@@`. */
  section: string;
  /** Starts with the `hunk` line itself. */
  lines: DiffLine[];
}

export interface DiffFile {
  /** The new path, or the old path for a deletion. */
  path: string;
  oldPath: string | null;
  newPath: string | null;
  /** A, M, D or T, from the extended header lines. */
  status: "A" | "M" | "D" | "T";
  binary: boolean;
  /** Under `.kiro/specs/`: the exported recovery spec. */
  isSpec: boolean;
  /** `diff --git`, `index`, mode, `---`/`+++` and `Binary files` lines. */
  meta: string[];
  hunks: DiffHunk[];
  additions: number;
  deletions: number;
}

export interface ParsedDiff {
  files: DiffFile[];
  additions: number;
  deletions: number;
  truncated: boolean;
  /** The `[lazarus] diff truncated at N characters; run git diff ...` note. */
  truncationNote: string | null;
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

/** Git's C-style quoted path (`"a/caf\303\251.py"`) or a plain one, without the `a/`/`b/` prefix. */
export function unquoteGitPath(raw: string): string | null {
  let path = raw.trim();
  if (path === "/dev/null") return null;
  if (path.startsWith('"') && path.endsWith('"') && path.length >= 2) {
    const bytes: number[] = [];
    const body = path.slice(1, -1);
    const encoder = new TextEncoder();
    const ESC: Record<string, number> = { n: 10, t: 9, r: 13, '"': 34, "\\": 92, a: 7, b: 8, f: 12, v: 11 };
    for (let i = 0; i < body.length; i += 1) {
      const ch = body[i] as string;
      if (ch !== "\\") {
        bytes.push(...encoder.encode(ch));
        continue;
      }
      const next = body[i + 1] ?? "";
      const octal = /^[0-7]{3}/.exec(body.slice(i + 1));
      if (octal) {
        bytes.push(parseInt(octal[0], 8));
        i += 3;
      } else {
        bytes.push(ESC[next] ?? next.charCodeAt(0));
        i += 1;
      }
    }
    path = new TextDecoder().decode(new Uint8Array(bytes));
  }
  return path.replace(/^[ab]\//, "");
}

function pathsFromDiffGit(line: string): [string | null, string | null] {
  const rest = line.slice("diff --git ".length);
  if (rest.startsWith('"')) {
    const match = /^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*"|\S.*)$/.exec(rest);
    if (match) return [unquoteGitPath(match[1] as string), unquoteGitPath(match[2] as string)];
  }
  // Unquoted: "a/<p> b/<p>"; without renames both halves are the same path.
  const half = (rest.length - 1) / 2;
  if (Number.isInteger(half) && rest[half] === " ") {
    const a = rest.slice(0, half);
    const b = rest.slice(half + 1);
    if (a.slice(2) === b.slice(2)) return [unquoteGitPath(a), unquoteGitPath(b)];
  }
  const split = rest.lastIndexOf(" b/");
  return split > 0 ? [unquoteGitPath(rest.slice(0, split)), unquoteGitPath(rest.slice(split + 1))] : [null, null];
}

/** Parses `git diff` output into files, hunks and numbered lines. Never throws. */
export function parseUnifiedDiff(patch: string): ParsedDiff {
  const out: ParsedDiff = { files: [], additions: 0, deletions: 0, truncated: false, truncationNote: null };
  const lines = patch.replace(/\r\n/g, "\n").split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0;
  let newLeft = 0;

  const finishFile = () => {
    if (!file) return;
    file.path = file.newPath ?? file.oldPath ?? file.path;
    file.isSpec = file.path.startsWith(SPEC_PATH_PREFIX);
    out.additions += file.additions;
    out.deletions += file.deletions;
    out.files.push(file);
    file = null;
    hunk = null;
  };

  for (const line of lines) {
    const inHunk = hunk !== null && (oldLeft > 0 || newLeft > 0);
    if (inHunk && file && hunk) {
      const h: DiffHunk = hunk;
      const f: DiffFile = file;
      const mark = line[0];
      if (mark === "+" && newLeft > 0) {
        h.lines.push({ kind: "add", text: line.slice(1), oldLine: null, newLine: newNo });
        newNo += 1;
        newLeft -= 1;
        f.additions += 1;
        continue;
      }
      if (mark === "-" && oldLeft > 0) {
        h.lines.push({ kind: "del", text: line.slice(1), oldLine: oldNo, newLine: null });
        oldNo += 1;
        oldLeft -= 1;
        f.deletions += 1;
        continue;
      }
      if ((mark === " " || line === "") && oldLeft > 0 && newLeft > 0) {
        h.lines.push({ kind: "context", text: line.slice(1), oldLine: oldNo, newLine: newNo });
        oldNo += 1;
        newNo += 1;
        oldLeft -= 1;
        newLeft -= 1;
        continue;
      }
      if (mark === "\\") {
        h.lines.push({ kind: "meta", text: line, oldLine: null, newLine: null });
        continue;
      }
      // Anything else ends the hunk early (a malformed or cut patch); handle it below.
      oldLeft = 0;
      newLeft = 0;
    }
    if (line.startsWith("\\") && hunk) {
      (hunk as DiffHunk).lines.push({ kind: "meta", text: line, oldLine: null, newLine: null });
      continue;
    }
    if (TRUNCATED_NOTE.test(line)) {
      finishFile();
      out.truncated = true;
      out.truncationNote = line;
      continue;
    }
    if (line.startsWith("diff --git ")) {
      finishFile();
      const [oldPath, newPath] = pathsFromDiffGit(line);
      file = { path: newPath ?? oldPath ?? "", oldPath, newPath, status: "M", binary: false, isSpec: false, meta: [line], hunks: [], additions: 0, deletions: 0 };
      continue;
    }
    if (!file) continue; // preamble or stray text outside a file
    const f: DiffFile = file;
    const header = HUNK_HEADER.exec(line);
    if (header) {
      oldNo = Number(header[1]);
      newNo = Number(header[3]);
      oldLeft = header[2] === undefined ? 1 : Number(header[2]);
      newLeft = header[4] === undefined ? 1 : Number(header[4]);
      hunk = { header: line, oldStart: oldNo, oldLines: oldLeft, newStart: newNo, newLines: newLeft, section: header[5] ?? "", lines: [] };
      hunk.lines.push({ kind: "hunk", text: line, oldLine: null, newLine: null });
      f.hunks.push(hunk);
      continue;
    }
    if (hunk) continue; // stray text after a hunk ended
    f.meta.push(line);
    if (line.startsWith("new file mode")) {
      f.status = "A";
      f.oldPath = null;
    } else if (line.startsWith("deleted file mode")) {
      f.status = "D";
      f.newPath = null;
    } else if (line.startsWith("--- ")) {
      f.oldPath = unquoteGitPath(line.slice(4));
    } else if (line.startsWith("+++ ")) {
      f.newPath = unquoteGitPath(line.slice(4));
    } else if (line.startsWith("Binary files ") || line === "GIT binary patch") {
      f.binary = true;
    } else if (line.startsWith("new mode ") && f.meta.some((m) => m.startsWith("old mode 120000") || m.startsWith("old mode 160000"))) {
      f.status = "T";
    }
  }
  finishFile();
  return out;
}

/** The parsed files with the backend's per-file counts and status where they are known. */
export function withFileStats(parsed: ParsedDiff, files: readonly RebirthFileOut[]): DiffFile[] {
  const byPath = new Map(files.map((f) => [f.path, f]));
  return parsed.files.map((file) => {
    const stat = byPath.get(file.path);
    if (!stat) return file;
    const status = stat.status === "A" || stat.status === "M" || stat.status === "D" || stat.status === "T" ? stat.status : file.status;
    return { ...file, status, binary: file.binary || stat.additions === null };
  });
}

// ---------------------------------------------------------------------------
// Continuity Memory (apps/rebirth/builder.py continuity_memory, version lazarus-continuity-v1)
// ---------------------------------------------------------------------------
export interface MemoryProject {
  id: string;
  name: string;
  repoUrl: string;
  defaultBranch: string;
}

export interface MemoryRevival {
  strategyId: string;
  strategyType: string;
  title: string;
  feasibilityScore: number | null;
  approvedBy: string;
  approvedAt: string | null;
  foundryRunId: string;
  revivalBranch: string;
  baseSha: string;
  rebirthCommitSha: string;
  specName: string;
  specPath: string;
  specFiles: string[];
}

export interface MemoryVerification {
  before: TestTotals | null;
  after: TestTotals | null;
  testsFixed: string[];
  failingBefore: string[];
  targetOutcomes: Record<string, string>;
  targetsRunSeparately: TestTotals | null;
  verificationAttempts: number;
  iterationsUsed: number;
  maxHealingIterations: number;
  python: string;
  sandboxMode: string;
  networkIsolated: boolean | null;
  installResolution: string;
}

export interface MemoryCompletedItem {
  itemId: string;
  kind: string;
  stage: string;
  title: string;
  featureId: string | null;
  files: string[];
  targetTests: string[];
}

export interface MemoryMergedBranch {
  branch: string;
  branchHead: string;
  mergeCommit: string;
  files: string[];
}

export interface MemoryRevivedFeature {
  featureId: string | null;
  title: string;
  vitalStatus: string;
  partial: boolean;
}

export interface MemoryRoadmapStep {
  step: number;
  featureId: string | null;
  title: string;
  vitalStatus: string;
  reason: string;
  detail: string;
  wouldTouch: string[];
  externalRequirements: string[];
  partial: boolean;
  status: string;
}

export interface ContinuityMemory {
  version: string;
  generatedAt: string;
  project: MemoryProject | null;
  revival: MemoryRevival | null;
  verification: MemoryVerification | null;
  completed: MemoryCompletedItem[];
  mergedBranches: MemoryMergedBranch[];
  featuresRevived: MemoryRevivedFeature[];
  roadmap: MemoryRoadmapStep[];
  openMarkers: string[];
  changedFiles: RebirthFileOut[];
  uncommittedFiles: string[];
}

type Memory = Record<string, unknown> | null | undefined;

function section(memory: Memory, key: string): Obj | null {
  const value = memory?.[key];
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Obj) : null;
}

export function readMemoryProject(memory: Memory): MemoryProject | null {
  const p = section(memory, "project");
  return p ? { id: str(p.id), name: str(p.name), repoUrl: str(p.repo_url), defaultBranch: str(p.default_branch) } : null;
}

export function readRevival(memory: Memory): MemoryRevival | null {
  const r = section(memory, "revival");
  if (!r) return null;
  return {
    strategyId: str(r.strategy_id),
    strategyType: str(r.strategy_type),
    title: str(r.title),
    feasibilityScore: num(r.feasibility_score),
    approvedBy: str(r.approved_by),
    approvedAt: strOrNull(r.approved_at),
    foundryRunId: str(r.foundry_run_id),
    revivalBranch: str(r.revival_branch),
    baseSha: str(r.base_sha),
    rebirthCommitSha: str(r.rebirth_commit_sha),
    specName: str(r.spec_name),
    specPath: str(r.spec_path),
    specFiles: strings(r.spec_files),
  };
}

export function readVerification(memory: Memory): MemoryVerification | null {
  const v = section(memory, "verification");
  if (!v) return null;
  const outcomes = obj(v.target_outcomes);
  return {
    before: readTotals(v.before),
    after: readTotals(v.after),
    testsFixed: strings(v.tests_fixed),
    failingBefore: strings(v.failing_before),
    targetOutcomes: Object.fromEntries(Object.entries(outcomes).filter(([, o]) => typeof o === "string")) as Record<string, string>,
    targetsRunSeparately: readTotals(v.targets_run_separately),
    verificationAttempts: int(v.verification_attempts),
    iterationsUsed: int(v.iterations_used),
    maxHealingIterations: int(v.max_healing_iterations),
    python: str(v.python),
    sandboxMode: str(v.sandbox_mode),
    networkIsolated: bool(v.network_isolated),
    installResolution: str(v.install_resolution),
  };
}

function entries(memory: Memory, key: string): Obj[] {
  return arr(memory?.[key]).filter((e): e is Obj => typeof e === "object" && e !== null && !Array.isArray(e));
}

export function readCompleted(memory: Memory): MemoryCompletedItem[] {
  return entries(memory, "completed").map((e) => ({
    itemId: str(e.item_id),
    kind: str(e.kind),
    stage: str(e.stage),
    title: str(e.title),
    featureId: strOrNull(e.feature_id),
    files: strings(e.files),
    targetTests: strings(e.target_tests),
  }));
}

export function readMergedBranches(memory: Memory): MemoryMergedBranch[] {
  return entries(memory, "merged_branches").map((e) => ({
    branch: str(e.branch),
    branchHead: str(e.branch_head),
    mergeCommit: str(e.merge_commit),
    files: strings(e.files),
  }));
}

export function readFeaturesRevived(memory: Memory): MemoryRevivedFeature[] {
  return entries(memory, "features_revived").map((e) => ({
    featureId: strOrNull(e.feature_id),
    title: str(e.title),
    vitalStatus: str(e.vital_status),
    partial: e.partial === true,
  }));
}

export function readRoadmap(memory: Memory): MemoryRoadmapStep[] {
  return entries(memory, "roadmap").map((e, index) => ({
    step: int(e.step, index + 1),
    featureId: strOrNull(e.feature_id),
    title: str(e.title),
    vitalStatus: str(e.vital_status),
    reason: str(e.reason),
    detail: str(e.detail),
    wouldTouch: strings(e.would_touch),
    externalRequirements: arr(e.external_requirements).map((r) => (typeof r === "string" ? r : str(obj(r).label))).filter(Boolean),
    partial: e.partial === true,
    status: str(e.status, "DEFERRED"),
  }));
}

export function readOpenMarkers(memory: Memory): string[] {
  return strings(memory?.open_markers);
}

export function readChangedFiles(memory: Memory): RebirthFileOut[] {
  return entries(memory, "changed_files")
    .filter((e) => typeof e.path === "string")
    .map((e) => ({ path: str(e.path), status: str(e.status), additions: num(e.additions), deletions: num(e.deletions) }));
}

export function readContinuityMemory(memory: Memory): ContinuityMemory {
  return {
    version: str(memory?.version),
    generatedAt: str(memory?.generated_at),
    project: readMemoryProject(memory),
    revival: readRevival(memory),
    verification: readVerification(memory),
    completed: readCompleted(memory),
    mergedBranches: readMergedBranches(memory),
    featuresRevived: readFeaturesRevived(memory),
    roadmap: readRoadmap(memory),
    openMarkers: readOpenMarkers(memory),
    changedFiles: readChangedFiles(memory),
    uncommittedFiles: strings(memory?.uncommitted_files),
  };
}

/** Pretty JSON of the memory for a download or a raw view. */
export function continuityMemoryJson(memory: Memory): string {
  return JSON.stringify(memory ?? {}, null, 2);
}

/** Totals of all files in the rebirth (`files` from RebirthOut). */
export function rebirthFileTotals(files: readonly RebirthFileOut[]): { files: number; additions: number; deletions: number; binary: number } {
  return files.reduce(
    (acc, f) => ({
      files: acc.files + 1,
      additions: acc.additions + (f.additions ?? 0),
      deletions: acc.deletions + (f.deletions ?? 0),
      binary: acc.binary + (f.additions === null ? 1 : 0),
    }),
    { files: 0, additions: 0, deletions: 0, binary: 0 },
  );
}

export type { AgentStage };
