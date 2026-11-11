/**
 * Pure logic behind The Autopsy (View 1): mapping the genome contract (design.md, "Genome API
 * contract") to React Flow nodes and edges, filters, selection, conflict lookups, snippet line
 * numbering and the live "materializing" list shown while the genome is pending.
 *
 * Nothing here formats a score in a way that changes it: C(f) arrives rounded to 4 decimals
 * (forensic-invariants.md section 2) and is always shown with exactly 4.
 */
import type { Edge, Node } from "@xyflow/react";

import type {
  BranchNodeData,
  CitationOut,
  ConflictEdgeData,
  ConflictOut,
  DocumentNodeData,
  EpistemicStatus,
  EvidenceEdgeData,
  EvidenceSourceType,
  FeatureNode,
  FeatureNodeData,
  GenomeGraphOut,
  GenomeNodeOut,
  JsonValue,
  ProjectStatus,
  TelemetryFrame,
  VitalStatus,
} from "./types";

// ---------------------------------------------------------------------------
// Enumerations in display order
// ---------------------------------------------------------------------------
export const VITAL_STATUSES: readonly VitalStatus[] = ["VERIFIED_WORKING", "ABANDONED_IN_PROGRESS", "GHOST_FEATURE"];
export const EPISTEMIC_STATUSES: readonly EpistemicStatus[] = ["KNOWN", "INFERRED", "CONFLICTED", "MISSING_CONTEXT"];

export const SOURCE_TYPE_LABEL: Record<EvidenceSourceType, string> = {
  AST_IMPLEMENTATION: "AST implementation",
  UNMERGED_BRANCH_COMMIT: "Unmerged branch commit",
  ISSUE_SPEC: "Issue / spec",
  TODO_COMMENT: "TODO comment",
  README_CLAIM: "README claim",
};

/** Short codes for edge labels, where space is tight. */
export const SOURCE_TYPE_CODE: Record<EvidenceSourceType, string> = {
  AST_IMPLEMENTATION: "AST",
  UNMERGED_BRANCH_COMMIT: "BRANCH",
  ISSUE_SPEC: "SPEC",
  TODO_COMMENT: "TODO",
  README_CLAIM: "README",
};

/** What each `status_reason` / `vital_status_reason` code means (apps/genome/scoring.py, reconciler.py). */
export const STATUS_REASON_TEXT: Record<string, string> = {
  CONFLICT: "A ConflictRecord exists for this feature, so it is CONFLICTED regardless of its score.",
  ABOVE_KNOWN_THRESHOLD: "C(f) ≥ 0.75 with a non-stub, non-test AST implementation on the default branch.",
  NO_DEFAULT_BRANCH_IMPLEMENTATION:
    "C(f) ≥ 0.75, but no non-stub, non-test implementation on the default branch, so it falls through to INFERRED.",
  ABOVE_INFERRED_THRESHOLD: "C(f) ≥ 0.45 with implementation-bearing evidence (code, branch commits or a detailed spec).",
  NO_IMPLEMENTATION_EVIDENCE:
    "Only prose evidence (README claims, TODOs or spec mentions), so it stays MISSING_CONTEXT whatever its score.",
  BELOW_INFERRED_THRESHOLD: "C(f) < 0.45.",
  HEAD_IMPLEMENTATION: "Implemented at the default branch's HEAD, with no stub.",
  HEAD_STUB: "A symbol at HEAD only raises NotImplementedError: the interface exists, the behavior does not.",
  BRANCH_ONLY: "The code exists only on an unmerged branch.",
  TESTS_ONLY: "Only tests on the default branch reference it.",
  NO_IMPLEMENTATION: "No implementation on the default branch or on any unmerged branch.",
};

export function describeStatusReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return STATUS_REASON_TEXT[reason] ?? reason;
}

// ---------------------------------------------------------------------------
// Formatting (never re-rounds C(f) beyond the 4 decimals the backend stores)
// ---------------------------------------------------------------------------
export const SCORE_DIGITS = 4;

