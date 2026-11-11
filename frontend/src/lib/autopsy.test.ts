import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  EPISTEMIC_STATUSES,
  VITAL_STATUSES,
  allFilters,
  buildFlowGraph,
  claimCitation,
  countFeatures,
  defibrillatorCta,
  featureNodes,
  findFeature,
  focusForConflict,
  focusForEdge,
  formatLineRange,
  formatLocation,
  formatScore,
  materializingNodes,
  numberSnippetLines,
  revealFeature,
  toggleValue,
  visibleNodeIds,
  type GenomeFilters,
} from "./autopsy";
import { MOCK_FEATURE_IDS, buildMockGenome } from "./mock/paypulse";
import { MOCK_GENOME_COMPLETE_AT, mockFrames } from "./mock/telemetry";
import type { EpistemicStatus, VitalStatus } from "./types";

const genome = buildMockGenome();
const README = "document:README.md";
const BRANCH = "branch:feat/webhook-retry-engine";

describe("buildFlowGraph", () => {
  it("maps every node and edge with the backend ids, types and positions", () => {
    const graph = buildFlowGraph(genome, allFilters());
    expect(graph.nodes.map((node) => node.id)).toEqual(genome.nodes.map((node) => node.id));
    expect(graph.nodes.map((node) => node.type)).toEqual(genome.nodes.map((node) => node.type));
    expect(graph.nodes.map((node) => node.position)).toEqual(genome.nodes.map((node) => node.position));
    expect(graph.edges.map((edge) => edge.id)).toEqual(genome.edges.map((edge) => edge.id));
    expect(graph.nodes.every((node) => node.draggable === false && node.connectable === false)).toBe(true);
  });

  it("routes conflict edges out of the claim handle and evidence edges out of the evidence handle", () => {
    const graph = buildFlowGraph(genome, allFilters());
    for (const edge of graph.edges) {
      expect(edge.sourceHandle).toBe(edge.type === "conflict" ? "claim" : "evidence");
      expect(edge.targetHandle).toBe("in");
    }
    expect(graph.edges.filter((edge) => edge.type === "conflict").every((edge) => edge.target === README)).toBe(true);
  });

  it("keeps C(f) exactly as sent in the node data", () => {
    const graph = buildFlowGraph(genome, allFilters());
    const scores = graph.nodes.flatMap((node) => (node.type === "feature" ? [node.data.feature.confidence_score] : []));
    expect(scores).toEqual(featureNodes(genome).map((node) => node.data.confidence_score));
  });

  it("drops filtered features, their orphaned satellites and every edge touching a hidden node", () => {
    const onlyVerified: GenomeFilters = { vital: new Set<VitalStatus>(["VERIFIED_WORKING"]), epistemic: new Set(EPISTEMIC_STATUSES) };
    const graph = buildFlowGraph(genome, onlyVerified);
    const ids = new Set(graph.nodes.map((node) => node.id));
    expect(ids.has(MOCK_FEATURE_IDS.ledger)).toBe(true);
    expect(ids.has(MOCK_FEATURE_IDS.retries)).toBe(false);
    // README's three conflicts all belong to non-verified features, so it goes too.
    expect(ids.has(README)).toBe(false);
    expect(ids.has(BRANCH)).toBe(false);
    expect(graph.edges).toEqual([]);
  });

  it("keeps a satellite while any of its features is visible", () => {
    const onlyGhosts: GenomeFilters = { vital: new Set<VitalStatus>(["GHOST_FEATURE"]), epistemic: new Set(EPISTEMIC_STATUSES) };
    const graph = buildFlowGraph(genome, onlyGhosts);
    expect(graph.nodes.map((node) => node.id)).toEqual([README, MOCK_FEATURE_IDS.pdf]);
    expect(graph.edges.map((edge) => edge.source)).toEqual([MOCK_FEATURE_IDS.pdf]);
  });

  it("dims everything unrelated to the selection and highlights the focused conflict edge", () => {
    const focus = focusForConflict(genome, genome.conflicts[1]!.id)!;
    const graph = buildFlowGraph(genome, allFilters(), focus);
    const selected = graph.nodes.filter((node) => node.selected).map((node) => node.id);
    expect(selected).toEqual([MOCK_FEATURE_IDS.currency]);
    const undimmed = graph.nodes.filter((node) => !node.data.dimmed).map((node) => node.id).sort();
    expect(undimmed).toEqual([README, MOCK_FEATURE_IDS.currency].sort());
    const highlighted = graph.edges.filter((edge) => edge.type === "conflict" && edge.data?.highlighted);
    expect(highlighted.map((edge) => edge.id)).toEqual([genome.conflicts[1]!.edge_id]);
    expect(highlighted[0]?.selected).toBe(true);
  });

  it("relates a branch selection to the features it carries", () => {
    const graph = buildFlowGraph(genome, allFilters(), { selection: { kind: "branch", id: BRANCH }, conflictId: null });
    const undimmed = graph.nodes.filter((node) => !node.data.dimmed).map((node) => node.id).sort();
    expect(undimmed).toEqual([BRANCH, MOCK_FEATURE_IDS.retries].sort());
  });

  it("never returns an edge whose endpoint is hidden, for any filter combination", () => {
    const subset = <T,>(values: readonly T[]) => fc.subarray([...values]).map((items) => new Set(items));
    fc.assert(
      fc.property(subset(VITAL_STATUSES), subset(EPISTEMIC_STATUSES), (vital, epistemic) => {
        const filters: GenomeFilters = { vital, epistemic };
        const graph = buildFlowGraph(genome, filters);
        const ids = new Set(graph.nodes.map((node) => node.id));
        for (const edge of graph.edges) {
          if (!ids.has(edge.source) || !ids.has(edge.target)) return false;
        }
        const visibleFeatures = graph.nodes.filter((node) => node.type === "feature");
        return visibleFeatures.every(
          (node) =>
            node.type === "feature" &&
            vital.has(node.data.feature.vital_status) &&
            epistemic.has(node.data.feature.epistemic_status),
        );
      }),
    );
  });
});

