/**
 * The recorded PayPulse rebirth artifact for the offline demo (`GET /api/projects/{id}/rebirth`, 200).
 *
 * paypulse-rebirth.json is real backend output: build_rebirth after the recorded RESTORE Foundry run
 * (mock/telemetry.ts MOCK_FOUNDRY_SCRIPT), with the mock's project, strategy and run ids. The human
 * approval of the current tab (who approved and when) is written over the recorded one, and the
 * artifact is stamped at the replayed REBIRTH_COMPLETE. This module is loaded on demand by the mock
 * client, so the 40 KB patch stays out of the other views' bundles.
 */
import type { RebirthOut } from "../types";
import RECORDED from "./paypulse-rebirth.json";
import { MOCK_REBIRTH_AT, type MockApprovalRecord } from "./telemetry";

export const MOCK_REBIRTH: RebirthOut = RECORDED as unknown as RebirthOut;

/** The recorded rebirth as the approval of `approval` produced it. */
export function buildMockRebirth(approval: MockApprovalRecord): RebirthOut {
  const memory = MOCK_REBIRTH.continuity_memory;
  const revival = typeof memory.revival === "object" && memory.revival !== null ? (memory.revival as Record<string, unknown>) : {};
  const createdAt = new Date(approval.approvedAt + MOCK_REBIRTH_AT).toISOString();
  return {
    ...MOCK_REBIRTH,
    created_at: createdAt,
    continuity_memory: {
      ...memory,
      generated_at: createdAt,
      revival: {
        ...revival,
        approved_by: approval.approvedBy || String(revival.approved_by ?? ""),
        approved_at: new Date(approval.approvedAt).toISOString(),
      },
    },
  };
}