/** `0.8594` -> "0.8594". Null, NaN or infinite -> "—". */
export function formatScore(value: number | null | undefined, digits: number = SCORE_DIGITS): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

/** "L10", "L36–43", or null when the citation has no line range. */
export function formatLineRange(start: number | null, end: number | null): string | null {
  if (start === null || !Number.isFinite(start)) return null;
  if (end === null || end === start || !Number.isFinite(end)) return `L${start}`;
  return `L${start}–${end}`;
}

/** "README.md:10", "paypulse/currency.py:36–43" or just the path. */
export function formatLocation(path: string, start: number | null, end: number | null): string {
  if (start === null || !Number.isFinite(start)) return path;
  if (end === null || end === start || !Number.isFinite(end)) return `${path}:${start}`;
  return `${path}:${start}–${end}`;
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 12) : "working tree";
}

// ---------------------------------------------------------------------------
// Snippets
// ---------------------------------------------------------------------------
export interface SnippetLine {
  /** Source line number, or null when the citation has no line range (e.g. a branch diff). */
  number: number | null;
  text: string;
}

/**
 * Split a snippet into lines numbered from `lineStart`. The text is kept exactly (no trimming);
 * only a single trailing newline is dropped so it does not produce an empty extra line.
 */
export function numberSnippetLines(snippet: string, lineStart: number | null): SnippetLine[] {
  const lines = snippet.split(/\r\n|\r|\n/);
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const start = typeof lineStart === "number" && Number.isInteger(lineStart) && lineStart > 0 ? lineStart : null;
  return lines.map((text, index) => ({ number: start === null ? null : start + index, text }));
}

// ---------------------------------------------------------------------------
// Genome lookups
// ---------------------------------------------------------------------------
export function isFeatureNode(node: GenomeNodeOut): node is FeatureNode {
  return node.type === "feature";
}

export function featureNodes(genome: GenomeGraphOut): FeatureNode[] {
  return genome.nodes.filter(isFeatureNode);
}

/** A feature node by node id or by `feature_id` (they are the same IntentNode id in the contract). */
export function findFeature(genome: GenomeGraphOut, featureId: string): FeatureNode | undefined {
  return featureNodes(genome).find((node) => node.id === featureId || node.data.feature_id === featureId);
}

export function citationsForFeature(genome: GenomeGraphOut, feature: FeatureNode): CitationOut[] {
  return genome.citations[feature.data.feature_id] ?? genome.citations[feature.id] ?? [];
}

export function conflictsForFeature(genome: GenomeGraphOut, feature: FeatureNode): ConflictOut[] {
  return genome.conflicts.filter((record) => record.feature_id === feature.data.feature_id || record.feature_id === feature.id);
}

/**
 * The citation that holds a conflict's claim: `claim_citation_id` first, then a README/spec
 * citation of the same feature at the claim's file and first line. Null when unresolved.
 */
export function claimCitation(genome: GenomeGraphOut, conflict: ConflictOut): CitationOut | null {
  const citations = genome.citations[conflict.feature_id] ?? [];
  if (conflict.claim_citation_id) {
    const exact = citations.find((citation) => citation.id === conflict.claim_citation_id);
    if (exact) return exact;
  }
  if (conflict.claim_file_path && conflict.claim_line_start !== null) {
    return (
      citations.find(
        (citation) =>
          (citation.source_type === "README_CLAIM" || citation.source_type === "ISSUE_SPEC") &&
          citation.file_path === conflict.claim_file_path &&
          citation.line_start === conflict.claim_line_start,
      ) ?? null
    );
  }
  return null;
}

export interface FeatureCounts {
  total: number;
  vital: Record<VitalStatus, number>;
  epistemic: Record<EpistemicStatus, number>;
}

