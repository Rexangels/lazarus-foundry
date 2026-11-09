/**
 * TypeScript mirror of the Django Ninja schemas (the "API contract" sections of
 * .kiro/specs/core-resurrection-engine/design.md). Sources:
 *   backend/apps/projects/api.py      (IngestRequest, IngestAccepted, ProjectOut, BranchOut)
 *   backend/apps/genome/schemas.py    (GenomeGraphOut and below)
 *   backend/apps/revival/schemas.py   (StrategiesOut, ApprovalOut and below)
 *   backend/apps/rebirth/schemas.py   (RebirthOut, RebirthNotReadyOut)
 *   backend/apps/telemetry/events.py  (TelemetryEventType, TelemetryFrame)
 * The forensic-schema-guard hook asks for this file to be re-checked whenever a schemas.py changes.
 * UUIDs and datetimes arrive as strings.
 */

export type Uuid = string;
export type IsoDateTime = string;

// ---------------------------------------------------------------------------
// Shared enums
// ---------------------------------------------------------------------------
export const PROJECT_STATUSES = [
  "DORMANT",
  "INGESTING",
  "SYNTHESIZING_GENOME",
  "AUTOPSY_COMPLETE",
  "STRATEGY_APPROVED",
  "FOUNDRY_RUNNING",
  "REBORN",
  "FAILED",
] as const;
export type ProjectStatus = (typeof PROJECT_STATUSES)[number];

export type VitalStatus = "VERIFIED_WORKING" | "ABANDONED_IN_PROGRESS" | "GHOST_FEATURE";
export type EpistemicStatus = "KNOWN" | "INFERRED" | "CONFLICTED" | "MISSING_CONTEXT";
export type FeatureCategory = "CORE_FEATURE" | "ARCHITECTURE" | "INTEGRATION" | "DATA_MODEL";
export type EvidenceSourceType =
  | "AST_IMPLEMENTATION"
  | "UNMERGED_BRANCH_COMMIT"
  | "ISSUE_SPEC"
  | "TODO_COMMENT"
  | "README_CLAIM";
export type SynthesisMode = "DETERMINISTIC" | "MODEL" | "MODEL_FALLBACK";
/** The backend never sends hex colors, only these tokens (design.md, "Color tokens"). */
export type ColorToken = "alive" | "dormant" | "ghost" | "conflict" | "neutral";
export type StrategyType = "RESTORE" | "MODERNIZE" | "EXTRACT";
export type AgentStage = "ENVIRONMENT" | "ARCHITECTURE" | "ENGINEERING" | "VERIFICATION" | "COMPLETE" | "FAILED";

/** `{detail}` bodies (404 and other Ninja errors). 422 bodies carry `detail` as a list. */
export interface ErrorOut {
  detail: string;
}

// ---------------------------------------------------------------------------
// Projects (apps/projects/api.py)
// ---------------------------------------------------------------------------
/** `POST /api/projects/ingest` body: exactly one of `repo_url` or `demo`. Unknown fields are rejected. */
export type IngestRequest =
  | { repo_url: string; name?: string; demo?: never }
  | { demo: "paypulse"; name?: string; repo_url?: never };

/** 202 */
export interface IngestAccepted {
  project_id: Uuid;
  name: string;
  status: ProjectStatus;
  is_golden_demo: boolean;
  telemetry_ws_path: string;
}

/** 503: the task queue (Redis) was unreachable; the project was created and marked FAILED. */
export interface QueueUnavailable {
  detail: string;
  project_id: Uuid;
  status: ProjectStatus;
}

export interface IngestionSummaryOut {
  branch_count: number;
  abandoned_branch_count: number;
  artifact_counts: Record<string, number>;
  file_status_counts: Record<string, Record<string, number>>;
}

/** `GET /api/projects/{project_id}` */
export interface ProjectOut {
  id: Uuid;
  name: string;
  repo_url: string;
  is_golden_demo: boolean;
  status: ProjectStatus;
  status_detail: string;
  default_branch: string;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  ingested_at: IsoDateTime | null;
  telemetry_ws_path: string;
  ingestion: IngestionSummaryOut;
}

/** `GET /api/projects/{project_id}/branches` (default branch first) */
export interface BranchOut {
  name: string;
  head_sha: string;
  last_commit_date: IsoDateTime;
  is_default: boolean;
  is_abandoned: boolean;
  commits_ahead: number;
  commits_behind: number;
  diff_summary: Record<string, unknown>;
}

