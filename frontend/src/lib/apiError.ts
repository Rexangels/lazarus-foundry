/**
 * The error type of the API clients (lib/api.ts). It lives in its own module so the mock client
 * (lib/mock/client.ts, imported by api.ts) can throw the same errors without an import cycle.
 */
export type ApiErrorKind = "offline" | "timeout" | "http" | "invalid_response";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

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
