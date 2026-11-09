/**
 * The HTTP client against a real local HTTP server that answers like the Django Ninja API.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { ApiError, createHttpApiClient, describeApiError, errorDetail, isUuid } from "./api";

const PROJECT_ID = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b";
const STRATEGY_ID = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";

interface Seen {
  method: string;
  url: string;
  body: string;
  headers: IncomingMessage["headers"];
}

let server: Server;
let baseUrl: string;
const seen: Seen[] = [];

function send(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

beforeAll(async () => {
  server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      seen.push({ method: req.method ?? "", url: req.url ?? "", body, headers: req.headers });
      const url = req.url ?? "";
      if (req.method === "POST" && url === "/api/projects/ingest") {
        const payload = JSON.parse(body) as Record<string, unknown>;
        if (payload.demo === "paypulse") {
          return send(res, 202, {
            project_id: PROJECT_ID,
            name: "PayPulse (Patient Zero)",
            status: "DORMANT",
            is_golden_demo: true,
            telemetry_ws_path: `/ws/projects/${PROJECT_ID}/telemetry/`,
          });
        }
        if (payload.repo_url === "https://github.com/queue/down") {
          return send(res, 503, { detail: "Ingestion could not be queued.", project_id: PROJECT_ID, status: "FAILED" });
        }
        return send(res, 422, { detail: [{ type: "value_error", loc: ["body"], msg: "Value error, host not allowed" }] });
      }
      if (url === `/api/projects/${PROJECT_ID}/genome`) {
        return send(res, 409, {
          detail: "The genome is not ready.",
          code: "GENOME_PENDING",
          project_id: PROJECT_ID,
          status: "INGESTING",
          status_detail: "",
          telemetry_ws_path: `/ws/projects/${PROJECT_ID}/telemetry/`,
        });
      }
      if (url === `/api/projects/${PROJECT_ID}/strategies`) {
        return send(res, 200, { project: { id: PROJECT_ID }, strategies: [] });
      }
      if (url === `/api/projects/${PROJECT_ID}/strategies/${STRATEGY_ID}/approve`) {
        return send(res, 409, {
          detail: "Stale.",
          code: "STRATEGY_STALE",
          project_id: PROJECT_ID,
          strategy_id: STRATEGY_ID,
          current_approval_token: "b".repeat(64),
          approved_strategy_id: null,
        });
      }
      if (url === "/api/slow") return; // never answers
      if (url === `/api/projects/${PROJECT_ID}/rebirth`) {
        res.writeHead(200, { "Content-Type": "text/html" });
        return res.end("<html>proxy error page</html>");
      }
      return send(res, 404, { detail: "Not Found" });
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}/api`;
});

afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

describe("createHttpApiClient", () => {
  it("triggers the golden demo with POST /api/projects/ingest {demo: paypulse}", async () => {
    const accepted = await createHttpApiClient({ baseUrl }).resurrectPatientZero();
    expect(accepted.project_id).toBe(PROJECT_ID);
    expect(accepted.telemetry_ws_path).toBe(`/ws/projects/${PROJECT_ID}/telemetry/`);
    const request = seen.find((entry) => entry.url === "/api/projects/ingest" && entry.body.includes("paypulse"));
    expect(request?.method).toBe("POST");
    expect(JSON.parse(request?.body ?? "{}")).toEqual({ demo: "paypulse" });
    expect(request?.headers["content-type"]).toBe("application/json");
  });

  it("reports a 503 (queue down) as an http ApiError with the server's detail", async () => {
    const error = await createHttpApiClient({ baseUrl })
      .ingest({ repo_url: "https://github.com/queue/down" })
      .catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("http");
    expect((error as ApiError).status).toBe(503);
    expect(describeApiError(error)).toBe("Ingestion could not be queued.");
  });

  it("joins 422 validation messages", async () => {
    const error = await createHttpApiClient({ baseUrl })
      .ingest({ repo_url: "https://example.org/x" })
      .catch((cause: unknown) => cause);
    expect((error as ApiError).status).toBe(422);
    expect(describeApiError(error)).toBe("Value error, host not allowed");
  });

  it("resolves a 409 GENOME_PENDING as not ready instead of throwing", async () => {
    const result = await createHttpApiClient({ baseUrl }).getGenome(PROJECT_ID);
    expect(result.ready).toBe(false);
    if (!result.ready) {
      expect(result.notReady.code).toBe("GENOME_PENDING");
      expect(result.notReady.status).toBe("INGESTING");
    }
  });

  it("resolves a 200 read as ready", async () => {
    const result = await createHttpApiClient({ baseUrl }).getStrategies(PROJECT_ID);
    expect(result.ready).toBe(true);
  });

  it("keeps the approval error code on the ApiError", async () => {
    const error = await createHttpApiClient({ baseUrl })
      .approveStrategy(PROJECT_ID, STRATEGY_ID, "a".repeat(64), { approver: "Dana" })
      .catch((cause: unknown) => cause);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).code).toBe("STRATEGY_STALE");
    const request = seen.find((entry) => entry.url.endsWith("/approve"));
    expect(JSON.parse(request?.body ?? "{}")).toEqual({ approval_token: "a".repeat(64) });
    expect(request?.headers["x-lazarus-approver"]).toBe("Dana");
  });

  it("maps 404 to an http error", async () => {
    const error = await createHttpApiClient({ baseUrl })
      .getProject("00000000-0000-4000-8000-000000000000")
      .catch((cause: unknown) => cause);
    expect((error as ApiError).kind).toBe("http");
    expect((error as ApiError).status).toBe(404);
  });

  it("flags a non-JSON success body as invalid_response", async () => {
    const error = await createHttpApiClient({ baseUrl }).getRebirth(PROJECT_ID).catch((cause: unknown) => cause);
    expect((error as ApiError).kind).toBe("invalid_response");
  });

  it("reports an unreachable backend as offline", async () => {
    const closed = createServer();
    await new Promise<void>((resolve) => closed.listen(0, "127.0.0.1", resolve));
    const port = (closed.address() as AddressInfo).port;
    await new Promise<void>((resolve) => closed.close(() => resolve()));
    const error = await createHttpApiClient({ baseUrl: `http://127.0.0.1:${port}/api` })
      .health()
      .catch((cause: unknown) => cause);
    expect((error as ApiError).kind).toBe("offline");
    expect(describeApiError(error)).toMatch(/unreachable/);
  });

  it("times out a request that never answers", async () => {
    // Route health() to the server's never-answering /api/slow.
    const client = createHttpApiClient({
      baseUrl,
      timeoutMs: 150,
      fetchImpl: (input, init) => fetch(String(input).replace(/\/health$/, "/slow"), init),
    });
    const error = await client.health().catch((cause: unknown) => cause);
    expect((error as ApiError).kind).toBe("timeout");
  });

  it("refuses non-UUID ids before building a URL", async () => {
    const client = createHttpApiClient({ baseUrl });
    await expect(client.getProject("../../admin")).rejects.toThrow(TypeError);
  });

  it("builds spec document URLs from API-relative paths", () => {
    expect(createHttpApiClient({ baseUrl: "http://localhost:8000/api" }).specDocumentUrl("/api/projects/x/specs/tasks.md")).toBe(
      "http://localhost:8000/api/projects/x/specs/tasks.md",
    );
    expect(createHttpApiClient({ baseUrl: "/api" }).specDocumentUrl("/api/projects/x/specs/tasks.md")).toBe(
      "/api/projects/x/specs/tasks.md",
    );
  });
});

describe("helpers", () => {
  it("validates UUIDs", () => {
    expect(isUuid(PROJECT_ID)).toBe(true);
    expect(isUuid("not-a-uuid")).toBe(false);
    expect(isUuid(`${PROJECT_ID}/../x`)).toBe(false);
  });

  it("reads Ninja error bodies", () => {
    expect(errorDetail({ detail: "Not Found" })).toBe("Not Found");
    expect(errorDetail({ detail: [{ msg: "a" }, { msg: "b" }] })).toBe("a; b");
    expect(errorDetail(null)).toBeNull();
  });
});
