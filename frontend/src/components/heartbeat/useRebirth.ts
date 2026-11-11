"use client";

import { useCallback, useEffect, useState } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { describeApiError, isApiError } from "@/lib/api";
import { REBIRTH_POLL_MS, shouldPollRebirth } from "@/lib/heartbeatView";
import type { RebirthNotReadyOut, RebirthOut } from "@/lib/types";

export type RebirthState =
  | { phase: "loading" }
  | { phase: "pending"; notReady: RebirthNotReadyOut }
  | { phase: "unavailable"; notReady: RebirthNotReadyOut }
  | { phase: "error"; message: string; offline: boolean }
  | { phase: "ready"; data: RebirthOut };

/**
 * `GET /api/projects/{id}/rebirth`: refetched when the project status changes and on every
 * REBIRTH_COMPLETE frame, and every 2.5 s while REBIRTH_PENDING and the Foundry is running.
 * An artifact on screen is never replaced by a later 409 or a failed background refetch.
 */
export function useRebirth(): { state: RebirthState; reload: () => void } {
  const { api, projectId, project, telemetry } = useProjectSession();
  const [state, setState] = useState<RebirthState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  const status = project?.status ?? null;
  const completions = telemetry.frames.filter((f) => f.event_type === "REBIRTH_COMPLETE").length;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    api
      .getRebirth(projectId)
      .then((result) => {
        if (cancelled) return;
        if (result.ready) {
          setState({ phase: "ready", data: result.data });
          return;
        }
        const { notReady } = result;
        const phase = notReady.code === "REBIRTH_UNAVAILABLE" ? "unavailable" : "pending";
        setState((previous) => (previous.phase === "ready" ? previous : { phase, notReady }));
        if (shouldPollRebirth(notReady.code, status ?? notReady.status)) timer = setTimeout(reload, REBIRTH_POLL_MS);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const offline = isApiError(cause) && (cause.kind === "offline" || cause.kind === "timeout");
        setState((previous) => (previous.phase === "ready" ? previous : { phase: "error", message: describeApiError(cause), offline }));
      });
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [api, projectId, status, completions, attempt, reload]);

  return { state, reload };
}
