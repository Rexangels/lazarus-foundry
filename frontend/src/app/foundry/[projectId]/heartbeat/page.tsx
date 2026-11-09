import type { Metadata } from "next";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Panel } from "@/components/ui/Panel";

export const metadata: Metadata = { title: "The Heartbeat" };

/** View 3. Placeholder: the 4-agent pipeline, live terminal, pytest before/after, diff and roadmap arrive in task 8.4. */
export default function HeartbeatPage() {
  return (
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">
      <ViewHeader
        index="03"
        title="The Heartbeat"
        metaphor="Four agents work on the patient in a sandbox until pytest comes back green, then the verified diff and the continuity roadmap are handed over."
      />
      <ResurrectionMonitor compact />
      <Panel eyebrow="Foundry" title="Live foundry and rebirth" headingId="foundry-heading">
        <p className="text-ink-muted">
          The agent pipeline, the live sandbox terminal, the before and after test runs, the unified diff and the
          continuity memory open here.
        </p>
      </Panel>
    </div>
  );
}
