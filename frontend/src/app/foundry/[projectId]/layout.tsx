import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { ProjectNav } from "@/components/shell/ProjectNav";
import { ProjectSessionProvider } from "@/components/telemetry/ProjectSession";
import { ProjectHeaderStatus, SessionErrorBanner } from "@/components/telemetry/SessionNotices";
import { isUuid } from "@/lib/api";

/**
 * The project command center. One session (project row + telemetry socket) is shared by the
 * Autopsy, Defibrillator and Heartbeat views, so switching views keeps the stream.
 */
export default async function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  if (!isUuid(projectId)) notFound();
  const id = projectId.toLowerCase();

  return (
    <ProjectSessionProvider projectId={id}>
      <AppShell nav={<ProjectNav projectId={id} />} status={<ProjectHeaderStatus />}>
        <div className="px-4 sm:px-6">
          <SessionErrorBanner />
        </div>
        {children}
      </AppShell>
    </ProjectSessionProvider>
  );
}
