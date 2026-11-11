"use client";

import { useCallback, useEffect, useState } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { describeApiError, isApiError } from "@/lib/api";
import type { GenomeGraphOut, GenomeNotReadyOut } from "@/lib/types";

export type GenomeState =
  | { phase: "loading" }
  | { phase: "pending"; notReady: GenomeNotReadyOut }
  | { phase: "unavailable"; notReady: GenomeNotReadyOut }
  | { phase: "error"; message: string; offline: boolean }
  | { phase: "ready"; genome: GenomeGraphOut };

/**
 * `GET /api/projects/{id}/genome`, refetched whenever the session's project status changes or a
 * GENOME_COMPLETE frame arrives, so a pending genome appears as soon as the backend finishes.
 * A shown genome stays on screen while a refetch is in flight.
 */
export function useGenome(): { state: GenomeState; reload: () => void } {
  const { api, projectId, project, telemetry } = useProjectSession();
  const [state, setState] = useState<GenomeState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  const status = project?.status ?? null;
  const completions = telemetry.frames.filter((frame) => frame.event_type === "GENOME_COMPLETE").length;

  useEffect(() => {
    let cancelled = false;
    api
      .getGenome(projectId)
      .then((result) => {
        if (cancelled) return;
        if (result.ready) setState({ phase: "ready", genome: result.data });
        else if (result.notReady.code === "GENOME_PENDING") setState({ phase: "pending", notReady: result.notReady });
        else setState({ phase: "unavailable", notReady: result.notReady });
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const offline = isApiError(cause) && (cause.kind === "offline" || cause.kind === "timeout");
        setState((previous) =>
          // Keep a genome that is already on screen if a background refetch fails.
          previous.phase === "ready" ? previous : { phase: "error", message: describeApiError(cause), offline },
        );
      });
    return () => {
      cancelled = true;
    };
  }, [api, projectId, status, completions, attempt]);

  return { state, reload };
}
