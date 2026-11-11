import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  classifyPytestLine,
  filterTerminal,
  healingLabel,
  parsePytestTotals,
  parseUnifiedDiff,
  pipelineState,
  pytestComparison,
  readContinuityMemory,
  terminalLines,
} from "./heartbeat";
import { MOCK_PROJECT_ID } from "./mock/paypulse";
import { MOCK_REBIRTH } from "./mock/rebirth";
import { MOCK_FOUNDRY_RUN_ID, MOCK_RECORDED_STRATEGY_ID, mockFoundryFrames, type MockApprovalRecord } from "./mock/telemetry";
import type { JsonValue, TelemetryEventType, TelemetryFrame } from "./types";

const APPROVAL: MockApprovalRecord = {
  strategyId: MOCK_RECORDED_STRATEGY_ID,
  approvalToken: "token",
  approvedAt: Date.parse("2026-01-01T00:01:00Z"),
  approvedBy: "demo",
  foundryRunId: MOCK_FOUNDRY_RUN_ID,
  runStartedAt: 0,
};
const RECORDED = mockFoundryFrames(APPROVAL);

let seq = 0;
function frame(event_type: TelemetryEventType, payload: Record<string, JsonValue>): TelemetryFrame {
  seq += 1;
  return {
    type: "telemetry",
    event_id: `e-${seq}`,
    project_id: MOCK_PROJECT_ID,
    event_type,
    payload: { foundry_run_id: MOCK_FOUNDRY_RUN_ID, ...payload },
    timestamp: new Date(Date.UTC(2026, 0, 1, 0, 0, seq)).toISOString(),
  };
}

describe("pipelineState", () => {
  it("is idle before any approval or frame", () => {
    const state = pipelineState({ frames: [], status: "AUTOPSY_COMPLETE" });
    expect(state.outcome).toBe("idle");
    expect(state.progress).toBe(0);
    expect(state.stages.map((s) => s.status)).toEqual(["pending", "pending", "pending", "pending"]);
  });

  it("follows the recorded run to reborn with the baseline and verification totals", () => {
    const state = pipelineState({ frames: RECORDED, status: "REBORN" });
    expect(state.outcome).toBe("reborn");
    expect(state.fromStatusFallback).toBe(false);
    expect(state.foundryRunId).toBe(MOCK_FOUNDRY_RUN_ID);
    expect(state.stages.every((s) => s.status === "done")).toBe(true);
    expect(state.progress).toBe(1);
    expect(state.baseline?.totals).toEqual({ tests: 16, passed: 15, failed: 1, errors: 0, skipped: 0 });
    expect(state.verification?.totals?.passed).toBe(39);
    expect(state.rebirth?.revivalBranch).toBe("lazarus/restore-7e1d2c3b");
  });

  it("marks earlier stages done when a later stage starts, even if their frames were missed", () => {
    const state = pipelineState({ frames: [frame("AGENT_STAGE_CHANGED", { stage: "ENGINEERING", status: "STARTED" })], status: "FOUNDRY_RUNNING" });
    expect(state.outcome).toBe("running");
    expect(state.current).toBe("ENGINEERING");
    expect(state.stages.map((s) => s.status)).toEqual(["done", "done", "running", "pending"]);
    expect(state.progress).toBeCloseTo(2.5 / 4);
  });

  it("reports self-healing n of max", () => {
    const state = pipelineState({
      frames: [frame("AGENT_STAGE_CHANGED", { stage: "VERIFICATION", status: "SELF_HEALING", iteration: 2, max_iterations: 3, files: ["a.py"] })],
      status: "FOUNDRY_RUNNING",
    });
    expect(state.stages[3]?.status).toBe("healing");
    expect(healingLabel(state)).toBe("Self-healing iteration 2 of 3");
    expect(healingLabel(pipelineState({ frames: [], status: "FOUNDRY_RUNNING" }))).toBeNull();
  });

  it("takes the failure from an ERROR frame, and from the project status when frames were missed", () => {
    const fromFrame = pipelineState({
      frames: [frame("ERROR", { stage: "FOUNDRY", agent_stage: "ENVIRONMENT", code: "INSTALL_FAILED", detail: "pip failed" })],
      status: "FAILED",
    });
    expect(fromFrame.outcome).toBe("failed");
    expect(fromFrame.failure).toMatchObject({ scope: "FOUNDRY", stage: "ENVIRONMENT", code: "INSTALL_FAILED", detail: "pip failed" });

    const fromStatus = pipelineState({
      frames: [frame("AGENT_STAGE_CHANGED", { stage: "ARCHITECTURE", status: "STARTED" })],
      status: "FAILED",
      statusDetail: "worker lost",
    });
    expect(fromStatus.outcome).toBe("failed");
    expect(fromStatus.fromStatusFallback).toBe(true);
    expect(fromStatus.failure).toMatchObject({ scope: "STATUS", stage: "ARCHITECTURE", detail: "worker lost" });
    expect(fromStatus.stages[1]?.status).toBe("failed");
  });

  it("ignores frames of another run", () => {
    const other = { ...frame("AGENT_STAGE_CHANGED", { stage: "VERIFICATION", status: "STARTED" }) };
    other.payload = { ...other.payload, foundry_run_id: "another-run" };
    const state = pipelineState({ frames: [other], status: "STRATEGY_APPROVED", foundryRunId: MOCK_FOUNDRY_RUN_ID });
    expect(state.outcome).toBe("queued");
    expect(state.stages[3]?.status).toBe("pending");
  });
});

