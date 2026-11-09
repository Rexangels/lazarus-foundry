"use client";

import { Button, ButtonLink } from "@/components/ui/Button";
import { ProjectStatusBadge } from "@/components/ui/StatusBadge";
import { MOCK_PROJECT_ID } from "@/lib/api";

import { ConnectionIndicator } from "./ConnectionIndicator";
import { useProjectSession } from "./ProjectSession";

/** Header status: project name, lifecycle status and telemetry connection. */
export function ProjectHeaderStatus() {
  const { project, telemetry } = useProjectSession();
  return (
    <div className="flex items-center gap-2">
      {project && (
        <>
          <span className="hidden max-w-48 truncate font-mono text-xs text-ink-muted lg:inline" title={project.name}>
            {project.name}
          </span>
          <ProjectStatusBadge status={project.status} />
        </>
      )}
      <ConnectionIndicator state={telemetry.connection} attempts={telemetry.attempts} />
    </div>
  );
}

/** Banner for a failed project fetch (backend offline, unknown project). */
export function SessionErrorBanner() {
  const { error, refresh, api } = useProjectSession();
  if (!error) return null;
  return (
    <div
      role="alert"
      className="mx-auto mt-4 flex max-w-7xl flex-wrap items-center gap-3 rounded-md border border-ghost/50 bg-ghost/10 px-4 py-3 text-sm text-ink"
    >
      <span aria-hidden className="font-mono text-ghost-text">✕</span>
      <span className="min-w-0 flex-1">
        <strong className="font-semibold text-ghost-text">{error.notFound ? "Unknown project. " : "No vital signs. "}</strong>
        {error.message}
        {error.offline && !error.notFound && " Retrying automatically."}
      </span>
      {!error.notFound && (
        <Button size="sm" variant="secondary" onClick={refresh}>
          Retry now
        </Button>
      )}
      {api.mode === "http" && (
        <ButtonLink size="sm" variant="ghost" href={`/foundry/${MOCK_PROJECT_ID}/autopsy`}>
          Open the offline demo
        </ButtonLink>
      )}
    </div>
  );
}