/** `GET /api/health` */
export interface HealthResponse {
  status: "ok";
  service: string;
  version: string;
  timestamp: IsoDateTime;
}

/** The `project` block shared by the genome, strategies and rebirth bodies. */
export interface ProjectRefOut {
  id: Uuid;
  name: string;
  status: ProjectStatus;
  status_detail: string;
  default_branch: string;
}

// ---------------------------------------------------------------------------
// Genome (apps/genome/schemas.py): GET /api/projects/{project_id}/genome
// ---------------------------------------------------------------------------
export interface GraphPosition {
  x: number;
  y: number;
}

export type GenomeProjectOut = ProjectRefOut;

export interface SynthesisValidationOut {
  dropped_citation_count: number;
  unsupported_citation_count: number;
  rejected_feature_count: number;
  dropped_conflict_count: number;
  vital_status_override_count: number;
  fallback_reason: string | null;
}

export interface SynthesisOut {
  id: Uuid | null;
  mode: SynthesisMode;
  model_id: string;
  started_at: IsoDateTime;
  finished_at: IsoDateTime;
  catalog_size: number;
  node_count: number;
  conflict_count: number;
  input_digest_prefix: string;
  validation: SynthesisValidationOut;
}

export interface FeatureNodeData {
  feature_id: string;
  feature_key: string | null;
  title: string;
  category: FeatureCategory;
  vital_status: VitalStatus;
  epistemic_status: EpistemicStatus;
  confidence_score: number;
  reconstructed_intent: string;
  actual_implementation_state: string;
  citation_count: number;
  conflict_count: number;
  color_token: ColorToken;
}

export interface FeatureNode {
  id: string;
  type: "feature";
  position: GraphPosition;
  data: FeatureNodeData;
}

export interface BranchNodeData {
  name: string;
  head_sha: string;
  last_commit_date: IsoDateTime | null;
  is_abandoned: boolean;
  is_merged: boolean;
  commits_ahead: number;
  commits_behind: number;
  dormancy_days: number;
  citation_count: number;
  feature_ids: string[];
  color_token: ColorToken;
}

export interface BranchNode {
  id: string;
  type: "branch";
  position: GraphPosition;
  data: BranchNodeData;
}

export interface DocumentNodeData {
  file_path: string;
  conflict_count: number;
  feature_ids: string[];
  color_token: ColorToken;
}

export interface DocumentNode {
  id: string;
  type: "document";
  position: GraphPosition;
  data: DocumentNodeData;
}

export type GenomeNodeOut = FeatureNode | BranchNode | DocumentNode;

export interface EvidenceEdgeData {
  citation_count: number;
  source_types: EvidenceSourceType[];
  color_token: ColorToken;
}

export interface EvidenceEdge {
  id: string;
  source: string;
  target: string;
  type: "evidence";
  data: EvidenceEdgeData;
}

export interface ConflictEdgeData {
  conflict_id: string;
  claim_source: string;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  claim_citation_id: string | null;
  color_token: ColorToken;
}

export interface ConflictEdge {
  id: string;
  source: string;
  target: string;
  type: "conflict";
  data: ConflictEdgeData;
}

export type GenomeEdgeOut = EvidenceEdge | ConflictEdge;

export interface CitationOut {
  id: string;
  feature_id: string;
  source_type: EvidenceSourceType;
  file_path: string;
  commit_sha: string;
  line_start: number | null;
  line_end: number | null;
  snippet: string;
  weight: number;
  branch_name: string | null;
  is_branch_only: boolean;
  artifact_id: number | null;
  signal_index: number | null;
}

export interface ConflictOut {
  id: string;
  feature_id: string;
  claim_source: string;
  reality_source: string;
  conflict_description: string;
  resolution_recommendation: string;
  created_at: IsoDateTime | null;
  claim_citation_id: string | null;
  claim_file_path: string | null;
  claim_line_start: number | null;
  claim_line_end: number | null;
  document_node_id: string | null;
  edge_id: string | null;
}

export interface ScoreTermOut {
  signal_index: number;
  rank: number;
  match_strength: number;
  age_days: number;
  decay: number;
  base_term: number;
  rank_factor: number;
  term: number;
}

export interface ScoreContributionOut {
  source_type: EvidenceSourceType;
  authority_weight: number;
  signal_count: number;
  uncapped: number;
  cap: number;
  contribution: number;
  capped: boolean;
  terms: ScoreTermOut[];
}

