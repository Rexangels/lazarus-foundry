import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { ApiError } from "./apiError";
import {
  GATE_IDLE,
  applyApproval,
  classifyNotReady,
  comparisonRows,
  defaultSelection,
  deferredItems,
  describeApprovalFailure,
  describeRisk,
  featureTitles,
  formatDecimal,
  formatScore,
  gateAvailability,
  gateReducer,
  generatedFromFrames,
  highestScoreIds,
  planItems,
  readScoreBreakdown,
  recommendedStrategyId,
  sequenceStep,
  substitutedFormula,
  type GateAction,
  type GatePhase,
} from "./defibrillator";
import { MOCK_FEATURE_IDS } from "./mock/paypulse";
import { MOCK_STRATEGIES, buildMockStrategies, mockApprove } from "./mock/strategies";
import { MOCK_GENOME_COMPLETE_AT, mockFrames, type MockApprovalRecord } from "./mock/telemetry";
import type { StrategiesNotReadyOut, StrategyOut } from "./types";

const byType = (type: string) => MOCK_STRATEGIES.find((s) => s.strategy_type === type) as StrategyOut;
const RESTORE = byType("RESTORE");
const MODERNIZE = byType("MODERNIZE");
const EXTRACT = byType("EXTRACT");

describe("readScoreBreakdown", () => {
  it("reads PlanScore.as_dict exactly as the backend sent it (design.md PayPulse table)", () => {
    const view = readScoreBreakdown(RESTORE);
    expect(view.score).toBe(68.1);
    expect(formatScore(view.score, view.scoreDecimals)).toBe("68.1");
    expect(view.terms.map((t) => [t.symbol, t.weight, t.raw, t.points])).toEqual([
      ["V", 0.5, 0.681, 34.05],
      ["D", 0.3, 0.3333, 20.001],
      ["R", 0.2, 0.3, 14],
    ]);
    expect(view.debt?.files).toHaveLength(5);
    expect(view.value?.verifiedFeatureIds).toEqual([MOCK_FEATURE_IDS.ledger, MOCK_FEATURE_IDS.invoices, MOCK_FEATURE_IDS.webhooks]);
    expect(view.value?.targets.map((t) => t.feasibility)).toEqual([0.68, 0.4061]);
    expect(readScoreBreakdown(MODERNIZE).score).toBe(58.1);
    expect(readScoreBreakdown(EXTRACT).score).toBe(57.3);
  });

  it("writes the formula and the risk derivation in the same notation as each spec bundle's design.md", () => {
    for (const strategy of MOCK_STRATEGIES) {
      const view = readScoreBreakdown(strategy);
      expect(strategy.spec.design_md).toContain(`**${formatScore(view.score)}** = ${substitutedFormula(view)}`);
      expect(strategy.spec.design_md).toContain(describeRisk(view));
    }
    expect(describeRisk(readScoreBreakdown(RESTORE))).toBe("base 0.25 + MERGE_DISTANCE 5 × 0.01 (cap 0.15) = 0.05");
  });

  it("falls back to the score columns when the breakdown is missing", () => {
    const view = readScoreBreakdown({ ...RESTORE, score_breakdown: {} });
    expect(view.score).toBe(RESTORE.feasibility_score);
    expect(view.terms.map((t) => t.raw)).toEqual([RESTORE.value_score, RESTORE.debt_cost_score, RESTORE.risk_score]);
    expect(view.value).toBeNull();
  });
});

describe("formatDecimal", () => {
  it("drops float noise and trailing zeros", () => {
    expect(formatDecimal(0.39999999999999997)).toBe("0.4");
    expect(formatDecimal(20.001)).toBe("20.001");
    expect(formatDecimal(null)).toBe("—");
  });

  it("stays within half a unit of the last decimal", () => {
    fc.assert(
      fc.property(fc.double({ min: -1e6, max: 1e6, noNaN: true }), (value) => {
        expect(Math.abs(Number(formatDecimal(value)) - value)).toBeLessThanOrEqual(5e-5 + 1e-9);
      }),
    );
  });
});

