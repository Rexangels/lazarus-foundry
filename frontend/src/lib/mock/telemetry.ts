/**
 * Recorded telemetry for the offline PayPulse demo, in the order and with the payload shapes the
 * backend emits (apps/ingestion/tasks.py, pipeline.py, apps/genome/tasks.py, apps/revival/services.py).
 * `at` is the offset in milliseconds from the moment "Resurrect" was pressed.
 */
import type { JsonValue, ProjectStatus, TelemetryEventType, TelemetryFrame } from "../types";
import {
  MOCK_BRANCH_HEAD,
  MOCK_BRANCH_NAME,
  MOCK_CONFLICTS,
  MOCK_FEATURE_NODES,
  MOCK_MAIN_HEAD,
  MOCK_PROJECT_ID,
  MOCK_PROJECT_NAME,
} from "./paypulse";

export interface ScriptedEvent {
  at: number;
  event_type: TelemetryEventType;
  payload: Record<string, JsonValue>;
}

const MAIN_FILES: { path: string; language: string | null; symbols: number; markers: number }[] = [
  { path: ".gitignore", language: null, symbols: 0, markers: 0 },
  { path: "README.md", language: "markdown", symbols: 0, markers: 0 },
  { path: "docs/architecture-notes.md", language: "markdown", symbols: 0, markers: 0 },
  { path: "paypulse/__init__.py", language: "python", symbols: 1, markers: 0 },
  { path: "paypulse/currency.py", language: "python", symbols: 5, markers: 2 },
  { path: "paypulse/invoices.py", language: "python", symbols: 13, markers: 0 },
  { path: "paypulse/ledger.py", language: "python", symbols: 24, markers: 0 },
  { path: "paypulse/webhooks.py", language: "python", symbols: 9, markers: 1 },
  { path: "requirements.txt", language: null, symbols: 0, markers: 0 },
  { path: "setup.cfg", language: null, symbols: 0, markers: 0 },
  { path: "tests/__init__.py", language: "python", symbols: 0, markers: 0 },
  { path: "tests/test_invoices.py", language: "python", symbols: 6, markers: 0 },
  { path: "tests/test_ledger.py", language: "python", symbols: 8, markers: 0 },
  { path: "tests/test_webhooks.py", language: "python", symbols: 7, markers: 0 },
];

const BRANCH_FILES = [
  { path: "paypulse/retry.py", language: "python", symbols: 6, markers: 1 },
  { path: "tests/test_retry.py", language: "python", symbols: 5, markers: 0 },
];

function astFiles(files: typeof MAIN_FILES): JsonValue {
  return files.map((file) => ({ ...file, status: "PARSED" }));
}

const NODE_START = 4200;
const NODE_STEP = 420;
const CONFLICT_START = NODE_START + NODE_STEP * MOCK_FEATURE_NODES.length + 200;
const CONFLICT_STEP = 380;
export const MOCK_GENOME_COMPLETE_AT = CONFLICT_START + CONFLICT_STEP * MOCK_CONFLICTS.length + 300;
export const MOCK_INGESTION_COMPLETE_AT = 3200;

const STRATEGIES: { type: string; title: string; score: number; v: number; d: number; r: number; inScope: number; deferred: number; items: number }[] = [
  { type: "RESTORE", title: "Restore PayPulse in its current stack", score: 68.1, v: 0.681, d: 0.3333, r: 0.3, inScope: 2, deferred: 1, items: 6 },
  { type: "MODERNIZE", title: "Restore and modernize PayPulse's pinned dependencies", score: 58.1, v: 0.681, d: 0.4, r: 0.7, inScope: 2, deferred: 1, items: 8 },
  { type: "EXTRACT", title: "Extract the ledger and invoices core", score: 57.3, v: 0.6667, d: 0.6, r: 0.4, inScope: 2, deferred: 4, items: 3 },
];

