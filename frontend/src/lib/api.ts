/**
 * Typed fetch client for the Django Ninja API (design.md section 2, "REST API").
 *
 * Every method either resolves with the documented body or throws an {@link ApiError}
 * whose `kind` tells the UI what happened:
 *   offline           the request never got a response (backend down, DNS, or a CORS refusal;
 *                     browsers report all three as the same TypeError)
 *   timeout           no response within `timeoutMs`
 *   http              the server answered with an unexpected status (`status`, parsed `body`)
 *   invalid_response  the body was not the JSON shape we expected
 * The 409 "not ready yet" answers of the genome, strategies and rebirth reads are not errors:
 * they resolve as `{ ready: false, notReady }` so screens can wait for telemetry.
 */
import { API_BASE_URL, DEMO_MOCK } from "./config";
import { MOCK_PROJECT_ID, createMockApiClient } from "./mock/client";
import type {
  ApprovalOut,
  BranchOut,
  GenomeGraphOut,
  GenomeNotReadyCode,
  GenomeNotReadyOut,
  HealthResponse,
  IngestAccepted,
  IngestRequest,
  NotReadyOut,
  ProjectOut,
  RebirthNotReadyCode,
  RebirthNotReadyOut,
  RebirthOut,
  StrategiesNotReadyCode,
  StrategiesNotReadyOut,
  StrategiesOut,
} from "./types";

export type ApiErrorKind = "offline" | "timeout" | "http" | "invalid_response";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly body: unknown;
  readonly url: string;

  constructor(kind: ApiErrorKind, message: string, options: { status?: number | null; body?: unknown; url: string }) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.body = options.body;
    this.url = options.url;
  }

  /** The server's `code` field (409/403 bodies), if any. */
  get code(): string | null {
    return isRecord(this.body) && typeof this.body.code === "string" ? this.body.code : null;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

export type Readiness<T, NotReady> = { ready: true; data: T } | { ready: false; notReady: NotReady };

export interface ApiClient {
  readonly mode: "http" | "mock";
  health(): Promise<HealthResponse>;
  ingest(request: IngestRequest): Promise<IngestAccepted>;
  /** The 1-click golden demo: `POST /api/projects/ingest {"demo": "paypulse"}`. */
  resurrectPatientZero(): Promise<IngestAccepted>;
  getProject(projectId: string): Promise<ProjectOut>;
  getBranches(projectId: string): Promise<BranchOut[]>;
  getGenome(projectId: string): Promise<Readiness<GenomeGraphOut, GenomeNotReadyOut>>;
  getStrategies(projectId: string): Promise<Readiness<StrategiesOut, StrategiesNotReadyOut>>;
  approveStrategy(
    projectId: string,
    strategyId: string,
    approvalToken: string,
    options?: { approver?: string },
  ): Promise<ApprovalOut>;
  getRebirth(projectId: string): Promise<Readiness<RebirthOut, RebirthNotReadyOut>>;
  /** Absolute URL of a spec document download (`document_urls` are API-relative paths). */
  specDocumentUrl(path: string): string;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Project and strategy ids are UUIDs; anything else is refused before it reaches a URL or route. */
export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A readable message from a Ninja error body: `{detail: str}` or a 422 `{detail: [{msg}]}`. */
export function errorDetail(body: unknown): string | null {
  if (!isRecord(body)) return typeof body === "string" && body.trim() ? body.trim() : null;
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (isRecord(item) && typeof item.msg === "string" ? item.msg : null))
      .filter((msg): msg is string => msg !== null);
    return messages.length ? messages.join("; ") : null;
  }
  return null;
}

/** One-line, user-facing description of any error thrown by the client. */
export function describeApiError(error: unknown): string {
  if (!isApiError(error)) return error instanceof Error ? error.message : "Unexpected error.";
  switch (error.kind) {
    case "offline":
      return "The Lazarus backend is unreachable. Start Daphne on port 8000, or open the recorded demo.";
    case "timeout":
      return "The Lazarus backend did not answer in time.";
    case "invalid_response":
      return "The Lazarus backend sent a response this UI does not understand.";
    case "http": {
      const detail = errorDetail(error.body);
      return detail ? detail : `The Lazarus backend answered HTTP ${error.status ?? "?"}.`;
    }
  }
}

function isNotReady<Code extends string>(body: unknown, codes: readonly Code[]): body is NotReadyOut<Code> {
  return (
    isRecord(body) &&
    typeof body.code === "string" &&
    (codes as readonly string[]).includes(body.code) &&
    typeof body.status === "string"
  );
}

function assertIngestAccepted(body: unknown, url: string): IngestAccepted {
  if (
    isRecord(body) &&
    isUuid(body.project_id) &&
    typeof body.telemetry_ws_path === "string" &&
    typeof body.status === "string"
  ) {
    return body as unknown as IngestAccepted;
  }
  throw new ApiError("invalid_response", "The ingest response has no valid project_id.", { body, url });
}

function requireUuid(value: string, what: string): string {
  if (!isUuid(value)) throw new TypeError(`${what} must be a UUID, got ${JSON.stringify(value)}`);
  return value;
}

export interface HttpApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

