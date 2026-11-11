import type { Metadata } from "next";

import { DefibrillatorView } from "@/components/defibrillator/DefibrillatorView";

export const metadata: Metadata = { title: "The Defibrillator" };

/**
 * View 2: the Restore / Modernize / Extract strategy deck with Score(S_k) breakdowns, the .kiro spec
 * preview and the Human Approval Gate (task 8.3). The project session comes from the [projectId] layout.
 */
export default function DefibrillatorPage() {
  return <DefibrillatorView />;
}
