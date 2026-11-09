/**
 * In-browser stand-in for the API, serving the recorded PayPulse run (NEXT_PUBLIC_DEMO_MOCK=1, or the
 * offline demo button on the landing page). The run is replayed on a clock that starts when
 * `resurrectPatientZero()` is called; the start time is kept in sessionStorage so a reload resumes
 * the same run instead of restarting it.
 *
 * Strategies and rebirth are not recorded yet, so those reads answer the same 409 bodies the backend
 * sends for a project that has not reached those stages.
 */
import type { ApiClient } from "../api";
import { telemetryWsPath } from "../config";
import type { NotReadyOut, ProjectOut } from "../types";
import { MOCK_DEFAULT_BRANCH, MOCK_PROJECT_ID, MOCK_PROJECT_NAME, buildMockGenome } from "./paypulse";
import { MOCK_GENOME_COMPLETE_AT, mockStatusAt } from "./telemetry";

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
  return now;
}

function elapsed(): number {
  return Date.now() - mockRunStartedAt();
}

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function notReady<Code extends string>(code: Code, detail: string): NotReadyOut<Code> {
  return {
    detail,
    code,
    project_id: MOCK_PROJECT_ID,
    status: mockStatusAt(elapsed()),
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
    status: mockStatusAt(ms),
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
      return {
        ready: false,
        notReady: notReady("STRATEGIES_UNAVAILABLE", "The offline demo does not include revival strategies yet."),
      };
    },
    async approveStrategy() {
      throw new Error("The offline demo cannot approve strategies.");
    },
    async getRebirth() {
      return {
        ready: false,
        notReady: notReady("REBIRTH_PENDING", "The offline demo does not include a rebirth artifact yet."),
      };
    },
    specDocumentUrl(path) {
      return path;
    },
  };
  return client;
}