describe("filters", () => {
  it("toggles values without mutating the original set", () => {
    const original = new Set(["a", "b"]);
    expect([...toggleValue(original, "a")]).toEqual(["b"]);
    expect([...toggleValue(original, "c")]).toEqual(["a", "b", "c"]);
    expect([...original]).toEqual(["a", "b"]);
  });

  it("widens the filters just enough to reveal a hidden feature", () => {
    const pdf = findFeature(genome, MOCK_FEATURE_IDS.pdf)!;
    const hidden: GenomeFilters = { vital: new Set<VitalStatus>(["VERIFIED_WORKING"]), epistemic: new Set<EpistemicStatus>(["KNOWN"]) };
    const revealed = revealFeature(hidden, pdf.data);
    expect([...revealed.vital].sort()).toEqual(["GHOST_FEATURE", "VERIFIED_WORKING"]);
    expect([...revealed.epistemic].sort()).toEqual(["CONFLICTED", "KNOWN"]);
    expect(visibleNodeIds(genome, revealed).has(pdf.id)).toBe(true);
  });

  it("counts features per status", () => {
    const counts = countFeatures(genome);
    expect(counts.total).toBe(6);
    expect(counts.vital).toEqual({ VERIFIED_WORKING: 3, ABANDONED_IN_PROGRESS: 2, GHOST_FEATURE: 1 });
    expect(counts.epistemic).toEqual({ KNOWN: 3, INFERRED: 0, CONFLICTED: 3, MISSING_CONTEXT: 0 });
  });
});

describe("conflict selection", () => {
  it("selects the conflict's feature and its edge", () => {
    for (const record of genome.conflicts) {
      const focus = focusForConflict(genome, record.id);
      expect(focus).toEqual({ selection: { kind: "feature", id: record.feature_id }, conflictId: record.id });
    }
    expect(focusForConflict(genome, "no-such-conflict")).toBeNull();
  });

  it("maps a conflict edge to its conflict and an evidence edge to its feature", () => {
    const record = genome.conflicts[0]!;
    expect(focusForEdge(genome, record.edge_id!)).toEqual(focusForConflict(genome, record.id));
    const evidence = genome.edges.find((edge) => edge.type === "evidence")!;
    expect(focusForEdge(genome, evidence.id)).toEqual({
      selection: { kind: "feature", id: MOCK_FEATURE_IDS.retries },
      conflictId: null,
    });
  });

  it("resolves the claim citation at the conflict's README line", () => {
    const lines = genome.conflicts.map((record) => {
      const citation = claimCitation(genome, record);
      expect(citation?.source_type).toBe("README_CLAIM");
      return `${citation?.file_path}:${citation?.line_start}`;
    });
    expect(lines).toEqual(["README.md:11", "README.md:10", "README.md:12"]);
  });

  it("falls back to the claim location when the citation id is missing, and to null when unresolved", () => {
    const record = { ...genome.conflicts[0]!, claim_citation_id: null };
    expect(claimCitation(genome, record)?.line_start).toBe(11);
    expect(claimCitation(genome, { ...record, claim_file_path: null, claim_line_start: null })).toBeNull();
  });
});