export interface ScoreCitationOut {
  signal_index: number;
  catalog_id: string;
  source_type: EvidenceSourceType;
  match_kind: string;
  match_strength: number;
  weight: number;
}

export interface ScoreBreakdownOut {
  confidence: number;
  z: number | null;
  evidence_total: number | null;
  prior_bias: number | null;
  conflict: boolean;
  conflict_penalty: number | null;
  contributions: ScoreContributionOut[];
  epistemic_status: EpistemicStatus;
  status_reason: string | null;
  vital_status: VitalStatus;
  vital_status_reason: string | null;
  feature_key: string | null;
  origin: string | null;
  matched_evidence_count: number | null;
  citations: ScoreCitationOut[];
  parameters: Record<string, unknown>;
}

export interface BranchSummaryOut {
  name: string;
  head_sha: string;
  last_commit_date: IsoDateTime | null;
  is_default: boolean;
  is_merged: boolean;
  is_abandoned: boolean;
  commits_ahead: number;
  commits_behind: number;
  dormancy_days: number;
  node_id: string | null;
}

export interface GenomeGraphOut {
  project: GenomeProjectOut;
  synthesis: SynthesisOut | null;
  nodes: GenomeNodeOut[];
  edges: GenomeEdgeOut[];
  /** Keyed by feature id (Evidence Drawer). */
  citations: Record<string, CitationOut[]>;
  conflicts: ConflictOut[];
  /** Keyed by feature id (explainable C(f)). */
  score_breakdowns: Record<string, ScoreBreakdownOut>;
  branches: BranchSummaryOut[];
}

/** 409 for the genome, strategies and rebirth reads: there is nothing to show yet. */
export interface NotReadyOut<Code extends string> {
  detail: string;
  code: Code;
  project_id: Uuid;
  status: ProjectStatus;
  status_detail: string;
  telemetry_ws_path: string;
}

export type GenomeNotReadyCode = "GENOME_PENDING" | "GENOME_UNAVAILABLE";
export type GenomeNotReadyOut = NotReadyOut<GenomeNotReadyCode>;

// ---------------------------------------------------------------------------
// Revival (apps/revival/schemas.py): strategies and the human approval gate
// ---------------------------------------------------------------------------
export type StrategyProjectOut = ProjectRefOut;

export interface StrategySynthesisValidationOut {
  fallback_reason: string | null;
  refined_bundle_count: number;
  applied_prose_count: number;
  dropped_prose_count: number;
}

export interface StrategySynthesisOut {
  id: Uuid;
  mode: SynthesisMode;
  model_id: string;
  started_at: IsoDateTime;
  finished_at: IsoDateTime;
  preserved_strategy_types: string[];
  validation: StrategySynthesisValidationOut;
}

/** Plan entries are passed through as JSON objects; their shape is design.md "PlanItem". */
export interface ActionPlanOut {
  items: Record<string, unknown>[];
  in_scope: Record<string, unknown>[];
  deferred: Record<string, unknown>[];
  files: string[];
  extract_root: string | null;
  risk_evidence: Record<string, unknown>;
}

export interface SpecDocumentLinksOut {
  requirements_md: string;
  design_md: string;
  tasks_md: string;
}

export interface SpecBundleOut {
  spec_name: string;
  requirements_md: string;
  design_md: string;
  tasks_md: string;
  evidence_index: Record<string, Record<string, unknown>>;
  mode: string;
  generated_at: string;
  document_urls: SpecDocumentLinksOut;
}

export interface ApprovalStateOut {
  approved_by_human: boolean;
  approved_at: IsoDateTime | null;
  approved_by: string;
  /** Token of the current content; send it back to approve exactly what was reviewed. */
  approval_token: string;
  approval_intact: boolean | null;
}

export interface StrategyOut {
  id: Uuid;
  strategy_type: StrategyType;
  title: string;
  summary: string;
  feasibility_score: number;
  value_score: number;
  debt_cost_score: number;
  risk_score: number;
  /** apps.revival.scoring.PlanScore.as_dict() */
  score_breakdown: Record<string, unknown>;
  is_actionable: boolean;
  is_recommended: boolean;
  action_plan: ActionPlanOut;
  spec: SpecBundleOut;
  approval: ApprovalStateOut;
  synthesis_id: Uuid | null;
  created_at: IsoDateTime;
}

