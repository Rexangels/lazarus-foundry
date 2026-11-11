"use client";

/**
 * Reconnecting telemetry WebSocket hook for `ws/projects/<uuid>/telemetry/`
 * (backend: apps/telemetry/consumers.py, events.py).
 *
 * - Frames are de-duplicated by `event_id` and ordered by `timestamp`; the server gives no
 *   sequence numbers because several processes emit events.
 * - Reconnects with capped exponential backoff and jitter. Close code 4404 (unknown project)
 *   is final. A page that becomes visible again reconnects at once.
 * - Sends `{"type": "ping"}` every 25 s so idle proxies keep the socket open.
 * - For the recorded demo project the frames are replayed on a timer instead (no socket).
 */
import { useEffect, useReducer, useRef } from "react";

import { telemetryUrl, telemetryWsPath } from "./config";
import { isMockProject } from "./api";
import { mockApproval, mockRunStartedAt, subscribeMockFrames } from "./mock/client";
import { MOCK_TELEMETRY_SCRIPT, mockApprovalFrames, mockFoundryTimeline, mockFrames } from "./mock/telemetry";
import { TELEMETRY_EVENT_TYPES, type ServerFrame, type TelemetryFrame } from "./types";

export type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "closed" | "not_found" | "replay";

export const CLOSE_PROJECT_NOT_FOUND = 4404;
export const MAX_FRAMES = 2000;
const PING_INTERVAL_MS = 25_000;

/** Parse one server message; anything that is not a known frame shape gives null. */
export function parseServerFrame(text: string): ServerFrame | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const frame = value as Record<string, unknown>;
  if (frame.type === "pong") return { type: "pong" };
  if (frame.type === "error") return { type: "error", detail: typeof frame.detail === "string" ? frame.detail : "" };
  if (
    frame.type === "telemetry" &&
    typeof frame.event_id === "string" &&
    typeof frame.project_id === "string" &&
    typeof frame.timestamp === "string" &&
    typeof frame.event_type === "string" &&
    (TELEMETRY_EVENT_TYPES as readonly string[]).includes(frame.event_type) &&
    typeof frame.payload === "object" &&
    frame.payload !== null &&
    !Array.isArray(frame.payload)
  ) {
    return frame as unknown as TelemetryFrame;
  }
  return null;
}

/** Merge new frames into an ordered, de-duplicated list, keeping the newest `limit`. */
export function mergeFrames(
  existing: readonly TelemetryFrame[],
  incoming: readonly TelemetryFrame[],
  limit: number = MAX_FRAMES,
): TelemetryFrame[] {
  const seen = new Set(existing.map((frame) => frame.event_id));
  const fresh = incoming.filter((frame) => {
    if (seen.has(frame.event_id)) return false;
    seen.add(frame.event_id);
    return true;
  });
  if (fresh.length === 0) return existing as TelemetryFrame[];
  const merged = [...existing, ...fresh];
  // Stable sort: equal timestamps keep arrival order.
  merged.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  return merged.length > limit ? merged.slice(merged.length - limit) : merged;
}

/** Delay before reconnect attempt `attempt` (0-based): 1 s doubling to 15 s, with up to 30 % jitter. */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(15_000, 1_000 * 2 ** Math.max(0, attempt));
  return Math.round(base * (1 + 0.3 * random()));
}

/** Whether a close code is worth reconnecting after. */
export function shouldReconnect(code: number): boolean {
  return code !== CLOSE_PROJECT_NOT_FOUND && code !== 1000;
}

export interface TelemetryState {
  frames: TelemetryFrame[];
  connection: ConnectionState;
  /** Last `{"type": "error"}` detail or transport problem. */
  lastError: string | null;
  attempts: number;
}

type Action =
  | { type: "reset" }
  | { type: "connection"; connection: ConnectionState; attempts?: number; error?: string | null }
  | { type: "frames"; frames: TelemetryFrame[] }
  | { type: "error"; detail: string };

const INITIAL: TelemetryState = { frames: [], connection: "idle", lastError: null, attempts: 0 };

function reducer(state: TelemetryState, action: Action): TelemetryState {
  switch (action.type) {
    case "reset":
      return INITIAL;
    case "connection":
      return {
        ...state,
        connection: action.connection,
        attempts: action.attempts ?? state.attempts,
        lastError: action.error === undefined ? state.lastError : action.error,
      };
    case "frames": {
      const frames = mergeFrames(state.frames, action.frames);
      return frames === state.frames ? state : { ...state, frames };
    }
    case "error":
      return { ...state, lastError: action.detail };
  }
}

export interface UseTelemetryOptions {
  /** Backend `telemetry_ws_path`; defaults to the canonical path for the project. */
  path?: string;
  enabled?: boolean;
  /** Called once per new frame, after de-duplication. */
  onFrame?: (frame: TelemetryFrame) => void;
}

