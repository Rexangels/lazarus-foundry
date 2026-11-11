import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { pytestComparison, readContinuityMemory } from "./heartbeat";
import {
  gutterDigits,
  headSlice,
  heartbeatPhase,
  isNearBottom,
  memoryFileName,
  shouldPollRebirth,
  tailSlice,
  totalsSentence,
  vitalTrace,
} from "./heartbeatView";
import { MOCK_REBIRTH } from "./mock/rebirth";

describe("tailSlice / headSlice", () => {
  it("keeps the newest lines and counts the rest", () => {
    expect(tailSlice([1, 2, 3, 4, 5], 2)).toEqual({ visible: [4, 5], hidden: 3 });
    expect(headSlice([1, 2, 3, 4, 5], 2)).toEqual({ visible: [1, 2], hidden: 3 });
    expect(tailSlice([1, 2], 5)).toEqual({ visible: [1, 2], hidden: 0 });
    expect(tailSlice([1, 2], 0)).toEqual({ visible: [], hidden: 2 });
  });

  it("never renders more than the cap and loses nothing from the count", () => {
    fc.assert(
      fc.property(fc.array(fc.integer(), { maxLength: 300 }), fc.integer({ min: 0, max: 400 }), (items, max) => {
        const tail = tailSlice(items, max);
        expect(tail.visible.length).toBeLessThanOrEqual(max);
        expect(tail.visible.length + tail.hidden).toBe(items.length);
        expect(tail.visible).toEqual(items.slice(items.length - tail.visible.length));
        const head = headSlice(items, max);
        expect(head.visible).toEqual(items.slice(0, head.visible.length));
        expect(head.visible.length + head.hidden).toBe(items.length);
      }),
    );
  });
});

describe("isNearBottom", () => {
  it("follows only near the bottom", () => {
    expect(isNearBottom({ scrollTop: 600, clientHeight: 400, scrollHeight: 1000 })).toBe(true);
    expect(isNearBottom({ scrollTop: 580, clientHeight: 400, scrollHeight: 1000 })).toBe(true);
    expect(isNearBottom({ scrollTop: 300, clientHeight: 400, scrollHeight: 1000 })).toBe(false);
  });
});

describe("shouldPollRebirth", () => {
  it("polls while the Foundry runs, never for an unavailable artifact", () => {
    expect(shouldPollRebirth("REBIRTH_PENDING", "FOUNDRY_RUNNING")).toBe(true);
    expect(shouldPollRebirth("REBIRTH_PENDING", "STRATEGY_APPROVED")).toBe(true);
    expect(shouldPollRebirth("REBIRTH_PENDING", "REBORN")).toBe(true);
    expect(shouldPollRebirth("REBIRTH_PENDING", "AUTOPSY_COMPLETE")).toBe(false);
    expect(shouldPollRebirth("REBIRTH_UNAVAILABLE", "FOUNDRY_RUNNING")).toBe(false);
    expect(shouldPollRebirth("REBIRTH_UNAVAILABLE", "FAILED")).toBe(false);
  });
});

describe("heartbeatPhase", () => {
  it("maps the rebirth read, status and pipeline outcome to a screen", () => {
    expect(heartbeatPhase({ rebirth: "ready", status: "REBORN", outcome: "reborn" })).toBe("reborn");
    expect(heartbeatPhase({ rebirth: "loading", status: null, outcome: "idle" })).toBe("loading");
    expect(heartbeatPhase({ rebirth: "pending", status: "AUTOPSY_COMPLETE", outcome: "idle" })).toBe("not-approved");
    expect(heartbeatPhase({ rebirth: "pending", status: null, outcome: "idle" })).toBe("loading");
    expect(heartbeatPhase({ rebirth: "pending", status: "STRATEGY_APPROVED", outcome: "idle" })).toBe("live");
    expect(heartbeatPhase({ rebirth: "pending", status: "FOUNDRY_RUNNING", outcome: "running" })).toBe("live");
    // The status can say REBORN a moment before the artifact is readable.
    expect(heartbeatPhase({ rebirth: "pending", status: "REBORN", outcome: "reborn" })).toBe("live");
    expect(heartbeatPhase({ rebirth: "unavailable", status: "FAILED", outcome: "failed" })).toBe("failed");
    expect(heartbeatPhase({ rebirth: "error", status: "FAILED", outcome: "failed" })).toBe("failed");
    expect(heartbeatPhase({ rebirth: "unavailable", status: "FAILED", outcome: "idle" })).toBe("unavailable");
    expect(heartbeatPhase({ rebirth: "error", status: "FOUNDRY_RUNNING", outcome: "running" })).toBe("error");
  });
});

describe("vitalTrace", () => {
  it("is erratic for the recorded baseline and a steady emerald pulse after", () => {
    const { before, after } = pytestComparison(MOCK_REBIRTH);
    expect(vitalTrace(before)).toEqual({ state: "erratic", token: "ghost", label: "Arrhythmia: 15 of 16 passed, 1 failed" });
    expect(vitalTrace(after)).toEqual({ state: "pulse", token: "alive", label: "Steady rhythm: 39 of 39 passed" });
  });

  it("is flat without tests or with nothing passing", () => {
    expect(vitalTrace({ totals: null, allPassing: null }).state).toBe("flat");
    expect(vitalTrace({ totals: { tests: 3, passed: 0, failed: 2, errors: 1, skipped: 0 }, allPassing: false })).toEqual({
      state: "flat",
      token: "ghost",
      label: "Flatline: 0 of 3 passed, 2 failed, 1 error",
    });
    expect(totalsSentence({ tests: 0, passed: 0, failed: 0, errors: 0, skipped: 0 })).toBe("No tests collected");
  });
});

describe("memoryFileName", () => {
  it("names the recorded memory after the project and the rebirth commit", () => {
    expect(memoryFileName(readContinuityMemory(MOCK_REBIRTH.continuity_memory))).toBe(
      "lazarus-continuity-paypulse-patient-zero-0220a8b424d1.json",
    );
  });

  it("only lets [a-z0-9-] into the name", () => {
    fc.assert(
      fc.property(fc.string(), fc.string(), (name, sha) => {
        const file = memoryFileName({
          project: { id: "", name, repoUrl: "", defaultBranch: "" },
          revival: { rebirthCommitSha: sha } as never,
        });
        expect(file).toMatch(/^lazarus-continuity-[a-z0-9-]+\.json$/);
      }),
    );
  });
});

describe("gutterDigits", () => {
  it("is at least 3 wide", () => {
    expect(gutterDigits(7)).toBe(3);
    expect(gutterDigits(1234)).toBe(4);
  });
});