export const MOCK_TELEMETRY_SCRIPT: ScriptedEvent[] = [
  { at: 250, event_type: "INGESTION_STARTED", payload: { name: MOCK_PROJECT_NAME, source: "demo", repo_url: null } },
  {
    at: 900,
    event_type: "BRANCH_DISCOVERED",
    payload: {
      name: "main", head_sha: MOCK_MAIN_HEAD, last_commit_date: "2023-01-18T11:39:00+00:00",
      is_default: true, is_merged: true, is_abandoned: false, commits_ahead: 0, commits_behind: 0,
    },
  },
  {
    at: 1250,
    event_type: "BRANCH_DISCOVERED",
    payload: {
      name: MOCK_BRANCH_NAME, head_sha: MOCK_BRANCH_HEAD, last_commit_date: "2021-07-24T05:50:00+00:00",
      is_default: false, is_merged: false, is_abandoned: true, commits_ahead: 4, commits_behind: 5,
    },
  },
  {
    at: 2000,
    event_type: "AST_PARSED",
    payload: { branch: "main", scope: "FULL_TREE", batch: 1, files: astFiles(MAIN_FILES), skipped: 0, files_processed: MAIN_FILES.length },
  },
  {
    at: 2600,
    event_type: "AST_PARSED",
    payload: { branch: MOCK_BRANCH_NAME, scope: "BRANCH_DIFF", batch: 1, files: astFiles(BRANCH_FILES), skipped: 0, files_processed: BRANCH_FILES.length },
  },
  {
    at: MOCK_INGESTION_COMPLETE_AT,
    event_type: "INGESTION_COMPLETE",
    payload: {
      default_branch: "main", branch_count: 2, unmerged_branch_count: 1, abandoned_branch_count: 1,
      artifact_counts: { AST_MODULE: 13, README_DOC: 2, DEPENDENCY_MANIFEST: 2, TODO_MARKER: 4, COMMIT_LOG: 1 },
      file_status_counts: { PARSED: 16 },
      status: "SYNTHESIZING_GENOME",
    },
  },
  ...MOCK_FEATURE_NODES.map((node, index): ScriptedEvent => ({
    at: NODE_START + NODE_STEP * index,
    event_type: "GENOME_NODE_CREATED",
    payload: {
      node_id: node.id,
      title: node.data.title,
      category: node.data.category,
      vital_status: node.data.vital_status,
      epistemic_status: node.data.epistemic_status,
      confidence: node.data.confidence_score,
      citation_count: node.data.citation_count,
      graph_position: { ...node.position },
    },
  })),
  ...MOCK_CONFLICTS.map((record, index): ScriptedEvent => ({
    at: CONFLICT_START + CONFLICT_STEP * index,
    event_type: "CONFLICT_DETECTED",
    payload: {
      node_id: record.feature_id,
      title: MOCK_FEATURE_NODES.find((node) => node.id === record.feature_id)?.data.title ?? "",
      claim_source: record.claim_source,
      reality_source: record.reality_source,
    },
  })),
  {
    at: MOCK_GENOME_COMPLETE_AT,
    event_type: "GENOME_COMPLETE",
    payload: {
      synthesis_id: "9d1c4b7e-2f3a-4c5d-8e6f-7a8b9c0d1e2f",
      mode: "DETERMINISTIC",
      model_id: "",
      node_count: 6,
      conflict_count: 3,
      vital_status_counts: { VERIFIED_WORKING: 3, ABANDONED_IN_PROGRESS: 2, GHOST_FEATURE: 1 },
      epistemic_status_counts: { KNOWN: 3, CONFLICTED: 3 },
      fallback_reason: null,
      status: "AUTOPSY_COMPLETE",
    },
  },
  ...STRATEGIES.map((strategy, index): ScriptedEvent => ({
    at: MOCK_GENOME_COMPLETE_AT + 400 + 300 * index,
    event_type: "STRATEGY_GENERATED",
    payload: {
      strategy_id: `5a7c0e2b-8d4f-4a1b-9c3e-6f2d1b0a0${index + 101}`,
      strategy_type: strategy.type,
      title: strategy.title,
      feasibility_score: strategy.score,
      value_score: strategy.v,
      debt_cost_score: strategy.d,
      risk_score: strategy.r,
      in_scope_count: strategy.inScope,
      deferred_count: strategy.deferred,
      item_count: strategy.items,
      is_actionable: true,
      approved_by_human: false,
      regenerated: true,
      spec_name: `lazarus-${strategy.type.toLowerCase()}-paypulse`,
      spec_mode: "DETERMINISTIC",
      synthesis_id: "c2e4a6b8-0d1f-4e3a-9b5c-7d9e1f3a5b7c",
    },
  })),
];

/** The project status the recorded run had reached `elapsedMs` after it started. */
export function mockStatusAt(elapsedMs: number): ProjectStatus {
  if (elapsedMs < 250) return "DORMANT";
  if (elapsedMs < MOCK_INGESTION_COMPLETE_AT) return "INGESTING";
  if (elapsedMs < MOCK_GENOME_COMPLETE_AT) return "SYNTHESIZING_GENOME";
  return "AUTOPSY_COMPLETE";
}

/** The scripted events as wire frames, stamped relative to `startedAt` (epoch ms). */
export function mockFrames(startedAt: number, upToElapsedMs = Number.POSITIVE_INFINITY): TelemetryFrame[] {
  return MOCK_TELEMETRY_SCRIPT.filter((event) => event.at <= upToElapsedMs).map((event, index) => ({
    type: "telemetry",
    event_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    project_id: MOCK_PROJECT_ID,
    event_type: event.event_type,
    payload: event.payload,
    timestamp: new Date(startedAt + event.at).toISOString(),
  }));
}
