/**
 * Pure logic of The Defibrillator (View 2): reading the Score(S_k) breakdown, the plan and the
 * deferrals out of the strategies contract, the comparison matrix, the Human Approval Gate's state
 * machine and its error handling. Components in components/defibrillator/ only render these.
 *
 * Contract: backend/apps/revival/schemas.py (StrategiesOut, ApprovalOut, ApprovalErrorOut),
 * scoring.py (PlanScore.as_dict) and approval.py (approval token, recommendation).
 */
import { isApiError } from "./apiError";
import { errorDetail } from "./api";
import type {
  ApprovalOut,
  ProjectStatus,
  StrategiesNotReadyOut,
  StrategiesOut,
  StrategyOut,
  StrategyType,
  TelemetryFrame,
} from "./types";

// ---------------------------------------------------------------------------
// Safe readers (score_breakdown and plan entries are passed through as JSON objects)
// ---------------------------------------------------------------------------
type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function rec(value: unknown): Json {
  return isRecord(value) ? value : {};
}
function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
function records(value: unknown): Json[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

/** Up to `digits` decimals with trailing zeros dropped: 0.39999999999999997 → "0.4", 20.001 → "20.001". */
export function formatDecimal(value: number | null | undefined, digits: number = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const rounded = Number(value.toFixed(digits));
  return Object.is(rounded, -0) ? "0" : String(rounded);
}

/** The score as the backend rounded it (score_decimals, 1 for Score(S_k)): 68.1 → "68.1", 58 → "58.0". */
export function formatScore(value: number | null | undefined, decimals: number = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(decimals);
}

// ---------------------------------------------------------------------------
// Score(S_k) breakdown (PlanScore.as_dict)
// ---------------------------------------------------------------------------
export type TermKey = "value" | "debt" | "risk";

export interface ScoreTerm {
  key: TermKey;
  /** V, D or R */
  symbol: "V" | "D" | "R";
  /** α, β or δ (forensic-invariants §4) */
  weightSymbol: "α" | "β" | "δ";
  label: string;
  weight: number | null;
  /** V, D or R itself. */
  raw: number | null;
  /** What the weight multiplies: V, 1 − D or 1 − R. */
  input: number | null;
  /** Contribution in score points (they sum to the score before rounding). */
  points: number | null;
  inverted: boolean;
}

export interface RiskFactor {
  factor: string;
  count: number | null;
  perUnit: number | null;
  cap: number | null;
  adjustment: number | null;
}

export interface ScoreBreakdownView {
  score: number;
  scoreDecimals: number;
  formula: string;
  terms: ScoreTerm[];
  pointsTotal: number | null;
  value: {
    mode: string;
    totalFeatures: number | null;
    verifiedFeatureIds: string[];
    targets: { featureId: string; title: string; feasibility: number | null }[];
    extractedFeatureIds: string[];
    unrounded: number | null;
  } | null;
  debt: { fileCount: number | null; fileBudget: number | null; files: string[]; unrounded: number | null } | null;
  risk: { base: number | null; factors: RiskFactor[]; risk: number | null } | null;
}

export const SCORE_FORMULA_FALLBACK = "round(100 * (alpha * V + beta * (1 - D) + delta * (1 - R)), 1)";

/**
 * The breakdown of one strategy. Missing parts fall back to the strategy's own score columns, so
 * an older or partial breakdown still renders (and says what is missing) instead of crashing.
 */
export function readScoreBreakdown(strategy: StrategyOut): ScoreBreakdownView {
  const raw = rec(strategy.score_breakdown);
  const weights = rec(raw.weights);
  const terms = rec(raw.terms);
  const parameters = rec(raw.parameters);
  const termOf = (key: TermKey, symbol: ScoreTerm["symbol"], weightSymbol: ScoreTerm["weightSymbol"], label: string, rawValue: number | null, inverted: boolean): ScoreTerm => {
    const t = rec(terms[key]);
    const weight = num(t.weight) ?? num(weights[key]);
    const input = num(t.input) ?? (rawValue === null ? null : inverted ? 1 - rawValue : rawValue);
    const points = num(t.points) ?? (weight !== null && input !== null ? 100 * weight * input : null);
    return { key, symbol, weightSymbol, label, weight, raw: rawValue, input, points, inverted };
  };
  const list = [
    termOf("value", "V", "α", "Value recovered", num(raw.value) ?? strategy.value_score, false),
    termOf("debt", "D", "β", "Debt cost", num(raw.debt_cost) ?? strategy.debt_cost_score, true),
    termOf("risk", "R", "δ", "Strategy risk", num(raw.risk) ?? strategy.risk_score, true),
  ];
  const allPoints = list.map((term) => term.points);
  const pointsTotal = allPoints.every((p): p is number => p !== null) ? allPoints.reduce((a, b) => a + b, 0) : null;

  const vd = isRecord(raw.value_derivation) ? raw.value_derivation : null;
  const dd = isRecord(raw.debt_derivation) ? raw.debt_derivation : null;
  const rd = isRecord(raw.risk_derivation) ? raw.risk_derivation : null;
  return {
    score: num(raw.score) ?? strategy.feasibility_score,
    scoreDecimals: num(parameters.score_decimals) ?? 1,
    formula: str(raw.formula) ?? SCORE_FORMULA_FALLBACK,
    terms: list,
    pointsTotal,
    value: vd && {
      mode: str(vd.mode) ?? "",
      totalFeatures: num(vd.total_features),
      verifiedFeatureIds: strings(vd.verified_feature_ids),
      targets: records(vd.targets).map((t) => ({
        featureId: str(t.feature_id) ?? "",
        title: str(t.title) ?? "",
        feasibility: num(t.feasibility),
      })),
      extractedFeatureIds: strings(vd.extracted_feature_ids),
      unrounded: num(vd.unrounded),
    },
    debt: dd && {
      fileCount: num(dd.file_count),
      fileBudget: num(dd.file_budget),
      files: strings(dd.files),
      unrounded: num(dd.unrounded),
    },
    risk: rd && {
      base: num(rd.base),
      factors: records(rd.factors).map((f) => ({
        factor: str(f.factor) ?? "?",
        count: num(f.count),
        perUnit: num(f.per_unit),
        cap: num(f.cap),
        adjustment: num(f.adjustment),
      })),
      risk: num(rd.risk),
    },
  };
}

/**
 * The formula with the strategy's numbers, in the same notation as the spec bundle's design.md
 * Overview: `round(100 × (0.5 × 0.681 + 0.3 × (1 − 0.3333) + 0.2 × (1 − 0.3)), 1)`.
 */
export function substitutedFormula(view: ScoreBreakdownView): string {
  const [v, d, r] = view.terms as [ScoreTerm, ScoreTerm, ScoreTerm];
  const f = (x: number | null) => formatDecimal(x);
  return (
    `round(100 × (${f(v.weight)} × ${f(v.raw)} + ${f(d.weight)} × (1 − ${f(d.raw)}) + ` +
    `${f(r.weight)} × (1 − ${f(r.raw)})), ${view.scoreDecimals})`
  );
}

/** How V was derived, in one line. */
export function describeValue(view: ScoreBreakdownView): string {
  const v = view.value;
  if (!v) return "No value derivation was recorded.";
  const total = v.totalFeatures ?? 0;
  if (v.mode === "EXTRACT_SHARE") {
    return `${v.extractedFeatureIds.length} extracted of ${total} feature(s)` +
      ` (${v.verifiedFeatureIds.length} verified working).`;
  }
  const sum = v.targets.reduce((acc, t) => acc + (t.feasibility ?? 0), 0);
  return `(${v.verifiedFeatureIds.length} verified + ${formatDecimal(sum)} from ${v.targets.length} target(s) weighted by feasibility) / ${total} feature(s).`;
}

/** `base 0.25 + MERGE_DISTANCE 5 × 0.01 (cap 0.15) = 0.05`, as in the spec bundle. */
export function describeRisk(view: ScoreBreakdownView): string {
  const r = view.risk;
  if (!r) return "No risk derivation was recorded.";
  const parts = r.factors.map(
    (f) =>
      `${f.factor} ${f.count ?? "?"} × ${formatDecimal(f.perUnit)} (cap ${formatDecimal(f.cap)}) = ${formatDecimal(f.adjustment)}`,
  );
  return [`base ${formatDecimal(r.base)}`, ...parts].join(" + ");
}

// ---------------------------------------------------------------------------
// The work order (action_plan)
// ---------------------------------------------------------------------------
export interface PlanItemView {
  id: string;
  kind: string;
  stage: string;
  title: string;
  rationale: string;
  featureId: string | null;
  branch: string | null;
  effort: string;
  feasibility: number | null;
  factors: { name: string; value: number | null; detail: string }[];
  filesToModify: string[];
  filesToCreate: string[];
  targetTests: string[];
  dependsOn: string[];
  openMarkers: string[];
  constraints: string[];
  copies: { source: string; destination: string }[];
  dependency: { name: string; currentSpecifier: string; manifest: string; line: number | null } | null;
  externalRequirements: string[];
  requiresResolution: boolean;
  evidenceCount: number;
}

export function planItems(strategy: StrategyOut): PlanItemView[] {
  return strategy.action_plan.items.map((raw, index) => {
    const dep = isRecord(raw.dependency) ? raw.dependency : null;
    return {
      id: str(raw.id) ?? `item-${index + 1}`,
      kind: str(raw.kind) ?? "?",
      stage: str(raw.stage) ?? "?",
      title: str(raw.title) ?? "(untitled item)",
      rationale: str(raw.rationale) ?? "",
      featureId: str(raw.feature_id),
      branch: str(raw.branch),
      effort: str(raw.effort) ?? "?",
      feasibility: num(raw.item_feasibility),
      factors: records(raw.feasibility_factors).map((f) => ({
        name: str(f.name) ?? "?",
        value: num(f.value),
        detail: str(f.detail) ?? "",
      })),
      filesToModify: strings(raw.files_to_modify),
      filesToCreate: strings(raw.files_to_create),
      targetTests: strings(raw.target_tests),
      dependsOn: strings(raw.depends_on),
      openMarkers: strings(raw.open_markers),
      constraints: strings(raw.constraints),
      copies: records(raw.copies).map((c) => ({ source: str(c.source) ?? "", destination: str(c.destination) ?? "" })),
      dependency: dep && {
        name: str(dep.name) ?? "?",
        currentSpecifier: str(dep.current_specifier) ?? "",
        manifest: str(dep.file_path) ?? str(dep.manifest) ?? "",
        line: num(dep.line_start),
      },
      externalRequirements: records(raw.external_requirements).map((e) => str(e.label) ?? str(e.capability) ?? "?"),
      requiresResolution: raw.requires_resolution === true,
      evidenceCount: Array.isArray(raw.evidence) ? raw.evidence.length : 0,
    };
  });
}

export interface DeferredView {
  featureId: string | null;
  title: string;
  reason: string;
  detail: string;
  partial: boolean;
  wouldTouch: string[];
  externalRequirements: string[];
}

export function deferredItems(strategy: StrategyOut): DeferredView[] {
  return strategy.action_plan.deferred.map((raw) => ({
    featureId: str(raw.feature_id),
    title: str(raw.title) ?? "(untitled feature)",
    reason: str(raw.reason) ?? "?",
    detail: str(raw.detail) ?? "",
    partial: raw.partial === true,
    wouldTouch: strings(raw.would_touch),
    externalRequirements: records(raw.external_requirements).map((e) => str(e.label) ?? str(e.capability) ?? "?"),
  }));
}

export interface InScopeView {
  featureId: string | null;
  title: string;
  feasibility: number | null;
  itemIds: string[];
  partial: boolean;
}

export function inScopeFeatures(strategy: StrategyOut): InScopeView[] {
  return strategy.action_plan.in_scope.map((raw) => ({
    featureId: str(raw.feature_id),
    title: str(raw.title) ?? "(untitled feature)",
    feasibility: num(raw.feasibility),
    itemIds: strings(raw.item_ids),
    partial: raw.partial === true,
  }));
}

export const DEFERRAL_REASON_LABEL: Record<string, string> = {
  NO_DETAILED_SPEC: "No detailed spec",
  NEEDS_NEW_DEPENDENCY: "Needs a new dependency",
  NEEDS_EXTERNAL_SERVICE: "Needs an external service",
  BRANCH_STILL_ACTIVE: "Branch still active",
  NO_ACTIONABLE_EVIDENCE: "No actionable evidence",
  OUTSIDE_SELECTED_CLUSTER: "Outside the extracted cluster",
  NOT_VERIFIED_WORKING: "Not verified working",
};

export const STAGE_ORDER = ["ENVIRONMENT", "ARCHITECTURE", "ENGINEERING"] as const;

/**
 * Feature id → title from every strategy's in-scope, deferred and V-target entries, so the V
 * derivation can name the verified features (the breakdown itself carries only their ids).
 */
export function featureTitles(strategies: readonly StrategyOut[]): Map<string, string> {
  const titles = new Map<string, string>();
  for (const strategy of strategies) {
    for (const entry of [...strategy.action_plan.in_scope, ...strategy.action_plan.deferred]) {
      const id = str(entry.feature_id);
      const title = str(entry.title);
      if (id && title && !titles.has(id)) titles.set(id, title);
    }
    for (const target of readScoreBreakdown(strategy).value?.targets ?? []) {
      if (target.featureId && target.title && !titles.has(target.featureId)) titles.set(target.featureId, target.title);
    }
  }
  return titles;
}

// ---------------------------------------------------------------------------
// The deck: emphasis and comparison
// ---------------------------------------------------------------------------
export const STRATEGY_META: Record<StrategyType, { glyph: string; verb: string; blurb: string }> = {
  RESTORE: { glyph: "↺", verb: "Restore", blurb: "Finish what was abandoned, in the current stack." },
  MODERNIZE: { glyph: "⇪", verb: "Modernize", blurb: "Restore, then lift the pinned dependencies." },
  EXTRACT: { glyph: "⧉", verb: "Extract", blurb: "Lift the verified core out into a clean package." },
};

/** Ids of the strategies with the highest Score(S_k) (ties included). */
export function highestScoreIds(strategies: readonly StrategyOut[]): Set<string> {
  if (strategies.length === 0) return new Set();
  const best = Math.max(...strategies.map((s) => s.feasibility_score));
  return new Set(strategies.filter((s) => s.feasibility_score === best).map((s) => s.id));
}

const TYPE_ORDER: Record<string, number> = { RESTORE: 0, MODERNIZE: 1, EXTRACT: 2 };

/**
 * Mirror of `approval.recommended_strategy_id` (highest score among actionable strategies; ties by
 * type order, then id). The UI shows the backend's `is_recommended`; the mock uses this.
 */
export function recommendedStrategyId(strategies: readonly StrategyOut[]): string | null {
  const candidates = strategies.filter((s) => s.is_actionable);
  if (candidates.length === 0) return null;
  const sorted = [...candidates].sort(
    (a, b) =>
      b.feasibility_score - a.feasibility_score ||
      (TYPE_ORDER[a.strategy_type] ?? 9) - (TYPE_ORDER[b.strategy_type] ?? 9) ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
  return sorted[0]?.id ?? null;
}

/** The strategy to show first: the approved one, else the recommended one, else the first. */
export function defaultSelection(data: StrategiesOut): string | null {
  return (
    data.approved_strategy_id ??
    data.strategies.find((s) => s.approval.approved_by_human)?.id ??
    data.recommended_strategy_id ??
    data.strategies.find((s) => s.is_recommended)?.id ??
    data.strategies[0]?.id ??
    null
  );
}

export interface ComparisonRow {
  key: string;
  label: string;
  /** Formatted cell per strategy id. */
  cells: Record<string, string>;
  /** Ids of the best value in the row (higher is better unless `lowerIsBetter`). */
  best: Set<string>;
  lowerIsBetter: boolean;
}

export function comparisonRows(strategies: readonly StrategyOut[]): ComparisonRow[] {
  const views = strategies.map((s) => ({ s, b: readScoreBreakdown(s) }));
  const row = (
    key: string,
    label: string,
    pick: (s: StrategyOut, b: ScoreBreakdownView) => number | null,
    format: (value: number | null) => string,
    lowerIsBetter = false,
    ranked = true,
  ): ComparisonRow => {
    const values = views.map(({ s, b }) => ({ id: s.id, value: pick(s, b) }));
    const known = values.filter((v): v is { id: string; value: number } => v.value !== null);
    const target = known.length ? (lowerIsBetter ? Math.min : Math.max)(...known.map((v) => v.value)) : null;
    return {
      key,
      label,
      cells: Object.fromEntries(values.map((v) => [v.id, format(v.value)])),
      best: new Set(
        ranked && known.length > 1 && target !== null ? known.filter((v) => v.value === target).map((v) => v.id) : [],
      ),
      lowerIsBetter,
    };
  };
  const term = (key: TermKey) => (_: StrategyOut, b: ScoreBreakdownView) => b.terms.find((t) => t.key === key)?.points ?? null;
  return [
    row("score", "Score(S_k)", (s, b) => b.score ?? s.feasibility_score, (v) => formatScore(v)),
    row("value", "V · value recovered", (s) => s.value_score, (v) => formatDecimal(v)),
    row("debt", "D · debt cost", (s) => s.debt_cost_score, (v) => formatDecimal(v), true),
    row("risk", "R · strategy risk", (s) => s.risk_score, (v) => formatDecimal(v), true),
    row("value_points", "α·V points", term("value"), (v) => formatDecimal(v)),
    row("debt_points", "β·(1 − D) points", term("debt"), (v) => formatDecimal(v)),
    row("risk_points", "δ·(1 − R) points", term("risk"), (v) => formatDecimal(v)),
    // More items is neither better nor worse, so the row is not ranked.
    row("items", "Plan items", (s) => s.action_plan.items.length, (v) => (v === null ? "—" : String(v)), false, false),
    row("files", "Files touched", (s) => s.action_plan.files.length, (v) => (v === null ? "—" : String(v)), true),
    row("deferred", "Deferred", (s) => s.action_plan.deferred.length, (v) => (v === null ? "—" : String(v)), true),
  ];
}

// ---------------------------------------------------------------------------
// The .kiro spec bundle
// ---------------------------------------------------------------------------
export const SPEC_DOCS = [
  { key: "requirements_md", file: "requirements.md", label: "Requirements" },
  { key: "design_md", file: "design.md", label: "Design" },
  { key: "tasks_md", file: "tasks.md", label: "Tasks" },
] as const;
export type SpecDocKey = (typeof SPEC_DOCS)[number]["key"];

export function specPath(specName: string): string {
  return `.kiro/specs/${specName || "<unnamed>"}/`;
}

/** Lines and footnote count of one document, for the tab labels and the gate summary. */
export function documentStats(markdown: string): { lines: number; footnotes: number } {
  const lines = markdown ? markdown.split("\n").length : 0;
  const footnotes = (markdown.match(/^\[\^[^\]]+\]:/gm) ?? []).length;
  return { lines, footnotes };
}

/** `sha256 1a2b3c4d5e6f…9f0e`, for showing which content the token covers. */
export function shortToken(token: string): string {
  return token.length > 20 ? `${token.slice(0, 12)}…${token.slice(-4)}` : token;
}

export function isApprovalToken(token: unknown): token is string {
  return typeof token === "string" && /^[0-9a-f]{64}$/.test(token);
}

// ---------------------------------------------------------------------------
// Loading states
// ---------------------------------------------------------------------------
/**
 * A 409 read as "still coming" or "nothing to show". STRATEGIES_PENDING is pending. The backend also
 * answers STRATEGIES_UNAVAILABLE in the moment between AUTOPSY_COMPLETE and the strategy rows being
 * written; that is still pending unless a status detail or a REVIVAL error says generation failed.
 */
export function classifyNotReady(notReady: StrategiesNotReadyOut, frames: readonly TelemetryFrame[]): "pending" | "unavailable" {
  if (notReady.code === "STRATEGIES_PENDING") return "pending";
  const revivalError = frames.some((f) => f.event_type === "ERROR" && f.payload.stage === "REVIVAL");
  return notReady.status === "AUTOPSY_COMPLETE" && !notReady.status_detail && !revivalError ? "pending" : "unavailable";
}

export interface GeneratedStrategyFrame {
  strategyId: string;
  strategyType: string;
  title: string;
  score: number | null;
}

/** STRATEGY_GENERATED frames, newest generation per type, in RESTORE, MODERNIZE, EXTRACT order. */
export function generatedFromFrames(frames: readonly TelemetryFrame[]): GeneratedStrategyFrame[] {
  const byType = new Map<string, GeneratedStrategyFrame>();
  for (const frame of frames) {
    if (frame.event_type !== "STRATEGY_GENERATED") continue;
    const p = frame.payload;
    const type = typeof p.strategy_type === "string" ? p.strategy_type : "?";
    byType.set(type, {
      strategyId: typeof p.strategy_id === "string" ? p.strategy_id : "",
      strategyType: type,
      title: typeof p.title === "string" ? p.title : "",
      score: typeof p.feasibility_score === "number" ? p.feasibility_score : null,
    });
  }
  return [...byType.values()].sort((a, b) => (TYPE_ORDER[a.strategyType] ?? 9) - (TYPE_ORDER[b.strategyType] ?? 9));
}

/** Statuses after a human approved a strategy (the Heartbeat is the next view). */
export function isPastApproval(status: ProjectStatus | null | undefined): boolean {
  return status === "STRATEGY_APPROVED" || status === "FOUNDRY_RUNNING" || status === "REBORN";
}

// ---------------------------------------------------------------------------
// The Human Approval Gate
// ---------------------------------------------------------------------------
export type GateAvailability =
  | { kind: "open" }
  | { kind: "approved"; intact: boolean | null }
  | { kind: "locked"; approvedId: string; approvedType: StrategyType | null }
  | { kind: "not_ready"; status: ProjectStatus }
  | { kind: "not_actionable" }
  | { kind: "invalid_token" };

/** Whether this strategy can be approved, checked in the backend's order (services.approve_strategy). */
export function gateAvailability(
  data: StrategiesOut,
  strategy: StrategyOut,
  status: ProjectStatus | null | undefined,
): GateAvailability {
  if (strategy.approval.approved_by_human) return { kind: "approved", intact: strategy.approval.approval_intact };
  const approved = data.strategies.find((s) => s.approval.approved_by_human && s.id !== strategy.id);
  const approvedId = data.approved_strategy_id && data.approved_strategy_id !== strategy.id ? data.approved_strategy_id : approved?.id;
  if (approvedId) {
    const type = data.strategies.find((s) => s.id === approvedId)?.strategy_type ?? null;
    return { kind: "locked", approvedId, approvedType: type };
  }
  const current = status ?? data.project.status;
  if (current !== "AUTOPSY_COMPLETE") return { kind: "not_ready", status: current };
  if (!strategy.is_actionable) return { kind: "not_actionable" };
  if (!isApprovalToken(strategy.approval.approval_token)) return { kind: "invalid_token" };
  return { kind: "open" };
}

export type ApprovalRecovery = "refresh" | "approver" | "retry" | "locked" | "none";

export interface ApprovalFailure {
  code: string | null;
  status: number | null;
  title: string;
  message: string;
  recovery: ApprovalRecovery;
}

/** What went wrong with `POST .../approve`, and what the person can do about it. */
export function describeApprovalFailure(error: unknown): ApprovalFailure {
  if (!isApiError(error)) {
    return {
      code: null,
      status: null,
      title: "The approval did not go through",
      message: error instanceof Error ? error.message : "Unexpected error.",
      recovery: "retry",
    };
  }
  if (error.kind === "offline" || error.kind === "timeout") {
    return {
      code: null,
      status: null,
      title: error.kind === "offline" ? "The backend is unreachable" : "The backend did not answer in time",
      message:
        "Nothing was approved as far as this screen knows. Reload the strategies before trying again: if the request reached the server, the strategy shows as approved.",
      recovery: "refresh",
    };
  }
  if (error.kind === "invalid_response") {
    return { code: null, status: error.status, title: "Unexpected response", message: error.message, recovery: "refresh" };
  }
  const detail = errorDetail(error.body) ?? error.message;
  const base = { code: error.code, status: error.status };
  if (error.status === 403) {
    return {
      ...base,
      title: "An approver name is required",
      message: `${detail} Enter your name; it is sent as X-Lazarus-Approver and stored as approved_by.`,
      recovery: "approver",
    };
  }
  if (error.status === 404) {
    return {
      ...base,
      title: "This strategy no longer exists",
      message: "The strategies were regenerated (regenerated rows get new ids). Reload them and review the new content before approving.",
      recovery: "refresh",
    };
  }
  if (error.status === 409) {
    switch (error.code) {
      case "STRATEGY_STALE":
        return {
          ...base,
          title: "The strategy changed after you reviewed it",
          message: `${detail} The approval token no longer matches its plan, scores or spec documents. Reload and review it again.`,
          recovery: "refresh",
        };
      case "ANOTHER_STRATEGY_APPROVED":
        return {
          ...base,
          title: "Another strategy is already approved",
          message: `${detail} A project has at most one approved strategy.`,
          recovery: "locked",
        };
      case "PROJECT_NOT_READY":
        return { ...base, title: "The project is not awaiting approval", message: detail, recovery: "refresh" };
      case "STRATEGY_NOT_ACTIONABLE":
        return { ...base, title: "This strategy has nothing to execute", message: detail, recovery: "none" };
      case "APPROVED_CONTENT_CHANGED":
        return {
          ...base,
          title: "The approved strategy was altered",
          message: `${detail} Its stored content no longer matches what was approved; the Foundry will refuse it.`,
          recovery: "none",
        };
      default:
        return { ...base, title: "The approval was refused", message: detail, recovery: "refresh" };
    }
  }
  if (error.status === 422) {
    return { ...base, title: "The approval request was malformed", message: detail, recovery: "refresh" };
  }
  return { ...base, title: `The backend answered HTTP ${error.status ?? "?"}`, message: detail, recovery: "retry" };
}

/**
 * The gate's two-step interaction: charge (arm) → confirm (shock). The shock only fires from the
 * armed state, so a single click can never approve.
 */
export type GatePhase =
  | { phase: "idle" }
  | { phase: "armed" }
  | { phase: "shocking" }
  | { phase: "approved"; result: ApprovalOut }
  | { phase: "failed"; failure: ApprovalFailure };

export type GateAction =
  | { type: "arm" }
  | { type: "disarm" }
  | { type: "shock" }
  | { type: "success"; result: ApprovalOut }
  | { type: "failure"; failure: ApprovalFailure }
  | { type: "reset" };

export const GATE_IDLE: GatePhase = { phase: "idle" };

export function gateReducer(state: GatePhase, action: GateAction): GatePhase {
  switch (action.type) {
    case "arm":
      return state.phase === "idle" || state.phase === "failed" ? { phase: "armed" } : state;
    case "disarm":
      return state.phase === "armed" || state.phase === "failed" ? GATE_IDLE : state;
    case "shock":
      return state.phase === "armed" ? { phase: "shocking" } : state;
    case "success":
      return state.phase === "shocking" ? { phase: "approved", result: action.result } : state;
    case "failure":
      return state.phase === "shocking" ? { phase: "failed", failure: action.failure } : state;
    case "reset":
      return GATE_IDLE;
  }
}

/** The charge → clear → shock sequence step the screen is on (0, 1, 2; 3 once delivered). */
export function sequenceStep(gate: GatePhase, approved: boolean): 0 | 1 | 2 | 3 {
  if (approved || gate.phase === "approved") return 3;
  if (gate.phase === "shocking") return 2;
  if (gate.phase === "armed") return 1;
  return 0;
}

/** The strategies body after a successful approval, before the refetch confirms it. */
export function applyApproval(data: StrategiesOut, result: ApprovalOut): StrategiesOut {
  const approved = result.strategy;
  return {
    ...data,
    project: { ...data.project, status: result.project_status },
    approved_strategy_id: approved.id,
    strategies: data.strategies.map((s) => (s.id === approved.id ? approved : s)),
  };
}
