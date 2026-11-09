import { describe, expect, it } from "vitest";

import { MOCK_GENOME_COMPLETE_AT, MOCK_INGESTION_COMPLETE_AT, mockFrames, mockStatusAt } from "./mock/telemetry";
import { derivePipelineProgress, describeFrame } from "./progress";
import type { TelemetryFrame } from "./types";

const START = Date.parse("2026-01-01T00:00:00Z");

describe("derivePipelineProgress", () => {
  it("starts with Ingest active for a dormant project", () => {
    const progress = derivePipelineProgress("DORMANT", []);
    expect(progress.states).toEqual({
      ingest: "active",
      genome: "pending",
      strategy: "pending",
      foundry: "pending",
      rebirth: "pending",
    });
  });

  it("follows the recorded PayPulse run through ingestion", () => {
    const at = MOCK_INGESTION_COMPLETE_AT + 1;
    const progress = derivePipelineProgress(mockStatusAt(at), mockFrames(START, at));
    expect(progress.states.ingest).toBe("done");
    expect(progress.states.genome).toBe("active");
    expect(progress.counters).toMatchObject({ branches: 2, abandonedBranches: 1, filesParsed: 16 });
  });

  it("counts the six genome features and three conflicts, then waits on the human at Strategy", () => {
    const progress = derivePipelineProgress("AUTOPSY_COMPLETE", mockFrames(START));
    expect(progress.states).toMatchObject({ ingest: "done", genome: "done", strategy: "active", foundry: "pending" });
    expect(progress.counters).toMatchObject({ features: 6, conflicts: 3, strategies: 3 });
    expect(progress.failed).toBe(false);
  });

  it("uses the polled status when the early frames were missed", () => {
    const progress = derivePipelineProgress("AUTOPSY_COMPLETE", []);
    expect(progress.states.genome).toBe("done");
    expect(progress.states.strategy).toBe("active");
  });

  it("marks the failing step on an ERROR frame and a FAILED status", () => {
    const error: TelemetryFrame = {
      type: "telemetry",
      event_id: "00000000-0000-4000-8000-0000000000ff",
      project_id: "0b5e7d1a-6c2f-4e8a-9f3d-2a1c0de0a001",
      event_type: "ERROR",
      payload: { stage: "GENOME", detail: "Saving the genome failed because of a database error." },
      timestamp: new Date(START + MOCK_GENOME_COMPLETE_AT - 1).toISOString(),
    };
    const frames = [...mockFrames(START, MOCK_INGESTION_COMPLETE_AT), error];
    const progress = derivePipelineProgress("FAILED", frames);
    expect(progress.failed).toBe(true);
    expect(progress.states).toMatchObject({ ingest: "done", genome: "failed", strategy: "pending" });
    expect(progress.failure?.detail).toMatch(/database error/);
  });

  it("shows a REVIVAL error on Strategy without failing the run", () => {
    const frames = mockFrames(START, MOCK_GENOME_COMPLETE_AT);
    const error: TelemetryFrame = {
      ...frames[0]!,
      event_id: "00000000-0000-4000-8000-0000000000fe",
      event_type: "ERROR",
      payload: { stage: "REVIVAL", detail: "Strategy generation failed." },
      timestamp: new Date(START + MOCK_GENOME_COMPLETE_AT + 1).toISOString(),
    };
    const progress = derivePipelineProgress("AUTOPSY_COMPLETE", [...frames, error]);
    expect(progress.failed).toBe(false);
    expect(progress.states).toMatchObject({ genome: "done", strategy: "failed" });
  });
});

describe("describeFrame", () => {
  it("summarises genome nodes with their exact C(f)", () => {
    const node = mockFrames(START).find((f) => f.event_type === "GENOME_NODE_CREATED" && f.payload.title === "PDF Invoice Export");
    expect(node && describeFrame(node)).toBe("Genome node: PDF Invoice Export  GHOST_FEATURE/CONFLICTED  C(f)=0.4182");
  });
});