export function useTelemetry(projectId: string | null, options: UseTelemetryOptions = {}): TelemetryState {
  const { path, enabled = true, onFrame } = options;
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const onFrameRef = useRef(onFrame);
  useEffect(() => {
    onFrameRef.current = onFrame;
  }, [onFrame]);

  useEffect(() => {
    dispatch({ type: "reset" });
    if (!projectId || !enabled) return;
    const seen = new Set<string>();
    const deliver = (frames: TelemetryFrame[]) => {
      const fresh = frames.filter((frame) => !seen.has(frame.event_id));
      fresh.forEach((frame) => seen.add(frame.event_id));
      if (fresh.length === 0) return;
      dispatch({ type: "frames", frames: fresh });
      fresh.forEach((frame) => onFrameRef.current?.(frame));
    };

    if (isMockProject(projectId)) {
      return replayMock(dispatch, deliver);
    }

    const url = telemetryUrl(path ?? telemetryWsPath(projectId));
    let socket: WebSocket | null = null;
    let attempt = 0;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;

    const clearTimers = () => {
      clearTimeout(retryTimer);
      clearInterval(pingTimer);
    };

    const connect = () => {
      if (disposed) return;
      clearTimers();
      dispatch({ type: "connection", connection: attempt === 0 ? "connecting" : "reconnecting", attempts: attempt });
      try {
        socket = new WebSocket(url);
      } catch (error) {
        dispatch({ type: "connection", connection: "closed", error: String(error) });
        return;
      }
      const current = socket;
      current.onopen = () => {
        attempt = 0;
        dispatch({ type: "connection", connection: "open", attempts: 0, error: null });
        pingTimer = setInterval(() => {
          if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify({ type: "ping" }));
        }, PING_INTERVAL_MS);
      };
      current.onmessage = (event: MessageEvent) => {
        if (typeof event.data !== "string") return;
        const frame = parseServerFrame(event.data);
        if (frame?.type === "telemetry") deliver([frame]);
        else if (frame?.type === "error") dispatch({ type: "error", detail: frame.detail });
      };
      current.onclose = (event: CloseEvent) => {
        clearInterval(pingTimer);
        if (disposed || socket !== current) return;
        if (event.code === CLOSE_PROJECT_NOT_FOUND) {
          dispatch({ type: "connection", connection: "not_found", error: "The backend does not know this project." });
          return;
        }
        if (!shouldReconnect(event.code)) {
          dispatch({ type: "connection", connection: "closed" });
          return;
        }
        const wait = reconnectDelay(attempt);
        attempt += 1;
        dispatch({
          type: "connection",
          connection: "reconnecting",
          attempts: attempt,
          error: event.code === 1011 ? "The backend could not open the telemetry stream." : "Telemetry connection lost.",
        });
        retryTimer = setTimeout(connect, wait);
      };
    };

    const onVisible = () => {
      if (document.visibilityState === "visible" && socket && socket.readyState === WebSocket.CLOSED && !disposed) {
        attempt = Math.min(attempt, 1);
        connect();
      }
    };

    connect();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      disposed = true;
      clearTimers();
      document.removeEventListener("visibilitychange", onVisible);
      if (socket && socket.readyState <= WebSocket.OPEN) socket.close(1000);
      socket = null;
    };
  }, [projectId, path, enabled]);

  return state;
}

function replayMock(dispatch: (action: Action) => void, deliver: (frames: TelemetryFrame[]) => void): () => void {
  const startedAt = mockRunStartedAt();
  dispatch({ type: "connection", connection: "replay", attempts: 0, error: null });
  const all = mockFrames(startedAt);
  const timers: ReturnType<typeof setTimeout>[] = [];
  const now = Date.now() - startedAt;
  deliver([...mockFrames(startedAt, now), ...mockApprovalFrames(mockApproval())]);
  MOCK_TELEMETRY_SCRIPT.forEach((event, index) => {
    const frame = all[index];
    if (event.at > now && frame) timers.push(setTimeout(() => deliver([frame]), event.at - now));
  });
  // The recorded Foundry run, replayed from the approval time (frames already due arrive at once).
  const foundryTimers: ReturnType<typeof setTimeout>[] = [];
  const scheduleFoundry = () => {
    foundryTimers.forEach(clearTimeout);
    foundryTimers.length = 0;
    const approval = mockApproval();
    if (!approval) return;
    const since = Date.now() - approval.approvedAt;
    const timeline = mockFoundryTimeline(approval);
    const due = timeline.filter((entry) => entry.at <= since).map((entry) => entry.frame);
    if (due.length) deliver(due);
    timeline.forEach((entry) => {
      if (entry.at > since) foundryTimers.push(setTimeout(() => deliver([entry.frame]), entry.at - since));
    });
  };
  scheduleFoundry();
  // Frames the demo emits live (a human approval in The Defibrillator), which also start the Foundry replay.
  const unsubscribe = subscribeMockFrames((frames) => {
    deliver(frames);
    if (frames.some((frame) => frame.event_type === "STRATEGY_APPROVED")) scheduleFoundry();
  });
  return () => {
    timers.forEach(clearTimeout);
    foundryTimers.forEach(clearTimeout);
    unsubscribe();
  };
}
