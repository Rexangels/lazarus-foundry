"use client";

/**
 * One project's live session, shared by every view under /foundry/[projectId]:
 * - the project row (`GET /api/projects/{id}`), polled while the pipeline is moving and refetched
 *   when a stage-ending telemetry event arrives;
 * - the telemetry stream (one WebSocket for all views);
 * - the derived five-step pipeline progress.
 * Views read it with `useProjectSession()`.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { describeApiError, getApiClient, isApiError, type ApiClient } from "@/lib/api";
import { derivePipelineProgress, isInFlight, type PipelineProgress } from "@/lib/progress";
import type { ProjectOut, TelemetryEventType, TelemetryFrame } from "@/lib/types";
import { useTelemetry, type TelemetryState } from "@/lib/ws";

const REFETCH_ON: ReadonlySet<TelemetryEventType> = new Set([
  "INGESTION_STARTED",
  "INGESTION_COMPLETE",
  "GENOME_COMPLETE",
  "STRATEGY_GENERATED",
  "STRATEGY_APPROVED",
  "AGENT_STAGE_CHANGED",
  "REBIRTH_COMPLETE",
  "ERROR",
]);

const POLL_IN_FLIGHT_MS = 3_000;
const POLL_OFFLINE_MS = 6_000;

export interface ProjectSession {
  projectId: string;
  api: ApiClient;
  project: ProjectOut | null;
  /** Set while the last project fetch failed. */
  error: { message: string; offline: boolean; notFound: boolean } | null;
  loading: boolean;
  telemetry: TelemetryState;
  progress: PipelineProgress;
  refresh: () => void;
  /** Subscribe to new telemetry frames (returns an unsubscribe function). */
  subscribe: (listener: (frame: TelemetryFrame) => void) => () => void;
}

const ProjectSessionContext = createContext<ProjectSession | null>(null);

export function useProjectSession(): ProjectSession {
  const session = useContext(ProjectSessionContext);
  if (!session) throw new Error("useProjectSession must be used inside <ProjectSessionProvider>.");
  return session;
}

export function ProjectSessionProvider({ projectId, children }: { projectId: string; children: ReactNode }) {
  const api = useMemo(() => getApiClient(projectId), [projectId]);
  const [project, setProject] = useState<ProjectOut | null>(null);
  const [error, setError] = useState<ProjectSession["error"]>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const listeners = useRef(new Set<(frame: TelemetryFrame) => void>());

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    api
      .getProject(projectId)
      .then((row) => {
        if (cancelled) return;
        setProject(row);
        setError(null);
        if (isInFlight(row.status)) timer = setTimeout(refresh, POLL_IN_FLIGHT_MS);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const notFound = isApiError(cause) && cause.status === 404;
        const offline = isApiError(cause) && (cause.kind === "offline" || cause.kind === "timeout");
        setError({
          message: notFound ? "No project with this id exists on the backend." : describeApiError(cause),
          offline,
          notFound,
        });
        if (!notFound) timer = setTimeout(refresh, POLL_OFFLINE_MS);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [api, projectId, tick, refresh]);

  const onFrame = useCallback(
    (frame: TelemetryFrame) => {
      listeners.current.forEach((listener) => listener(frame));
      if (REFETCH_ON.has(frame.event_type)) refresh();
    },
    [refresh],
  );

  // The canonical path equals the backend's telemetry_ws_path; using it directly avoids a reconnect
  // when the project row arrives.
  const telemetry = useTelemetry(error?.notFound ? null : projectId, { onFrame });

  const progress = useMemo(
    () => derivePipelineProgress(project?.status ?? null, telemetry.frames, project?.status_detail ?? ""),
    [project?.status, project?.status_detail, telemetry.frames],
  );

  const subscribe = useCallback((listener: (frame: TelemetryFrame) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo<ProjectSession>(
    () => ({ projectId, api, project, error, loading, telemetry, progress, refresh, subscribe }),
    [projectId, api, project, error, loading, telemetry, progress, refresh, subscribe],
  );

  return <ProjectSessionContext.Provider value={value}>{children}</ProjectSessionContext.Provider>;
}