describe("terminal", () => {
  it("renders the recorded run with commands, stdout/stderr and stages", () => {
    const { lines, dropped } = terminalLines(RECORDED);
    expect(dropped).toBe(0);
    expect(lines.some((l) => l.kind === "command" && l.text.startsWith("$ "))).toBe(true);
    expect(lines.some((l) => l.kind === "test-fail" && l.text.includes("baseline"))).toBe(true);
    expect(lines.at(-1)?.kind).toBe("success");
    expect(new Set(lines.map((l) => l.id)).size).toBe(lines.length);
  });

  it("keeps only the newest `limit` lines and counts the dropped ones", () => {
    fc.assert(
      fc.property(fc.array(fc.integer({ min: 0, max: 40 }), { maxLength: 12 }), fc.integer({ min: 1, max: 60 }), (sizes, limit) => {
        const frames = sizes.map((n) =>
          frame("SANDBOX_LOG", { stage: "ENVIRONMENT", command: "", lines: Array.from({ length: n }, (_, i) => ({ stream: "stdout", line: `l${i}` })) }),
        );
        const total = sizes.reduce((a, b) => a + b, 0);
        const all = terminalLines(frames, Number.MAX_SAFE_INTEGER).lines;
        const capped = terminalLines(frames, limit);
        expect(capped.lines.length).toBe(Math.min(total, limit));
        expect(capped.dropped).toBe(Math.max(0, total - limit));
        expect(capped.lines).toEqual(all.slice(all.length - capped.lines.length));
      }),
    );
  });

  it("filters by stage and stream, keeping lines without a stage", () => {
    const { lines } = terminalLines([
      frame("SANDBOX_LOG", { stage: "ENVIRONMENT", command: "pip install", lines: [{ stream: "stdout", line: "ok" }, { stream: "stderr", line: "warn" }] }),
      frame("SANDBOX_LOG", { stage: "VERIFICATION", command: "pytest", lines: [{ stream: "stdout", line: "39 passed" }] }),
      frame("TEST_PASSED", { phase: "VERIFICATION", iteration: 1, totals: { tests: 1, passed: 1, failed: 0, errors: 0, skipped: 0 } }),
    ]);
    const verification = filterTerminal(lines, { stage: "VERIFICATION" });
    expect(verification.map((l) => l.text)).toEqual(["$ pytest", "39 passed", "TEST_PASSED verification 1: 1 passed of 1"]);
    const noStderr = filterTerminal(lines, { stderr: false });
    expect(noStderr.some((l) => l.kind === "stderr")).toBe(false);
    expect(filterTerminal(lines, { stdout: false }).filter((l) => l.kind === "stdout")).toEqual([]);
  });
});

describe("pytest summaries", () => {
  it("reads bare -q and bordered summary lines", () => {
    expect(parsePytestTotals(MOCK_REBIRTH.before_test_output)).toEqual({ tests: 16, passed: 15, failed: 1, errors: 0, skipped: 0 });
    expect(parsePytestTotals("===== 2 passed, 1 skipped in 0.10s =====")).toEqual({ tests: 3, passed: 2, failed: 0, errors: 0, skipped: 1 });
    expect(parsePytestTotals("no summary here")).toBeNull();
    expect(classifyPytestLine("1 failed, 15 passed in 0.88s")).toBe("summary-fail");
    expect(classifyPytestLine("39 passed in 0.65s")).toBe("summary-pass");
  });

  it("compares the recorded before/after runs", () => {
    const cmp = pytestComparison(MOCK_REBIRTH);
    expect(cmp.before.totals).toMatchObject({ tests: 16, passed: 15, failed: 1 });
    expect(cmp.before.allPassing).toBe(false);
    expect(cmp.before.branch).toBe("main");
    expect(cmp.after.totals).toMatchObject({ tests: 39, passed: 39, failed: 0 });
    expect(cmp.after.allPassing).toBe(true);
    expect(cmp.after.branch).toBe("lazarus/restore-7e1d2c3b");
    expect(cmp.fixed).toEqual(["tests/test_webhooks.py::test_dispatch_retries_with_exponential_backoff"]);
  });
});

