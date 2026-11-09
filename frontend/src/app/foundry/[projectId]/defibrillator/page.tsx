import type { Metadata } from "next";

import { ViewHeader } from "@/components/shell/ViewHeader";
import { ResurrectionMonitor } from "@/components/telemetry/ResurrectionMonitor";
import { Panel } from "@/components/ui/Panel";

export const metadata: Metadata = { title: "The Defibrillator" };

/** View 2. Placeholder: strategy cards, Score(S_k) breakdowns and the approval gate arrive in task 8.3. */
export default function DefibrillatorPage() {
  return (
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6 sm:py-10">
      <ViewHeader
        index="02"
        title="The Defibrillator"
        metaphor="Three ways to restart the heart: Restore, Modernize or Extract, each scored from 0 to 100. Nothing changes until a human approves one."
      />
      <ResurrectionMonitor compact />
      <Panel eyebrow="Strategy matrix" title="Revival strategies" headingId="strategy-matrix-heading" tone="conflict">
        <p className="text-ink-muted">
          The strategy cards, their feasibility breakdowns, the .kiro spec preview and the human approval gate open
          here.
        </p>
      </Panel>
    </div>
  );
}
