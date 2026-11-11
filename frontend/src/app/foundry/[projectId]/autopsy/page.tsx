import type { Metadata } from "next";

import { AutopsyView } from "@/components/autopsy/AutopsyView";

export const metadata: Metadata = { title: "The Autopsy" };

/**
 * View 1: the React Flow Genome Graph, the Evidence Citations drawer and the Conflict Inspector
 * (task 8.2). The project session comes from the [projectId] layout.
 */
export default function AutopsyPage() {
  return <AutopsyView />;
}
