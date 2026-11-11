import { describe, expect, it } from "vitest";

import { isApiError } from "../apiError";
import { isApprovalToken } from "../defibrillator";
import type { TelemetryFrame } from "../types";
import { createMockApiClient, mockApproval, mockProjectStatus, restartMockRun, subscribeMockFrames } from "./client";
import { MOCK_PROJECT_ID } from "./paypulse";
import { MOCK_STRATEGIES, buildMockStrategies, mockApprove, type MockApproveInput } from "./strategies";
import { MOCK_STRATEGIES_READY_AT, MOCK_STRATEGY_SUMMARY } from "./telemetry";

const [RESTORE, MODERNIZE] = MOCK_STRATEGIES as [(typeof MOCK_STRATEGIES)[number], (typeof MOCK_STRATEGIES)[number]];

function input(over: Partial<MockApproveInput> = {}): MockApproveInput {
  return {
    status: "AUTOPSY_COMPLETE",
    approval: null,
    strategyId: RESTORE.id,
    token: RESTORE.approval.approval_token,
    approver: "",
    now: 5_000,
    runStartedAt: 0,
    foundryRunId: "7e1d2c3b-4a5f-4e6d-8c7b-9a0f1e2d3c4b",
    ...over,
  };
}

describe("recorded strategies", () => {
  it("are the backend's three strategies with valid approval tokens, and the replay summary agrees", () => {
    expect(MOCK_STRATEGIES.map((s) => s.strategy_type)).toEqual(["RESTORE", "MODERNIZE", "EXTRACT"]);
    expect(MOCK_STRATEGIES.every((s) => isApprovalToken(s.approval.approval_token) && s.is_actionable)).toBe(true);
    expect(MOCK_STRATEGY_SUMMARY).toEqual(
      MOCK_STRATEGIES.map((s) => ({
        id: s.id,
        type: s.strategy_type,
        title: s.title,
        score: s.feasibility_score,
        v: s.value_score,
        d: s.debt_cost_score,
        r: s.risk_score,
        inScope: s.action_plan.in_scope.length,
        deferred: s.action_plan.deferred.length,
        items: s.action_plan.items.length,
      })),
    );
  });

  it("build the strategies body with the recommendation and no approval", () => {
    const data = buildMockStrategies("AUTOPSY_COMPLETE", null);
    expect(data.recommended_strategy_id).toBe(RESTORE.id);
    expect(data.approved_strategy_id).toBeNull();
    expect(data.strategies.filter((s) => s.is_recommended).map((s) => s.id)).toEqual([RESTORE.id]);
    expect(data.strategies[0]?.spec.document_urls.tasks_md).toBe(
      `/api/projects/${MOCK_PROJECT_ID}/strategies/${RESTORE.id}/specs/tasks.md`,
    );
  });
});

describe("mockApprove (services.approve_strategy's order)", () => {
  it("approves with the current token, then answers an identical repeat idempotently", () => {
    const first = mockApprove(input({ approver: "Dr. F" }));
    expect(first.ok).toBe(true);
    if (!first.ok) return;
    expect(first.body.already_approved).toBe(false);
    expect(first.body.project_status).toBe("STRATEGY_APPROVED");
    expect(first.body.strategy.approval).toMatchObject({ approved_by_human: true, approved_by: "Dr. F", approval_intact: true });
    const repeat = mockApprove(input({ approval: first.approval, status: "STRATEGY_APPROVED" }));
    expect(repeat.ok && repeat.body.already_approved).toBe(true);
  });

  it("refuses like the backend", () => {
    const approved = mockApprove(input());
    if (!approved.ok) throw new Error("approval failed");
    const code = (outcome: ReturnType<typeof mockApprove>) => (!outcome.ok && outcome.status === 409 ? outcome.body.code : outcome.ok ? 200 : 404);
    expect(code(mockApprove(input({ approval: approved.approval, token: "0".repeat(64) })))).toBe("STRATEGY_STALE");
    expect(code(mockApprove(input({ approval: approved.approval, strategyId: MODERNIZE.id, token: MODERNIZE.approval.approval_token })))).toBe(
      "ANOTHER_STRATEGY_APPROVED",
    );
    expect(code(mockApprove(input({ status: "SYNTHESIZING_GENOME" })))).toBe("PROJECT_NOT_READY");
    const stale = mockApprove(input({ token: "f".repeat(64) }));
    expect(!stale.ok && stale.status === 409 && stale.body.current_approval_token).toBe(RESTORE.approval.approval_token);
    expect(code(mockApprove(input({ strategyId: "00000000-0000-4000-8000-000000000000" })))).toBe(404);
  });
});

describe("mock client approval", () => {
  it("approves through the client, moves the project to STRATEGY_APPROVED and emits STRATEGY_APPROVED", async () => {
    restartMockRun(Date.now() - MOCK_STRATEGIES_READY_AT - 1_000);
    const client = createMockApiClient();
    expect(mockProjectStatus()).toBe("AUTOPSY_COMPLETE");
    const read = await client.getStrategies(MOCK_PROJECT_ID);
    expect(read.ready).toBe(true);
    if (!read.ready) return;
    const restore = read.data.strategies[0]!;

    const frames: TelemetryFrame[] = [];
    const unsubscribe = subscribeMockFrames((batch) => frames.push(...batch));
    const result = await client.approveStrategy(MOCK_PROJECT_ID, restore.id, restore.approval.approval_token, { approver: "Igor" });
    unsubscribe();

    expect(result.already_approved).toBe(false);
    expect(mockApproval()?.strategyId).toBe(restore.id);
    expect(mockProjectStatus()).toBe("STRATEGY_APPROVED");
    expect((await client.getProject(MOCK_PROJECT_ID)).status).toBe("STRATEGY_APPROVED");
    expect(frames.map((f) => [f.event_type, f.payload.strategy_type, f.payload.approved_by])).toEqual([
      ["STRATEGY_APPROVED", "RESTORE", "Igor"],
    ]);

    const other = read.data.strategies[1]!;
    const refused = await client.approveStrategy(MOCK_PROJECT_ID, other.id, other.approval.approval_token).catch((e: unknown) => e);
    expect(isApiError(refused) && refused.status === 409 && refused.code).toBe("ANOTHER_STRATEGY_APPROVED");

    restartMockRun();
    expect(mockApproval()).toBeNull(); // a new run forgets the approval
  });
});