describe("the deck", () => {
  it("recommends and ranks RESTORE, like approval.recommended_strategy_id", () => {
    expect(recommendedStrategyId(MOCK_STRATEGIES)).toBe(RESTORE.id);
    expect([...highestScoreIds(MOCK_STRATEGIES)]).toEqual([RESTORE.id]);
    expect(recommendedStrategyId(MOCK_STRATEGIES.map((s) => ({ ...s, is_actionable: s.id !== RESTORE.id })))).toBe(MODERNIZE.id);
    expect(recommendedStrategyId(MOCK_STRATEGIES.map((s) => ({ ...s, is_actionable: false })))).toBeNull();
  });

  it("marks the best value of each comparison row", () => {
    const rows = Object.fromEntries(comparisonRows(MOCK_STRATEGIES).map((row) => [row.key, row]));
    expect([...(rows.score?.best ?? [])]).toEqual([RESTORE.id]);
    expect(rows.score?.cells[EXTRACT.id]).toBe("57.3");
    expect([...(rows.debt?.best ?? [])]).toEqual([RESTORE.id]); // lower is better
    expect(new Set(rows.value?.best)).toEqual(new Set([RESTORE.id, MODERNIZE.id]));
    expect(rows.items?.best.size).toBe(0); // item counts are not ranked
  });

  it("reads plan items and deferrals", () => {
    expect(planItems(RESTORE).map((i) => i.id)).toEqual(["restore-01", "restore-02", "restore-03", "restore-04", "restore-05"]);
    expect(planItems(MODERNIZE).filter((i) => i.kind === "UPGRADE_DEPENDENCY").every((i) => i.requiresResolution && i.dependency)).toBe(true);
    expect(deferredItems(RESTORE).map((d) => [d.reason, d.partial])).toEqual([
      ["NEEDS_EXTERNAL_SERVICE", true],
      ["NEEDS_NEW_DEPENDENCY", false],
    ]);
    expect(featureTitles(MOCK_STRATEGIES).get(MOCK_FEATURE_IDS.ledger)).toBe("Double-entry ledger with balanced, atomic postings");
  });

  it("selects the approved strategy first, then the recommended one", () => {
    expect(defaultSelection(buildMockStrategies("AUTOPSY_COMPLETE", null))).toBe(RESTORE.id);
    const approval: MockApprovalRecord = {
      strategyId: EXTRACT.id,
      approvalToken: EXTRACT.approval.approval_token,
      approvedAt: 0,
      approvedBy: "",
      foundryRunId: "x",
      runStartedAt: 0,
    };
    expect(defaultSelection(buildMockStrategies("STRATEGY_APPROVED", approval))).toBe(EXTRACT.id);
  });
});

describe("gateAvailability", () => {
  it("opens only for an unapproved, actionable strategy while the project is AUTOPSY_COMPLETE", () => {
    const data = buildMockStrategies("AUTOPSY_COMPLETE", null);
    expect(gateAvailability(data, RESTORE, "AUTOPSY_COMPLETE")).toEqual({ kind: "open" });
    expect(gateAvailability(data, RESTORE, "SYNTHESIZING_GENOME")).toEqual({ kind: "not_ready", status: "SYNTHESIZING_GENOME" });
    expect(gateAvailability(data, { ...RESTORE, is_actionable: false }, "AUTOPSY_COMPLETE")).toEqual({ kind: "not_actionable" });
    expect(gateAvailability(data, { ...RESTORE, approval: { ...RESTORE.approval, approval_token: "nope" } }, "AUTOPSY_COMPLETE")).toEqual({
      kind: "invalid_token",
    });
  });

  it("locks the other strategies once one is approved", () => {
    const outcome = mockApprove({
      status: "AUTOPSY_COMPLETE",
      approval: null,
      strategyId: RESTORE.id,
      token: RESTORE.approval.approval_token,
      approver: "Dr. F",
      now: 1_000,
      runStartedAt: 0,
      foundryRunId: "run",
    });
    if (!outcome.ok) throw new Error("approval failed");
    const data = applyApproval(buildMockStrategies("AUTOPSY_COMPLETE", null), outcome.body);
    expect(data.approved_strategy_id).toBe(RESTORE.id);
    expect(data.project.status).toBe("STRATEGY_APPROVED");
    const approved = data.strategies.find((s) => s.id === RESTORE.id) as StrategyOut;
    expect(gateAvailability(data, approved, "STRATEGY_APPROVED")).toEqual({ kind: "approved", intact: true });
    expect(gateAvailability(data, EXTRACT, "STRATEGY_APPROVED")).toEqual({ kind: "locked", approvedId: RESTORE.id, approvedType: "RESTORE" });
  });
});

