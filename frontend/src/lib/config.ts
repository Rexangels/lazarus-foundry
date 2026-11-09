/**
 * Runtime configuration from NEXT_PUBLIC_* variables (inlined by Next.js at build time).
 *
 * NEXT_PUBLIC_API_BASE_URL  REST base including `/api` (default http://localhost:8000/api).
 *                           Set it to `/api` to go through the Next.js proxy in next.config.ts.
 * NEXT_PUBLIC_WS_BASE_URL   WebSocket base including `/ws` (default ws://localhost:8000/ws).
 * NEXT_PUBLIC_DEMO_MOCK     `1` serves every project from the recorded PayPulse data in
 *                           src/lib/mock/ (no backend needed).
 */

export const DEFAULT_API_BASE_URL = "http://localhost:8000/api";
export const DEFAULT_WS_BASE_URL = "ws://localhost:8000/ws";

function trimTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, "");
}

function readEnv(value: string | undefined, fallback: string): string {
  const trimmed = value?.trim();
  return trimmed ? trimTrailingSlashes(trimmed) : fallback;
}

export const API_BASE_URL: string = readEnv(process.env.NEXT_PUBLIC_API_BASE_URL, DEFAULT_API_BASE_URL);
export const WS_BASE_URL: string = readEnv(process.env.NEXT_PUBLIC_WS_BASE_URL, DEFAULT_WS_BASE_URL);
export const DEMO_MOCK: boolean = ["1", "true", "yes"].includes(
  (process.env.NEXT_PUBLIC_DEMO_MOCK ?? "").trim().toLowerCase(),
);

/**
 * Absolute WebSocket URL for a backend `telemetry_ws_path` such as `/ws/projects/<id>/telemetry/`.
 *
 * The backend path already starts with `/ws`, and the configured base usually ends with it,
 * so the overlap is removed once. A relative base (`/ws`) is resolved against the page origin.
 */
export function telemetryUrl(path: string, base: string = WS_BASE_URL, pageOrigin?: string): string {
  const cleanBase = trimTrailingSlashes(base);
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  let joined: string;
  if (cleanBase.endsWith("/ws") && (cleanPath === "/ws" || cleanPath.startsWith("/ws/"))) {
    joined = cleanBase.slice(0, -"/ws".length) + cleanPath;
  } else {
    joined = cleanBase + cleanPath;
  }
  if (/^wss?:\/\//i.test(joined)) return joined;
  if (/^https?:\/\//i.test(joined)) return joined.replace(/^http/i, "ws");
  const origin = pageOrigin ?? (typeof window !== "undefined" ? window.location.origin : "http://localhost:3000");
  return origin.replace(/^http/i, "ws") + (joined.startsWith("/") ? joined : `/${joined}`);
}

/** Canonical path of a project's telemetry socket (mirrors apps.projects.api.telemetry_ws_path). */
export function telemetryWsPath(projectId: string): string {
  return `/ws/projects/${projectId}/telemetry/`;
}
