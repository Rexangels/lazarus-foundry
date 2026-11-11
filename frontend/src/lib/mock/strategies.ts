/**
 * The recorded PayPulse revival strategies for the offline demo, and the mock approval gate.
 *
 * paypulse-strategies.json is real backend output: the no-database chain of
 * backend/tests/test_rebirth_paypulse.py (ingest the fixture → deterministic genome →
 * plan_strategies → score_plan → render_spec_bundle → validate_spec_bundle → as_storage), with the
 * feature ids mapped onto the mock genome's ids and each `approval_token` computed by
 * `apps.revival.approval.approval_token` over that content. Scores match design.md's PayPulse table
 * (RESTORE 68.1, MODERNIZE 58.1, EXTRACT 57.3).
 *
 * `mockApprove` applies the backend's checks in the backend's order (services.approve_strategy), so
 * the offline demo answers the same 200/404/409 bodies. This module is loaded on demand by the mock
 * client, so the JSON stays out of the other views' bundles.
 */
import { recommendedStrategyId } from "../defibrillator";
import type {
  ApprovalErrorCode,
  ApprovalErrorOut,
  ApprovalOut,
  ErrorOut,
  ProjectStatus,
  StrategiesOut,
  StrategyOut,
} from "../types";
import { telemetryWsPath } from "../config";
import { MOCK_DEFAULT_BRANCH, MOCK_PROJECT_ID, MOCK_PROJECT_NAME } from "./paypulse";
import RECORDED from "./paypulse-strategies.json";
import { MOCK_STRATEGY_SYNTHESIS_ID, type MockApprovalRecord } from "./telemetry";

export const MOCK_STRATEGIES: readonly StrategyOut[] = RECORDED as unknown as StrategyOut[];

const SYNTHESIS_AT = "2026-01-01T00:00:12+00:00";

function withApproval(strategy: StrategyOut, approval: MockApprovalRecord | null, recommended: string | null): StrategyOut {
  const approved = approval?.strategyId === strategy.id;
  return {
    ...strategy,
    is_recommended: strategy.id === recommended,
    approval: {
      ...strategy.approval,
      approved_by_human: approved,
      approved_at: approved && approval ? new Date(approval.approvedAt).toISOString() : null,
      approved_by: approved && approval ? approval.approvedBy : "",
      approval_intact: approved && approval ? approval.approvalToken === strategy.approval.approval_token : null,
    },
  };
}

/** `GET /api/projects/{id}/strategies` (200) for the recorded run in `status`. */
export function buildMockStrategies(status: ProjectStatus, approval: MockApprovalRecord | null): StrategiesOut {
  const recommended = recommendedStrategyId(MOCK_STRATEGIES);
  const strategies = MOCK_STRATEGIES.map((s) => withApproval(s, approval, recommended));
  return {
    project: { id: MOCK_PROJECT_ID, name: MOCK_PROJECT_NAME, status, status_detail: "", default_branch: MOCK_DEFAULT_BRANCH },
    synthesis: {
      id: MOCK_STRATEGY_SYNTHESIS_ID,
      mode: "DETERMINISTIC",
      model_id: "",
      started_at: SYNTHESIS_AT,
      finished_at: SYNTHESIS_AT,
      preserved_strategy_types: [],
      validation: { fallback_reason: null, refined_bundle_count: 0, applied_prose_count: 0, dropped_prose_count: 0 },
    },
    recommended_strategy_id: recommended,
    approved_strategy_id: strategies.find((s) => s.approval.approved_by_human)?.id ?? null,
    telemetry_ws_path: telemetryWsPath(MOCK_PROJECT_ID),
    strategies,
  };
}

export type MockApproveOutcome =
  | { ok: true; body: ApprovalOut; approval: MockApprovalRecord }
  | { ok: false; status: 404; body: ErrorOut }
  | { ok: false; status: 409; body: ApprovalErrorOut };

export interface MockApproveInput {
  status: ProjectStatus;
  approval: MockApprovalRecord | null;
  strategyId: string;
  token: string;
  approver: string;
  now: number;
  runStartedAt: number;
  foundryRunId: string;
}

/** `POST .../strategies/{id}/approve` against the recorded strategies (services.approve_strategy's order). */
export function mockApprove(input: MockApproveInput): MockApproveOutcome {
  const { status, approval, strategyId, token } = input;
  const strategy = MOCK_STRATEGIES.find((s) => s.id === strategyId);
  if (!strategy) return { ok: false, status: 404, body: { detail: "Strategy not found." } };
  const refuse = (code: ApprovalErrorCode, detail: string, extra: Partial<ApprovalErrorOut> = {}): MockApproveOutcome => ({
    ok: false,
    status: 409,
    body: {
      detail,
      code,
      project_id: MOCK_PROJECT_ID,
      strategy_id: strategyId,
      current_approval_token: null,
      approved_strategy_id: null,
      ...extra,
    },
  });
  const current = strategy.approval.approval_token;
  const respond = (record: MockApprovalRecord, alreadyApproved: boolean): MockApproveOutcome => {
    const data = buildMockStrategies("STRATEGY_APPROVED", record);
    return {
      ok: true,
      approval: record,
      body: {
        strategy: data.strategies.find((s) => s.id === strategyId) as StrategyOut,
        foundry_run_id: record.foundryRunId,
        project_status: "STRATEGY_APPROVED",
        already_approved: alreadyApproved,
        telemetry_ws_path: telemetryWsPath(MOCK_PROJECT_ID),
      },
    };
  };

  if (approval?.strategyId === strategyId) {
    if (token === approval.approvalToken) return respond(approval, true);
    return refuse("STRATEGY_STALE", "This strategy was already approved with different content.", {
      current_approval_token: approval.approvalToken,
    });
  }
  if (approval) {
    return refuse("ANOTHER_STRATEGY_APPROVED", "Another strategy of this project is already approved.", {
      approved_strategy_id: approval.strategyId,
    });
  }
  if (status !== "AUTOPSY_COMPLETE") {
    return refuse("PROJECT_NOT_READY", `The project is ${status}; strategies can only be approved after AUTOPSY_COMPLETE.`);
  }
  if (token !== current) {
    return refuse("STRATEGY_STALE", "The strategy changed since it was reviewed; reload it and review it again.", {
      current_approval_token: current,
    });
  }
  if (!strategy.is_actionable) return refuse("STRATEGY_NOT_ACTIONABLE", "This strategy has no plan items to execute.");
  return respond(
    {
      strategyId,
      approvalToken: token,
      approvedAt: input.now,
      approvedBy: input.approver,
      foundryRunId: input.foundryRunId,
      runStartedAt: input.runStartedAt,
    },
    false,
  );
}