export function countFeatures(genome: GenomeGraphOut): FeatureCounts {
  const vital = Object.fromEntries(VITAL_STATUSES.map((status) => [status, 0])) as Record<VitalStatus, number>;
  const epistemic = Object.fromEntries(EPISTEMIC_STATUSES.map((status) => [status, 0])) as Record<EpistemicStatus, number>;
  const features = featureNodes(genome);
  for (const node of features) {
    vital[node.data.vital_status] = (vital[node.data.vital_status] ?? 0) + 1;
    epistemic[node.data.epistemic_status] = (epistemic[node.data.epistemic_status] ?? 0) + 1;
  }
  return { total: features.length, vital, epistemic };
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------
export interface GenomeFilters {
  vital: ReadonlySet<VitalStatus>;
  epistemic: ReadonlySet<EpistemicStatus>;
}

export function allFilters(): GenomeFilters {
  return { vital: new Set(VITAL_STATUSES), epistemic: new Set(EPISTEMIC_STATUSES) };
}

export function isUnfiltered(filters: GenomeFilters): boolean {
  return filters.vital.size === VITAL_STATUSES.length && filters.epistemic.size === EPISTEMIC_STATUSES.length;
}

export function toggleValue<T>(set: ReadonlySet<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

export function isFeatureVisible(data: FeatureNodeData, filters: GenomeFilters): boolean {
  return filters.vital.has(data.vital_status) && filters.epistemic.has(data.epistemic_status);
}

/** The filters widened just enough to show `data` (used when the inspector selects a hidden feature). */
export function revealFeature(filters: GenomeFilters, data: FeatureNodeData): GenomeFilters {
  if (isFeatureVisible(data, filters)) return filters;
  return {
    vital: new Set([...filters.vital, data.vital_status]),
    epistemic: new Set([...filters.epistemic, data.epistemic_status]),
  };
}

/** Features of a satellite (branch or document) node: its `feature_ids` plus any feature with an edge to it. */
function satelliteFeatures(genome: GenomeGraphOut, nodeId: string, declared: readonly string[]): Set<string> {
  const ids = new Set(declared);
  for (const edge of genome.edges) if (edge.target === nodeId) ids.add(edge.source);
  return ids;
}

/**
 * Node ids left visible by the filters. A branch or document node stays while any of its features
 * is visible; one with no features at all (an uncited abandoned branch) always stays.
 */
export function visibleNodeIds(genome: GenomeGraphOut, filters: GenomeFilters): Set<string> {
  const visible = new Set<string>();
  const visibleFeatureIds = new Set<string>();
  for (const node of featureNodes(genome)) {
    if (isFeatureVisible(node.data, filters)) {
      visible.add(node.id);
      visibleFeatureIds.add(node.id);
      visibleFeatureIds.add(node.data.feature_id);
    }
  }
  for (const node of genome.nodes) {
    if (node.type === "feature") continue;
    const features = satelliteFeatures(genome, node.id, node.data.feature_ids);
    if (features.size === 0 || [...features].some((id) => visibleFeatureIds.has(id))) visible.add(node.id);
  }
  return visible;
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------
export type AutopsySelection = { kind: "feature" | "branch" | "document"; id: string } | null;

export interface GraphFocus {
  selection: AutopsySelection;
  /** Conflict id whose edge is highlighted. */
  conflictId: string | null;
}

export const EMPTY_FOCUS: GraphFocus = { selection: null, conflictId: null };

export function selectionForNode(genome: GenomeGraphOut, nodeId: string): AutopsySelection {
  const node = genome.nodes.find((candidate) => candidate.id === nodeId);
  return node ? { kind: node.type, id: node.id } : null;
}

/** Selecting a conflict selects its feature and highlights its edge. */
export function focusForConflict(genome: GenomeGraphOut, conflictId: string): GraphFocus | null {
  const record = genome.conflicts.find((candidate) => candidate.id === conflictId);
  if (!record) return null;
  const feature = findFeature(genome, record.feature_id);
  return { selection: feature ? { kind: "feature", id: feature.id } : null, conflictId: record.id };
}

/** A conflict edge focuses its conflict; an evidence edge selects its feature. */
export function focusForEdge(genome: GenomeGraphOut, edgeId: string): GraphFocus | null {
  const edge = genome.edges.find((candidate) => candidate.id === edgeId);
  if (!edge) return null;
  if (edge.type === "conflict") {
    const fromRecord = genome.conflicts.find((record) => record.edge_id === edge.id || record.id === edge.data.conflict_id);
    if (fromRecord) return focusForConflict(genome, fromRecord.id);
    return { selection: selectionForNode(genome, edge.source), conflictId: edge.data.conflict_id };
  }
  return { selection: selectionForNode(genome, edge.source), conflictId: null };
}

/** The selected node and its direct neighbours; null when nothing is selected. */
export function relatedNodeIds(genome: GenomeGraphOut, selection: AutopsySelection): Set<string> | null {
  if (!selection) return null;
  const related = new Set([selection.id]);
  for (const edge of genome.edges) {
    if (edge.source === selection.id) related.add(edge.target);
    if (edge.target === selection.id) related.add(edge.source);
  }
  if (selection.kind !== "feature") {
    const node = genome.nodes.find((candidate) => candidate.id === selection.id);
    if (node && node.type !== "feature") {
      for (const featureId of node.data.feature_ids) {
        const feature = findFeature(genome, featureId);
        if (feature) related.add(feature.id);
      }
    }
  }
  return related;
}

// ---------------------------------------------------------------------------
// React Flow mapping
// ---------------------------------------------------------------------------
export type FeatureFlowNode = Node<{ feature: FeatureNodeData; dimmed: boolean }, "feature">;
export type BranchFlowNode = Node<{ branch: BranchNodeData; dimmed: boolean }, "branch">;
export type DocumentFlowNode = Node<{ document: DocumentNodeData; dimmed: boolean }, "document">;
export type GenomeFlowNode = FeatureFlowNode | BranchFlowNode | DocumentFlowNode;

export type EvidenceFlowEdge = Edge<{ evidence: EvidenceEdgeData; dimmed: boolean }, "evidence">;
export type ConflictFlowEdge = Edge<{ conflict: ConflictEdgeData; dimmed: boolean; highlighted: boolean }, "conflict">;
export type GenomeFlowEdge = EvidenceFlowEdge | ConflictFlowEdge;

/** Handle ids: a feature's claims leave on the left (documents), its evidence on the right (branches). */
export const HANDLE = { claim: "claim", evidence: "evidence", target: "in" } as const;

export interface FlowGraph {
  nodes: GenomeFlowNode[];
  edges: GenomeFlowEdge[];
}

function featureAriaLabel(data: FeatureNodeData): string {
  return (
    `Feature: ${data.title}. ${data.vital_status}, ${data.epistemic_status}, C(f) ${formatScore(data.confidence_score)}, ` +
    `${data.citation_count} citations, ${data.conflict_count} conflicts. Press Enter for evidence.`
  );
}

/**
 * The genome as React Flow elements, in the backend's order and at the backend's `position`s.
 * Filtered-out nodes and every edge touching one are dropped; with a selection, unrelated
 * elements are dimmed, and the focused conflict's edge is highlighted and selected.
 */
export function buildFlowGraph(genome: GenomeGraphOut, filters: GenomeFilters, focus: GraphFocus = EMPTY_FOCUS): FlowGraph {
  const visible = visibleNodeIds(genome, filters);
  const related = relatedNodeIds(genome, focus.selection);
  const dimmed = (id: string) => related !== null && !related.has(id);
  const base = { draggable: false, connectable: false, selectable: true, focusable: true } as const;

  const nodes: GenomeFlowNode[] = [];
  for (const node of genome.nodes) {
    if (!visible.has(node.id)) continue;
    const common = {
      ...base,
      id: node.id,
      position: { x: node.position.x, y: node.position.y },
      selected: focus.selection?.id === node.id,
    };
    if (node.type === "feature") {
      nodes.push({ ...common, type: "feature", data: { feature: node.data, dimmed: dimmed(node.id) }, ariaLabel: featureAriaLabel(node.data) });
    } else if (node.type === "branch") {
      nodes.push({
        ...common,
        type: "branch",
        data: { branch: node.data, dimmed: dimmed(node.id) },
        ariaLabel:
          `Branch ${node.data.name}${node.data.is_abandoned ? ", abandoned" : ""}, dormant ${node.data.dormancy_days} days, ` +
          `${node.data.commits_ahead} ahead, ${node.data.commits_behind} behind. Press Enter for details.`,
      });
    } else {
      nodes.push({
        ...common,
        type: "document",
        data: { document: node.data, dimmed: dimmed(node.id) },
        ariaLabel: `Document ${node.data.file_path}, ${node.data.conflict_count} conflicting claims. Press Enter for the claims.`,
      });
    }
  }

  const edges: GenomeFlowEdge[] = [];
  for (const edge of genome.edges) {
    if (!visible.has(edge.source) || !visible.has(edge.target)) continue;
    const isDimmed = dimmed(edge.source) || dimmed(edge.target);
    if (edge.type === "evidence") {
      edges.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: "evidence",
        sourceHandle: HANDLE.evidence,
        targetHandle: HANDLE.target,
        selectable: true,
        focusable: true,
        data: { evidence: edge.data, dimmed: isDimmed },
        ariaLabel: `Evidence: ${edge.data.citation_count} citations (${edge.data.source_types.join(", ")})`,
      });
    } else {
      const highlighted = focus.conflictId !== null && edge.data.conflict_id === focus.conflictId;
      edges.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: "conflict",
        sourceHandle: HANDLE.claim,
        targetHandle: HANDLE.target,
        selectable: true,
        focusable: true,
        selected: highlighted,
        zIndex: highlighted ? 10 : 1,
        data: { conflict: edge.data, dimmed: isDimmed && !highlighted, highlighted },
        ariaLabel: `Conflict: ${edge.data.claim_source}`,
      });
    }
  }
  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// Pending genome: live telemetry