describe("gateReducer", () => {
  const action: fc.Arbitrary<GateAction> = fc.oneof(
    fc.constant<GateAction>({ type: "arm" }),
    fc.constant<GateAction>({ type: "disarm" }),
    fc.constant<GateAction>({ type: "shock" }),
    fc.constant<GateAction>({ type: "reset" }),
    fc.constant<GateAction>({ type: "success", result: {} as never }),
    fc.constant<GateAction>({ type: "failure", failure: describeApprovalFailure(new Error("x")) }),
  );

  it("never approves without charging (arm) and then confirming (shock)", () => {
    fc.assert(
      fc.property(fc.array(action, { maxLength: 30 }), (actions) => {
        let state: GatePhase = GATE_IDLE;
        for (const next of actions) {
          const after = gateReducer(state, next);
          if (after.phase === "shocking" && state.phase !== "shocking") expect(state.phase).toBe("armed");
          if (after.phase === "approved" && state.phase !== "approved") expect(state.phase).toBe("shocking");
          state = after;
        }
      }),
    );
  });

  it("walks charge → clear → shock", () => {
    let state = gateReducer(GATE_IDLE, { type: "shock" });
    expect(state).toEqual(GATE_IDLE); // a stray confirm does nothing
    state = gateReducer(state, { type: "arm" });
    expect(sequenceStep(state, false)).toBe(1);
    state = gateReducer(state, { type: "shock" });
    expect(sequenceStep(state, false)).toBe(2);
    expect(gateReducer(state, { type: "disarm" }).phase).toBe("shocking"); // cannot stand down mid-shock
    expect(sequenceStep(GATE_IDLE, true)).toBe(3);
  });
});

describe("describeApprovalFailure", () => {
  const http = (status: number, body: unknown) => new ApiError("http", "x", { status, body, url: "/approve" });
  const conflict = (code: string) => http(409, { detail: "Refused.", code });

  it("maps every backend answer to a recovery", () => {
    expect(describeApprovalFailure(http(403, { detail: "Header.", code: "APPROVER_REQUIRED" })).recovery).toBe("approver");
    expect(describeApprovalFailure(http(404, { detail: "Not found." })).recovery).toBe("refresh");
    expect(describeApprovalFailure(conflict("STRATEGY_STALE")).recovery).toBe("refresh");
    expect(describeApprovalFailure(conflict("PROJECT_NOT_READY")).recovery).toBe("refresh");
    expect(describeApprovalFailure(conflict("ANOTHER_STRATEGY_APPROVED")).recovery).toBe("locked");
    expect(describeApprovalFailure(conflict("STRATEGY_NOT_ACTIONABLE")).recovery).toBe("none");
    expect(describeApprovalFailure(conflict("APPROVED_CONTENT_CHANGED")).recovery).toBe("none");
    expect(describeApprovalFailure(http(422, { detail: [{ msg: "bad token" }] })).message).toBe("bad token");
    expect(describeApprovalFailure(http(500, "boom")).recovery).toBe("retry");
    expect(describeApprovalFailure(new ApiError("offline", "down", { url: "/approve" })).recovery).toBe("refresh");
    expect(describeApprovalFailure(conflict("STRATEGY_STALE")).code).toBe("STRATEGY_STALE");
  });
});

describe("loading states", () => {
  const notReady = (over: Partial<StrategiesNotReadyOut>): StrategiesNotReadyOut => ({
    detail: "",
    code: "STRATEGIES_UNAVAILABLE",
    project_id: "p",
    status: "AUTOPSY_COMPLETE",
    status_detail: "",
    telemetry_ws_path: "",
    ...over,
  });

  it("treats the gap before the rows exist as pending, and a failed generation as unavailable", () => {
    expect(classifyNotReady(notReady({ code: "STRATEGIES_PENDING", status: "INGESTING" }), [])).toBe("pending");
    expect(classifyNotReady(notReady({}), [])).toBe("pending");
    expect(classifyNotReady(notReady({ status_detail: "Strategy generation failed." }), [])).toBe("unavailable");
    expect(classifyNotReady(notReady({ status: "FAILED" }), [])).toBe("unavailable");
    const error = { ...mockFrames(0)[0]!, event_type: "ERROR" as const, payload: { stage: "REVIVAL" } };
    expect(classifyNotReady(notReady({}), [error])).toBe("unavailable");
  });

  it("lists the STRATEGY_GENERATED frames of the recorded run", () => {
    const generated = generatedFromFrames(mockFrames(0));
    expect(generated.map((g) => [g.strategyType, g.strategyId, g.score])).toEqual(
      MOCK_STRATEGIES.map((s) => [s.strategy_type, s.id, s.feasibility_score]),
    );
    expect(generatedFromFrames(mockFrames(0, MOCK_GENOME_COMPLETE_AT))).toEqual([]);
  });
});
