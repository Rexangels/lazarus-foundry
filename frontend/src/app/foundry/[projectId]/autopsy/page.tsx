import type { Metadata } from "next";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Panel } from "@/components/ui/Panel";

export const metadata: Metadata = { title: "The Autopsy" };

/**
 * View 1. Placeholder: the live resurrection monitor. The React Flow genome graph, Evidence Drawer
 * and Conflict Inspector (task 8.2) mount below it.
 */
export default function AutopsyPage() {
  return (
    <div className="mx-auto max-w-7xl space-y-6 py-8 sm:py-10 px-4 sm:px-6">
      <ViewHeader
        index="01"
        title="The Autopsy"
        metaphor="What was this software meant to become, what still works, what died mid-flight, and where the README lies about the code."
      />
      <ResurrectionMonitor />
      <Panel eyebrow="Genome graph" title="Project Genome" headingId="genome-graph-heading">
        <p className="text-ink-muted">
          The genome graph, the evidence ledger with cited line ranges and the conflict inspector open here once the
          genome is reconstructed.
        </p>
      </Panel>
    </div>
  );
}
