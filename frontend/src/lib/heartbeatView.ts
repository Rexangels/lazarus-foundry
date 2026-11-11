/**
 * Pure helpers for the Heartbeat's components (Task 8.4, UI): which screen to show, when to poll
 * the rebirth read, DOM caps for the terminal and the diff, the ECG trace of a pytest run and the
 * Continuity Memory download name. The data itself comes from lib/heartbeat.ts.
 */
import type { ContinuityMemory, DiffFile, PipelineOutcome, PytestSummary, TestTotals } from "./heartbeat";
import type { ColorToken, ProjectStatus, RebirthNotReadyCode } from "./types";

/** Terminal rows kept in the DOM; older lines stay in the buffer and are counted, not rendered. */
export const MAX_TERMINAL_DOM_ROWS = 2000;
/** Diff lines rendered per file before a "show all" button. */
export const MAX_DIFF_ROWS_PER_FILE = 1500;
/** `GET .../rebirth` poll interval while REBIRTH_PENDING and the Foundry is running. */
export const REBIRTH_POLL_MS = 2_500;
/** At most one terminal DOM commit (and so one screen reader announcement batch) per interval. */
export const TERMINAL_THROTTLE_MS = 750;

/** The last `max` items, and how many earlier ones were left out. */
export function tailSlice<T>(items: readonly T[], max: number): { visible: T[]; hidden: number } {
  const limit = Math.max(0, Math.floor(max));
  const hidden = Math.max(0, items.length - limit);
  return { visible: hidden ? items.slice(hidden) : [...items], hidden };
}

/** The first `max` items, and how many later ones were left out. */
export function headSlice<T>(items: readonly T[], max: number): { visible: T[]; hidden: number } {
  const limit = Math.max(0, Math.floor(max));
  const hidden = Math.max(0, items.length - limit);
  return { visible: hidden ? items.slice(0, limit) : [...items], hidden };
}

/** Whether a scroll container is (close to) scrolled to the bottom: the terminal follows new lines only then. */
export function isNearBottom(el: { scrollTop: number; clientHeight: number; scrollHeight: number }, threshold = 32): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

/** The Foundry has been approved and has not finished yet. */
export function foundryActive(status: ProjectStatus | null | undefined): boolean {
  return status === "STRATEGY_APPROVED" || status === "FOUNDRY_RUNNING";
}

/**
 * Poll `GET .../rebirth` again: the artifact is pending while the Foundry runs, and briefly after
 * the project turned REBORN (the status can be read before the artifact row is visible).
 */
export function shouldPollRebirth(code: RebirthNotReadyCode, status: ProjectStatus | null | undefined): boolean {
  return code === "REBIRTH_PENDING" && (foundryActive(status) || status === "REBORN");
}

export type RebirthPhase = "loading" | "pending" | "unavailable" | "error" | "ready";

export type HeartbeatPhase =
  /** Nothing known yet. */
  | "loading"
  /** No strategy approved: back to the Defibrillator. */
  | "not-approved"
  /** Approved, queued or running; the rebirth is pending. */
  | "live"
  /** The rebirth artifact is on screen. */
  | "reborn"
  /** The Foundry or the rebirth failed. */
  | "failed"
  /** REBIRTH_UNAVAILABLE without a Foundry failure to show (e.g. the project failed before the Foundry). */
  | "unavailable"
  | "error";

/** Which Heartbeat screen to show. A stored artifact always wins; a Foundry failure beats a read error. */
export function heartbeatPhase(input: { rebirth: RebirthPhase; status: ProjectStatus | null; outcome: PipelineOutcome }): HeartbeatPhase {
  const { rebirth, status, outcome } = input;
  if (rebirth === "ready") return "reborn";
  if (rebirth === "loading") return "loading";
  if (outcome === "failed") return "failed";
  if (rebirth === "error") return "error";
  if (rebirth === "unavailable") return "unavailable";
  // REBIRTH_PENDING
  if (outcome === "idle" && status !== "STRATEGY_APPROVED" && status !== "FOUNDRY_RUNNING" && status !== "REBORN") {
    return status === null ? "loading" : "not-approved";
  }
  return "live";
}

export interface VitalTrace {
  /** HeartbeatLine state: flat (nothing passes or nothing ran), erratic (some fail), pulse (all pass). */
  state: "flat" | "erratic" | "pulse";
  token: ColorToken;
  label: string;
}

/** "15 of 16 passed, 1 failed" (errors and skips only when present). */
export function totalsSentence(totals: TestTotals | null): string {
  if (!totals) return "No totals recorded";
  if (totals.tests === 0) return "No tests collected";
  const parts = [`${totals.passed} of ${totals.tests} passed`];
  if (totals.failed) parts.push(`${totals.failed} failed`);
  if (totals.errors) parts.push(`${totals.errors} ${totals.errors === 1 ? "error" : "errors"}`);
  if (totals.skipped) parts.push(`${totals.skipped} skipped`);
  return parts.join(", ");
}

/** The ECG of one pytest run: steady emerald only when every collected test passed. */
export function vitalTrace(summary: Pick<PytestSummary, "totals" | "allPassing">): VitalTrace {
  const t = summary.totals;
  if (!t || t.tests === 0) return { state: "flat", token: "neutral", label: `Flatline: ${totalsSentence(t).toLowerCase()}` };
  if (summary.allPassing) return { state: "pulse", token: "alive", label: `Steady rhythm: ${totalsSentence(t)}` };
  if (t.passed === 0) return { state: "flat", token: "ghost", label: `Flatline: ${totalsSentence(t)}` };
  return { state: "erratic", token: "ghost", label: `Arrhythmia: ${totalsSentence(t)}` };
}

const DIFF_STATUS: Record<DiffFile["status"], { word: string; token: ColorToken }> = {
  A: { word: "added", token: "alive" },
  M: { word: "modified", token: "dormant" },
  D: { word: "deleted", token: "ghost" },
  T: { word: "type changed", token: "neutral" },
};

export function diffStatusLabel(status: DiffFile["status"]): { letter: DiffFile["status"]; word: string; token: ColorToken } {
  return { letter: status, ...DIFF_STATUS[status] };
}

function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

/** `lazarus-continuity-<project>-<rebirth sha12>.json`; only [a-z0-9-] reaches the file name. */
export function memoryFileName(memory: Pick<ContinuityMemory, "project" | "revival">): string {
  const name = slug(memory.project?.name ?? "") || "project";
  const sha = (memory.revival?.rebirthCommitSha ?? "").toLowerCase().replace(/[^0-9a-f]/g, "").slice(0, 12);
  return `lazarus-continuity-${name}${sha ? `-${sha}` : ""}.json`;
}

/** Digits needed for a diff gutter (at least 3, so short files line up). */
export function gutterDigits(maxLine: number): number {
  return Math.max(3, String(Math.max(0, Math.floor(maxLine))).length);
}