export interface StrategiesOut {
  project: StrategyProjectOut;
  synthesis: StrategySynthesisOut | null;
  recommended_strategy_id: Uuid | null;
  approved_strategy_id: Uuid | null;
  telemetry_ws_path: string;
  /** RESTORE, MODERNIZE, EXTRACT */
  strategies: StrategyOut[];
}

export type StrategiesNotReadyCode = "STRATEGIES_PENDING" | "STRATEGIES_UNAVAILABLE";
export type StrategiesNotReadyOut = NotReadyOut<StrategiesNotReadyCode>;

export interface ApproveRequest {
  /** 64 lowercase hex characters. */
  approval_token: string;
}

export interface ApprovalOut {
  strategy: StrategyOut;
  foundry_run_id: Uuid | null;
  project_status: ProjectStatus;
  already_approved: boolean;
  telemetry_ws_path: string;
}

export type ApprovalErrorCode =
  | "PROJECT_NOT_READY"
  | "STRATEGY_STALE"
  | "STRATEGY_NOT_ACTIONABLE"
  | "ANOTHER_STRATEGY_APPROVED"
  | "APPROVED_CONTENT_CHANGED";

export interface ApprovalErrorOut {
  detail: string;
  code: ApprovalErrorCode;
  project_id: Uuid;
  strategy_id: Uuid;
  current_approval_token: string | null;
  approved_strategy_id: Uuid | null;
}

export interface ApproverRequiredOut {
  detail: string;
  code: "APPROVER_REQUIRED";
}

// ---------------------------------------------------------------------------
// Rebirth (apps/rebirth/schemas.py): GET /api/projects/{project_id}/rebirth
// ---------------------------------------------------------------------------
export type RebirthProjectOut = ProjectRefOut;

export interface RebirthFileOut {
  path: string;
  /** A, M, D or T */
  status: string;
  /** null for a binary file */
  additions: number | null;
  deletions: number | null;
}

export interface RebirthOut {
  id: Uuid;
  project: RebirthProjectOut;
  foundry_run_id: Uuid;
  strategy_id: Uuid;
  strategy_type: StrategyType;
  revival_branch: string;
  base_sha: string;
  rebirth_commit_sha: string;
  pr_title: string;
  pr_body_markdown: string;
  unified_diff_patch: string;
  files: RebirthFileOut[];
  exported_kiro_specs_path: string;
  spec_files: string[];
  continuity_memory: Record<string, unknown>;
  preview_url: string;
  before_test_output: string;
  after_test_output: string;
  tests_passed: boolean;
  iterations_used: number;
  created_at: IsoDateTime;
  telemetry_ws_path: string;
}

export type RebirthNotReadyCode = "REBIRTH_PENDING" | "REBIRTH_UNAVAILABLE";
export type RebirthNotReadyOut = NotReadyOut<RebirthNotReadyCode>;

// ---------------------------------------------------------------------------
// Telemetry (apps/telemetry/events.py): ws/projects/<uuid>/telemetry/
// ---------------------------------------------------------------------------
export const TELEMETRY_EVENT_TYPES = [
  // System 1: ingestion
  "INGESTION_STARTED",
  "BRANCH_DISCOVERED",
  "AST_PARSED",
  "INGESTION_COMPLETE",
  // System 2: genome
  "GENOME_NODE_CREATED",
  "CONFLICT_DETECTED",
  "GENOME_COMPLETE",
  // System 3: revival
  "STRATEGY_GENERATED",
  "STRATEGY_APPROVED",
  // System 4: foundry
  "AGENT_STAGE_CHANGED",
  "SANDBOX_LOG",
  "TEST_PASSED",
  "TEST_FAILED",
  // System 5: rebirth
  "REBIRTH_COMPLETE",
  // Any stage
  "ERROR",
] as const;
export type TelemetryEventType = (typeof TELEMETRY_EVENT_TYPES)[number];

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** One `{"type": "telemetry"}` frame. Order by `timestamp`, de-duplicate by `event_id`. */
export interface TelemetryFrame {
  type: "telemetry";
  event_id: Uuid;
  project_id: Uuid;
  event_type: TelemetryEventType;
  payload: Record<string, JsonValue>;
  timestamp: IsoDateTime;
}

export type ServerFrame = TelemetryFrame | { type: "pong" } | { type: "error"; detail: string };
