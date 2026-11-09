import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { telemetryUrl } from "./config";
import type { TelemetryFrame } from "./types";
import { CLOSE_PROJECT_NOT_FOUND, mergeFrames, parseServerFrame, reconnectDelay, shouldReconnect } from "./ws";

const PROJECT = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b";

function frame(id: number, ms: number): TelemetryFrame {
  return {
    type: "telemetry",
    event_id: `00000000-0000-4000-8000-${String(id).padStart(12, "0")}`,
    project_id: PROJECT,
    event_type: "AST_PARSED",
    payload: {},
    timestamp: new Date(1_700_000_000_000 + ms).toISOString(),
  };
}

describe("telemetryUrl", () => {
  it("joins the backend path onto the /ws base without doubling /ws", () => {
    expect(telemetryUrl(`/ws/projects/${PROJECT}/telemetry/`, "ws://localhost:8000/ws")).toBe(
      `ws://localhost:8000/ws/projects/${PROJECT}/telemetry/`,
    );
    expect(telemetryUrl(`/ws/projects/${PROJECT}/telemetry/`, "ws://localhost:8000")).toBe(
      `ws://localhost:8000/ws/projects/${PROJECT}/telemetry/`,
    );
  });

  it("turns http(s) and relative bases into ws(s) URLs", () => {
    expect(telemetryUrl("/ws/x/", "https://lab.example/ws")).toBe("wss://lab.example/ws/x/");
    expect(telemetryUrl("/ws/x/", "/ws", "http://localhost:3000")).toBe("ws://localhost:3000/ws/x/");
  });
});

describe("parseServerFrame", () => {
  it("accepts telemetry, pong and error frames", () => {
    expect(parseServerFrame(JSON.stringify(frame(1, 0)))?.type).toBe("telemetry");
    expect(parseServerFrame('{"type":"pong"}')).toEqual({ type: "pong" });
    expect(parseServerFrame('{"type":"error","detail":"no"}')).toEqual({ type: "error", detail: "no" });
  });

  it("rejects unknown event types and malformed JSON", () => {
    expect(parseServerFrame(JSON.stringify({ ...frame(1, 0), event_type: "NOPE" }))).toBeNull();
    expect(parseServerFrame("{not json")).toBeNull();
    expect(parseServerFrame("[]")).toBeNull();
  });
});

describe("reconnect policy", () => {
  it("never reconnects after 4404 (unknown project) or a normal close", () => {
    expect(shouldReconnect(CLOSE_PROJECT_NOT_FOUND)).toBe(false);
    expect(shouldReconnect(1000)).toBe(false);
    expect(shouldReconnect(1006)).toBe(true);
    expect(shouldReconnect(1011)).toBe(true);
  });

  it("property: delays grow from 1 s and stay within the 15 s cap plus 30 % jitter", () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 50 }), fc.double({ min: 0, max: 1, maxExcluded: true, noNaN: true }), (attempt, r) => {
        const delay = reconnectDelay(attempt, () => r);
        expect(delay).toBeGreaterThanOrEqual(1000);
        expect(delay).toBeLessThanOrEqual(19_500);
        expect(reconnectDelay(attempt + 1, () => r)).toBeGreaterThanOrEqual(delay);
      }),
    );
  });
});

describe("mergeFrames", () => {
  /** **Validates: apps/telemetry/events.py (clients order by timestamp, de-duplicate by event_id)** */
  it("property: merged frames are unique by event_id, ordered by timestamp, and merging is idempotent", () => {
    const frames = fc.array(fc.tuple(fc.integer({ min: 1, max: 40 }), fc.integer({ min: 0, max: 10_000 })), {
      maxLength: 60,
    });
    fc.assert(
      fc.property(frames, frames, (a, b) => {
        // An event id always carries the same timestamp, as on the wire.
        const stamp = (id: number) => (id * 7919) % 10_000;
        const left = a.map(([id]) => frame(id, stamp(id)));
        const right = b.map(([id]) => frame(id, stamp(id)));
        const merged = mergeFrames(mergeFrames([], left), right);
        const ids = merged.map((f) => f.event_id);
        expect(new Set(ids).size).toBe(ids.length);
        expect(new Set(ids)).toEqual(new Set([...left, ...right].map((f) => f.event_id)));
        for (let i = 1; i < merged.length; i += 1) {
          expect(Date.parse(merged[i]!.timestamp)).toBeGreaterThanOrEqual(Date.parse(merged[i - 1]!.timestamp));
        }
        expect(mergeFrames(merged, right)).toBe(merged);
      }),
    );
  });

  it("keeps only the newest frames beyond the limit", () => {
    const merged = mergeFrames([], [frame(1, 10), frame(2, 20), frame(3, 30)], 2);
    expect(merged.map((f) => f.event_id.slice(-1))).toEqual(["2", "3"]);
  });
});