describe("snippets and formatting", () => {
  it("numbers snippet lines from line_start", () => {
    const lines = numberSnippetLines("def convert():\n    raise NotImplementedError\n", 36);
    expect(lines).toEqual([
      { number: 36, text: "def convert():" },
      { number: 37, text: "    raise NotImplementedError" },
    ]);
  });

  it("leaves lines unnumbered without a line range and keeps text exactly", () => {
    expect(numberSnippetLines("@@ new file @@\r\n+class RetryPolicy:", null)).toEqual([
      { number: null, text: "@@ new file @@" },
      { number: null, text: "+class RetryPolicy:" },
    ]);
    expect(numberSnippetLines("", 10)).toEqual([{ number: 10, text: "" }]);
  });

  it("numbers consecutively and preserves every line for any snippet", () => {
    const line = fc.string().map((text) => text.replace(/[\r\n]/g, ""));
    fc.assert(
      fc.property(fc.array(line, { minLength: 1, maxLength: 40 }), fc.integer({ min: 1, max: 100_000 }), (rows, start) => {
        const numbered = numberSnippetLines(rows.join("\n"), start);
        const expected = rows.length > 1 && rows[rows.length - 1] === "" ? rows.slice(0, -1) : rows;
        return (
          numbered.length === expected.length &&
          numbered.every((row, index) => row.number === start + index && row.text === expected[index])
        );
      }),
    );
  });

  it("formats C(f) with exactly four decimals and never changes a four-decimal value", () => {
    expect(formatScore(0.8594)).toBe("0.8594");
    expect(formatScore(0.4)).toBe("0.4000");
    expect(formatScore(null)).toBe("—");
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 10_000 }), (n) => {
        const value = Math.round(n) / 10_000;
        return Number(formatScore(value)) === value;
      }),
    );
  });

  it("formats line ranges and locations", () => {
    expect(formatLineRange(10, 10)).toBe("L10");
    expect(formatLineRange(36, 43)).toBe("L36–43");
    expect(formatLineRange(null, null)).toBeNull();
    expect(formatLocation("README.md", 11, 11)).toBe("README.md:11");
    expect(formatLocation("paypulse/currency.py", 36, 43)).toBe("paypulse/currency.py:36–43");
    expect(formatLocation("paypulse/retry.py", null, null)).toBe("paypulse/retry.py");
  });
});

describe("pending genome and call to action", () => {
  it("collects the features and conflicts announced by telemetry", () => {
    const frames = mockFrames(0, MOCK_GENOME_COMPLETE_AT);
    const { nodes, conflicts } = materializingNodes(frames);
    expect(nodes.map((node) => node.nodeId)).toEqual(featureNodes(genome).map((node) => node.id));
    expect(nodes.map((node) => node.confidence)).toEqual(featureNodes(genome).map((node) => node.data.confidence_score));
    expect(conflicts).toBe(3);
    expect(materializingNodes([...frames, { ...frames[0]!, event_id: "restart" }]).nodes).toEqual([]);
  });

  it("offers the Defibrillator once the autopsy is complete", () => {
    expect(defibrillatorCta("SYNTHESIZING_GENOME")).toBeNull();
    expect(defibrillatorCta("AUTOPSY_COMPLETE")).toBe("charge");
    expect(defibrillatorCta("FOUNDRY_RUNNING")).toBe("link");
    expect(defibrillatorCta("FAILED")).toBeNull();
  });
});