interface RequestOptions {
  body?: unknown;
  headers?: Record<string, string>;
  /** Statuses that resolve (with their parsed body) instead of throwing. */
  accept: readonly number[];
}

export function createHttpApiClient(options: HttpApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? API_BASE_URL).replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
  const timeoutMs = options.timeoutMs ?? 15_000;

  async function request(
    method: "GET" | "POST",
    path: string,
    { body, headers, accept }: RequestOptions,
  ): Promise<{ status: number; body: unknown; url: string }> {
    const url = `${baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(url, {
        method,
        headers: {
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          ...headers,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: "no-store",
      });
    } catch (cause) {
      if (controller.signal.aborted) {
        throw new ApiError("timeout", `No response from ${url} within ${timeoutMs} ms.`, { url });
      }
      throw new ApiError("offline", `Could not reach ${url}: ${cause instanceof Error ? cause.message : cause}`, {
        url,
      });
    } finally {
      clearTimeout(timer);
    }

    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        if (accept.includes(response.status)) {
          throw new ApiError("invalid_response", `${url} did not return JSON.`, { status: response.status, url });
        }
        parsed = text;
      }
    }
    if (!accept.includes(response.status)) {
      throw new ApiError("http", errorDetail(parsed) ?? `HTTP ${response.status} from ${url}`, {
        status: response.status,
        body: parsed,
        url,
      });
    }
    return { status: response.status, body: parsed, url };
  }

  async function readiness<T, Code extends string>(
    path: string,
    codes: readonly Code[],
  ): Promise<Readiness<T, NotReadyOut<Code>>> {
    const { status, body, url } = await request("GET", path, { accept: [200, 409] });
    if (status === 200) return { ready: true, data: body as T };
    if (isNotReady(body, codes)) return { ready: false, notReady: body };
    throw new ApiError("http", errorDetail(body) ?? `Unexpected 409 from ${url}`, { status, body, url });
  }

  const client: ApiClient = {
    mode: "http",
    async health() {
      return (await request("GET", "/health", { accept: [200] })).body as HealthResponse;
    },
    async ingest(payload) {
      const { body, url } = await request("POST", "/projects/ingest", { body: payload, accept: [202] });
      return assertIngestAccepted(body, url);
    },
    resurrectPatientZero() {
      return client.ingest({ demo: "paypulse" });
    },
    async getProject(projectId) {
      const path = `/projects/${requireUuid(projectId, "projectId")}`;
      return (await request("GET", path, { accept: [200] })).body as ProjectOut;
    },
    async getBranches(projectId) {
      const path = `/projects/${requireUuid(projectId, "projectId")}/branches`;
      return (await request("GET", path, { accept: [200] })).body as BranchOut[];
    },
    getGenome(projectId) {
      return readiness<GenomeGraphOut, GenomeNotReadyCode>(
        `/projects/${requireUuid(projectId, "projectId")}/genome`,
        ["GENOME_PENDING", "GENOME_UNAVAILABLE"],
      );
    },
    getStrategies(projectId) {
      return readiness<StrategiesOut, StrategiesNotReadyCode>(
        `/projects/${requireUuid(projectId, "projectId")}/strategies`,
        ["STRATEGIES_PENDING", "STRATEGIES_UNAVAILABLE"],
      );
    },
    async approveStrategy(projectId, strategyId, approvalToken, approveOptions = {}) {
      const path =
        `/projects/${requireUuid(projectId, "projectId")}` +
        `/strategies/${requireUuid(strategyId, "strategyId")}/approve`;
      const headers: Record<string, string> = approveOptions.approver
        ? { "X-Lazarus-Approver": approveOptions.approver }
        : {};
      const { body } = await request("POST", path, {
        body: { approval_token: approvalToken },
        headers,
        accept: [200],
      });
      return body as ApprovalOut;
    },
    getRebirth(projectId) {
      return readiness<RebirthOut, RebirthNotReadyCode>(
        `/projects/${requireUuid(projectId, "projectId")}/rebirth`,
        ["REBIRTH_PENDING", "REBIRTH_UNAVAILABLE"],
      );
    },
    specDocumentUrl(path) {
      if (/^https?:\/\//i.test(path)) return path;
      const apiRoot = baseUrl.replace(/\/api$/, "");
      return path.startsWith("/api/") ? `${apiRoot}${path}` : `${baseUrl}${path.startsWith("/") ? "" : "/"}${path}`;
    },
  };
  return client;
}

let httpClient: ApiClient | null = null;
let mockClient: ApiClient | null = null;

/** The recorded PayPulse demo runs under this id, with or without NEXT_PUBLIC_DEMO_MOCK. */
export function isMockProject(projectId: string | null | undefined): boolean {
  return projectId === MOCK_PROJECT_ID;
}

export function getMockApiClient(): ApiClient {
  mockClient ??= createMockApiClient();
  return mockClient;
}

/**
 * The client a screen should use: the mock client when NEXT_PUBLIC_DEMO_MOCK=1 or the project is
 * the recorded demo, otherwise the HTTP client.
 */
export function getApiClient(projectId?: string | null): ApiClient {
  if (DEMO_MOCK || isMockProject(projectId)) return getMockApiClient();
  httpClient ??= createHttpApiClient();
  return httpClient;
}

export { MOCK_PROJECT_ID };