// ---------------------------------------------------------------------------
export interface MaterializingNode {
  nodeId: string;
  title: string;
  vitalStatus: VitalStatus | null;
  epistemicStatus: EpistemicStatus | null;
  confidence: number | null;
  citationCount: number | null;
}

function asString(value: JsonValue | undefined): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: JsonValue | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Features announced by GENOME_NODE_CREATED frames so far (first appearance order, latest payload
 * wins) and the CONFLICT_DETECTED count. A new INGESTION_STARTED starts over.
 */
export function materializingNodes(frames: readonly TelemetryFrame[]): { nodes: MaterializingNode[]; conflicts: number } {
  let byId = new Map<string, MaterializingNode>();
  let conflicts = 0;
  for (const frame of frames) {
    if (frame.event_type === "INGESTION_STARTED") {
      byId = new Map();
      conflicts = 0;
    } else if (frame.event_type === "CONFLICT_DETECTED") {
      conflicts += 1;
    } else if (frame.event_type === "GENOME_NODE_CREATED") {
      const p = frame.payload;
      const nodeId = asString(p.node_id);
      if (!nodeId) continue;
      const vital = asString(p.vital_status);
      const epistemic = asString(p.epistemic_status);
      byId.set(nodeId, {
        nodeId,
        title: asString(p.title) ?? "Unnamed feature",
        vitalStatus: vital && (VITAL_STATUSES as readonly string[]).includes(vital) ? (vital as VitalStatus) : null,
        epistemicStatus:
          epistemic && (EPISTEMIC_STATUSES as readonly string[]).includes(epistemic) ? (epistemic as EpistemicStatus) : null,
        confidence: asNumber(p.confidence),
        citationCount: asNumber(p.citation_count),
      });
    }
  }
  return { nodes: [...byId.values()], conflicts };
}

// ---------------------------------------------------------------------------
// Call to action
// ---------------------------------------------------------------------------
/** `charge`: the autopsy just finished and the Defibrillator is the next step; `link`: already past it. */
export function defibrillatorCta(status: ProjectStatus | null | undefined): "charge" | "link" | null {
  if (status === "AUTOPSY_COMPLETE") return "charge";
  if (status === "STRATEGY_APPROVED" || status === "FOUNDRY_RUNNING" || status === "REBORN") return "link";
  return null;
}
