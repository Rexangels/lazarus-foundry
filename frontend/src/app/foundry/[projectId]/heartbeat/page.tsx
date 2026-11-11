import type { Metadata } from "next";

import { HeartbeatView } from "@/components/heartbeat/HeartbeatView";

export const metadata: Metadata = { title: "The Heartbeat" };

/**
 * View 3: the Foundry's four agents and live sandbox terminal, then the rebirth (before/after
 * pytest, PR, unified diff, Continuity Memory) (task 8.4). The project session comes from the
 * [projectId] layout.
 */
export default function HeartbeatPage() {
  return <HeartbeatView />;
}