describe("parseUnifiedDiff", () => {
  const parsed = parseUnifiedDiff(MOCK_REBIRTH.unified_diff_patch);

  it("agrees with the backend's per-file counts for the recorded patch", () => {
    expect(parsed.truncated).toBe(false);
    expect(parsed.files.map((f) => f.path)).toEqual(MOCK_REBIRTH.files.map((f) => f.path));
    for (const file of parsed.files) {
      const stat = MOCK_REBIRTH.files.find((f) => f.path === file.path);
      expect([file.status, file.additions, file.deletions]).toEqual([stat?.status, stat?.additions, stat?.deletions]);
    }
    expect(parsed.files.filter((f) => f.isSpec).length).toBe(3);
    expect(parsed.additions).toBe(657);
    expect(parsed.deletions).toBe(14);
  });

  it("numbers old and new lines consecutively inside every hunk", () => {
    for (const hunk of parsed.files.flatMap((f) => f.hunks)) {
      const body = hunk.lines.filter((l) => l.kind === "add" || l.kind === "del" || l.kind === "context");
      const olds = body.filter((l) => l.oldLine !== null).map((l) => l.oldLine);
      const news = body.filter((l) => l.newLine !== null).map((l) => l.newLine);
      expect(olds).toEqual(Array.from({ length: hunk.oldLines }, (_, i) => hunk.oldStart + i));
      expect(news).toEqual(Array.from({ length: hunk.newLines }, (_, i) => hunk.newStart + i));
    }
  });

  it("reads the truncation note, a deletion and a quoted path, and never throws", () => {
    const patch = [
      'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"',
      "deleted file mode 100644",
      '--- "a/caf\\303\\251.py"',
      "+++ /dev/null",
      "@@ -1,2 +0,0 @@",
      "-x = 1",
      "-y = 2",
      "[lazarus] diff truncated at 200000 characters; run git diff base..head for the rest",
    ].join("\n");
    const result = parseUnifiedDiff(patch);
    expect(result.files).toHaveLength(1);
    expect(result.files[0]).toMatchObject({ path: "café.py", status: "D", deletions: 2, newPath: null });
    expect(result.truncated).toBe(true);
    expect(result.truncationNote).toMatch(/^\[lazarus\] diff truncated/);
    fc.assert(
      fc.property(fc.string({ unit: fc.constantFrom(..."diff --git a/x b/x\n@@ -1,2 +1 @@\n+- \\\"".split("")) }), (text) => {
        expect(() => parseUnifiedDiff(text)).not.toThrow();
      }),
    );
  });
});

describe("Continuity Memory readers", () => {
  it("read the recorded memory", () => {
    const memory = readContinuityMemory(MOCK_REBIRTH.continuity_memory);
    expect(memory.version).toBe("lazarus-continuity-v1");
    expect(memory.project?.defaultBranch).toBe("main");
    expect(memory.revival).toMatchObject({ strategyType: "RESTORE", revivalBranch: "lazarus/restore-7e1d2c3b", feasibilityScore: 68.1 });
    expect(memory.verification).toMatchObject({ verificationAttempts: 1, iterationsUsed: 0, maxHealingIterations: 3 });
    expect(memory.changedFiles).toHaveLength(8);
  });

  it("tolerate a missing or mistyped memory", () => {
    const empty = readContinuityMemory(null);
    expect(empty).toMatchObject({ version: "", project: null, revival: null, verification: null, completed: [], roadmap: [], openMarkers: [] });
    const odd = readContinuityMemory({ verification: "x", roadmap: [1, { title: 2, step: "3" }], open_markers: ["TODO", 4], changed_files: [{ status: "M" }] });
    expect(odd.verification).toBeNull();
    expect(odd.roadmap).toEqual([
      { step: 1, featureId: null, title: "", vitalStatus: "", reason: "", detail: "", wouldTouch: [], externalRequirements: [], partial: false, status: "DEFERRED" },
    ]);
    expect(odd.openMarkers).toEqual(["TODO"]);
    expect(odd.changedFiles).toEqual([]);
  });
});
