"use client";

import { useCallback, useEffect, useState } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { describeApiError, isApiError } from "@/lib/api";
import { applyApproval, classifyNotReady } from "@/lib/defibrillator";
import type { ApprovalOut, StrategiesNotReadyOut, StrategiesOut } from "@/lib/types";

export type StrategiesState =
  | { phase: "loading" }
  | { phase: "pending"; notReady: StrategiesNotReadyOut }
  | { phase: "unavailable"; notReady: StrategiesNotReadyOut }
  | { phase: "error"; message: string; offline: boolean }
  | { phase: "ready"; data: StrategiesOut };

const PENDING_POLL_MS = 2_500;

/**
 * `GET /api/projects/{id}/strategies`, refetched when the session's status changes, on every
 * STRATEGY_GENERATED (regenerated rows get new ids) and STRATEGY_APPROVED frame, and every 2.5 s
 * while pending. Strategies already on screen stay while a background refetch fails.
 */
export function useStrategies(): {
  state: StrategiesState;
  reload: () => void;
  approved: (result: ApprovalOut) => void;
} {
  const { api, projectId, project, telemetry } = useProjectSession();
  const [state, setState] = useState<StrategiesState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  const status = project?.status ?? null;
  const generated = telemetry.frames.filter((f) => f.event_type === "STRATEGY_GENERATED").length;
  const approvals = telemetry.frames.filter((f) => f.event_type === "STRATEGY_APPROVED").length;
  const frames = telemetry.frames;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    api
      .getStrategies(projectId)
      .then((result) => {
        if (cancelled) return;
        if (result.ready) {
          setState({ phase: "ready", data: result.data });
          return;
        }
        const kind = classifyNotReady(result.notReady, frames);
        setState((previous) =>
          // A deck on screen is never replaced by a transient 409 (e.g. a regeneration in progress).
          previous.phase === "ready" ? previous : { phase: kind, notReady: result.notReady },
        );
        if (kind === "pending") timer = setTimeout(reload, PENDING_POLL_MS);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const offline = isApiError(cause) && (cause.kind === "offline" || cause.kind === "timeout");
        setState((previous) =>
          previous.phase === "ready" ? previous : { phase: "error", message: describeApiError(cause), offline },
        );
      });
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // `frames` is read for the REVIVAL-error check only; the counts above decide when to refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, projectId, status, generated, approvals, attempt, reload]);

  const approved = useCallback((result: ApprovalOut) => {
    setState((previous) => (previous.phase === "ready" ? { phase: "ready", data: applyApproval(previous.data, result) } : previous));
  }, []);

  return { state, reload, approved };
}
