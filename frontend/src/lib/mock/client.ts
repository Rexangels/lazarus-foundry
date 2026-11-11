/**
 * In-browser stand-in for the API, serving the recorded PayPulse run (NEXT_PUBLIC_DEMO_MOCK=1, or the
 * offline demo button on the landing page). The run is replayed on a clock that starts when
 * `resurrectPatientZero()` is called; the start time is kept in sessionStorage so a reload resumes
 * the same run instead of restarting it.
 *
 * The strategies are the recorded backend output (mock/strategies.ts, loaded on demand), and
 * approving one works: it is kept in sessionStorage for the current run, moves the project to
 * STRATEGY_APPROVED and emits the STRATEGY_APPROVED frame. Approving RESTORE then replays the
 * recorded Foundry run from the approval time (mock/telemetry.ts MOCK_FOUNDRY_SCRIPT, about 30 s):
 * FOUNDRY_RUNNING, then REBORN, after which the rebirth read returns the recorded RebirthOut
 * (mock/rebirth.ts, loaded on demand). Until then it answers the backend's 409 REBIRTH_PENDING body.
 * Only RESTORE was recorded; another approved strategy stays STRATEGY_APPROVED.
 */
import type { ApiClient } from "../api";
import { ApiError } from "../apiError";
import { telemetryWsPath } from "../config";
import type { NotReadyOut, ProjectOut, ProjectStatus, TelemetryFrame } from "../types";
import { MOCK_DEFAULT_BRANCH, MOCK_PROJECT_ID, MOCK_PROJECT_NAME, buildMockGenome } from "./paypulse";
import {
  MOCK_FOUNDRY_RUN_ID,
  MOCK_GENOME_COMPLETE_AT,
  MOCK_STRATEGIES_READY_AT,
  mockApprovalFrames,
  mockFoundryRecorded,
  mockFoundryStatusAt,
  mockStatusAt,
  type MockApprovalRecord,
} from "./telemetry";

export { MOCK_PROJECT_ID };

const START_KEY = "lazarus.mock.startedAt";
let memoryStart: number | null = null;

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null; // storage disabled (privacy mode)
  }
}

/** Epoch ms when the recorded run started; starts it now if it never started in this tab. */
export function mockRunStartedAt(now: number = Date.now()): number {
  if (memoryStart !== null) return memoryStart;
  const stored = Number(storage()?.getItem(START_KEY));
  memoryStart = Number.isFinite(stored) && stored > 0 && stored <= now ? stored : now;
  storage()?.setItem(START_KEY, String(memoryStart));
  return memoryStart;
}

export function restartMockRun(now: number = Date.now()): number {
  memoryStart = now;
  storage()?.setItem(START_KEY, String(now));
  saveMockApproval(null); // a new run has not been approved
  return now;
}

function elapsed(): number {
  return Date.now() - mockRunStartedAt();
}

// ---------------------------------------------------------------------------
// The offline demo's human approval (The Defibrillator). The Heartbeat replays the Foundry from
// `mockApproval().approvedAt`.
// ---------------------------------------------------------------------------
const APPROVAL_KEY = "lazarus.mock.approval";
let memoryApproval: MockApprovalRecord | null | undefined;

function isApprovalRecord(value: unknown): value is MockApprovalRecord {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.strategyId === "string" &&
    typeof v.approvalToken === "string" &&
    typeof v.approvedAt === "number" &&
    typeof v.approvedBy === "string" &&
    typeof v.foundryRunId === "string" &&
    typeof v.runStartedAt === "number"
  );
}

function saveMockApproval(record: MockApprovalRecord | null): void {
  memoryApproval = record;
  if (record) storage()?.setItem(APPROVAL_KEY, JSON.stringify(record));
  else storage()?.removeItem(APPROVAL_KEY);
}

/** The approval of the current recorded run, if a human approved a strategy in this tab. */
export function mockApproval(): MockApprovalRecord | null {
  if (memoryApproval === undefined) {
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(storage()?.getItem(APPROVAL_KEY) ?? "null");
    } catch {
      parsed = null;
    }
    memoryApproval = isApprovalRecord(parsed) ? parsed : null;
  }
  return memoryApproval && memoryApproval.runStartedAt === mockRunStartedAt() ? memoryApproval : null;
}

/**
 * The recorded project's status: the replay clock, then, once a human approved, the Foundry replay
 * clock (STRATEGY_APPROVED → FOUNDRY_RUNNING → REBORN; mock/telemetry.ts mockFoundryStatusAt).
 */
export function mockProjectStatus(now: number = Date.now()): ProjectStatus {
  const status = mockStatusAt(now - mockRunStartedAt(now));
  const approval = mockApproval();
  return status === "AUTOPSY_COMPLETE" && approval ? mockFoundryStatusAt(approval, now - approval.approvedAt) : status;
}

const frameListeners = new Set<(frames: TelemetryFrame[]) => void>();

/** Live frames the mock emits outside the scripted replay (the approval); used by lib/ws.ts. */
export function subscribeMockFrames(listener: (frames: TelemetryFrame[]) => void): () => void {
  frameListeners.add(listener);
  return () => {
    frameListeners.delete(listener);
  };
}

function emitMockFrames(frames: TelemetryFrame[]): void {
  if (frames.length) frameListeners.forEach((listener) => listener(frames));
}

/** The recorded run's id, so the replayed frames, the revival branch and the rebirth agree. */
function mockFoundryRunId(): string {
  return MOCK_FOUNDRY_RUN_ID;
}

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function notReady<Code extends string>(code: Code, detail: string): NotReadyOut<Code> {
  return {
    detail,
    code,
    project_id: MOCK_PROJECT_ID,
    status: mockProjectStatus(),
    status_detail: "",
    telemetry_ws_path: telemetryWsPath(MOCK_PROJECT_ID),
  };
}

function project(): ProjectOut {
  const ms = elapsed();
  const ingested = ms >= 3200;
  return {
    id: MOCK_PROJECT_ID,
    name: MOCK_PROJECT_NAME,
    repo_url: "",
    is_golden_demo: true,
    status: mockProjectStatus(),
    status_detail: "",
    default_branch: ingested ? MOCK_DEFAULT_BRANCH : "",
    created_at: new Date(mockRunStartedAt()).toISOString(),
    updated_at: new Date().toISOString(),
    ingested_at: ingested ? new Date(mockRunStartedAt() + 3200).toISOString() : null,
    telemetry_ws_path: telemetryWsPath(MOCK_PROJECT_ID),
    ingestion: {
      branch_count: ingested ? 2 : 0,
      abandoned_branch_count: ingested ? 1 : 0,
      artifact_counts: ingested
        ? { AST_MODULE: 13, README_DOC: 2, DEPENDENCY_MANIFEST: 2, TODO_MARKER: 4, COMMIT_LOG: 1 }
        : { AST_MODULE: 0, README_DOC: 0, DEPENDENCY_MANIFEST: 0, TODO_MARKER: 0, COMMIT_LOG: 0 },
      file_status_counts: ingested ? { main: { PARSED: 14 }, "feat/webhook-retry-engine": { PARSED: 2 } } : {},
    },
  };
}

export function createMockApiClient(): ApiClient {
  const client: ApiClient = {
    mode: "mock",
    async health() {
      return { status: "ok", service: "lazarus-foundry (offline demo)", version: "mock", timestamp: new Date().toISOString() };
    },
    async ingest() {
      return client.resurrectPatientZero();
    },
    async resurrectPatientZero() {
      await delay(450); // the charge-up is part of the show
      restartMockRun();
      return {
        project_id: MOCK_PROJECT_ID,
        name: MOCK_PROJECT_NAME,
        status: "DORMANT",
        is_golden_demo: true,
        telemetry_ws_path: telemetryWsPath(MOCK_PROJECT_ID),
      };
    },
    async getProject() {
      return project();
    },
    async getBranches() {
      return [];
    },
    async getGenome() {
      if (elapsed() < MOCK_GENOME_COMPLETE_AT) {
        return {
          ready: false,
          notReady: notReady(
            "GENOME_PENDING",
            "The genome is not ready yet. Follow the telemetry and request it again after GENOME_COMPLETE.",
          ),
        };
      }
      return { ready: true, data: buildMockGenome() };
    },
    async getStrategies() {
      const ms = elapsed();
      if (ms < MOCK_GENOME_COMPLETE_AT) {
        return {
          ready: false,
          notReady: notReady(
            "STRATEGIES_PENDING",
            `The strategies are not ready: the project is ${mockProjectStatus()}. Follow the telemetry WebSocket and request them again after STRATEGY_GENERATED.`,
          ),
        };
      }
      if (ms < MOCK_STRATEGIES_READY_AT) {
        // The moment between AUTOPSY_COMPLETE and the strategy rows being written.
        return {
          ready: false,
          notReady: notReady("STRATEGIES_UNAVAILABLE", "No revival strategies exist: the project is AUTOPSY_COMPLETE."),
        };
      }
      const { buildMockStrategies } = await import("./strategies");
      return { ready: true, data: buildMockStrategies(mockProjectStatus(), mockApproval()) };
    },
    async approveStrategy(_projectId, strategyId, approvalToken, options = {}) {
      const { mockApprove } = await import("./strategies");
      await delay(900); // the capacitor discharge is part of the show
      const url = `/api/projects/${MOCK_PROJECT_ID}/strategies/${strategyId}/approve`;
      const outcome = mockApprove({
        status: mockProjectStatus(),
        approval: mockApproval(),
        strategyId,
        token: approvalToken.trim().toLowerCase(),
        approver: (options.approver ?? "").trim(),
        now: Date.now(),
        runStartedAt: mockRunStartedAt(),
        foundryRunId: mockFoundryRunId(),
      });
      if (!outcome.ok) {
        throw new ApiError("http", outcome.body.detail, { status: outcome.status, body: outcome.body, url });
      }
      if (!outcome.body.already_approved) {
        saveMockApproval(outcome.approval);
        emitMockFrames(mockApprovalFrames(outcome.approval));
      }
      return outcome.body;
    },
    async getRebirth() {
      const status = mockProjectStatus();
      const approval = mockApproval();
      if (status === "REBORN" && mockFoundryRecorded(approval)) {
        const { buildMockRebirth } = await import("./rebirth");
        return { ready: true, data: buildMockRebirth(approval) };
      }
      const detail =
        approval && !mockFoundryRecorded(approval)
          ? "The rebirth artifact is not ready: the offline demo recorded the Foundry run of the RESTORE strategy only."
          : `The rebirth artifact is not ready: the project is ${status}. Follow the telemetry WebSocket and request it again after REBIRTH_COMPLETE.`;
      return { ready: false, notReady: notReady("REBIRTH_PENDING", detail) };
    },
    specDocumentUrl(path) {
      return path;
    },
  };
  return client;
}
