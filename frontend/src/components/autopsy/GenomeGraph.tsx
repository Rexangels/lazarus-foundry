"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  type EdgeChange,
  type EdgeTypes,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import { useCallback, useMemo } from "react";

import {
  buildFlowGraph,
  focusForEdge,
  selectionForNode,
  type GenomeFilters,
  type GenomeFlowEdge,
  type GenomeFlowNode,
  type GraphFocus,
} from "@/lib/autopsy";
import { COLOR_TOKEN_HEX, normalizeColorToken } from "@/lib/palette";
import type { GenomeGraphOut } from "@/lib/types";

import { ConflictEdgeView, EvidenceEdgeView } from "./GenomeEdges";
import { BranchNodeView, DocumentNodeView, FeatureNodeView } from "./GenomeNodes";

const nodeTypes = { feature: FeatureNodeView, branch: BranchNodeView, document: DocumentNodeView } satisfies NodeTypes;
const edgeTypes = { evidence: EvidenceEdgeView, conflict: ConflictEdgeView } satisfies EdgeTypes;

function minimapColor(node: GenomeFlowNode): string {
  if (node.type === "feature") return COLOR_TOKEN_HEX[normalizeColorToken(node.data.feature.color_token)];
  if (node.type === "branch") return COLOR_TOKEN_HEX[normalizeColorToken(node.data.branch.color_token)];
  return COLOR_TOKEN_HEX.conflict;
}

/**
 * The React Flow Genome Graph. Positions are the backend's (`graph_position`), so nodes are not
 * draggable. Selection is owned by the Autopsy view: a click, or Enter/Space on a focused node or
 * edge, reports a focus; clicking the empty canvas (or Esc on the selected node) clears it.
 */
export function GenomeGraph({
  genome,
  filters,
  focus,
  onFocus,
  onClear,
}: {
  genome: GenomeGraphOut;
  filters: GenomeFilters;
  focus: GraphFocus;
  onFocus: (focus: GraphFocus) => void;
  onClear: () => void;
}) {
  const graph = useMemo(() => buildFlowGraph(genome, filters, focus), [genome, filters, focus]);

  const onNodesChange = useCallback(
    (changes: NodeChange<GenomeFlowNode>[]) => {
      const picked = changes.find((change) => change.type === "select" && change.selected);
      if (picked && picked.type === "select") {
        const selection = selectionForNode(genome, picked.id);
        if (selection) onFocus({ selection, conflictId: null });
        return;
      }
      const current = focus.selection?.id;
      if (current && changes.some((change) => change.type === "select" && !change.selected && change.id === current)) {
        onClear();
      }
    },
    [genome, focus.selection?.id, onFocus, onClear],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange<GenomeFlowEdge>[]) => {
      const picked = changes.find((change) => change.type === "select" && change.selected);
      if (picked && picked.type === "select") {
        const next = focusForEdge(genome, picked.id);
        if (next) onFocus(next);
      }
    },
    [genome, onFocus],
  );

  return (
    <div className="relative h-[min(72vh,660px)] min-h-[460px] overflow-hidden rounded-md border border-line bg-obsidian/70">
      <ReactFlow<GenomeFlowNode, GenomeFlowEdge>
        className="genome-flow"
        colorMode="dark"
        nodes={graph.nodes}
        edges={graph.edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        nodesFocusable
        edgesFocusable
        fitView
        fitViewOptions={{ padding: 0.16 }}
        minZoom={0.3}
        maxZoom={1.8}
        zoomOnScroll={false}
        preventScrolling={false}
        zoomOnDoubleClick
        attributionPosition="top-right"
        aria-label="Project genome graph"
      >
        <Background variant={BackgroundVariant.Lines} gap={48} lineWidth={1} />
        <Controls showInteractive={false} position="bottom-left" />
        <MiniMap<GenomeFlowNode>
          position="bottom-right"
          pannable
          zoomable
          ariaLabel="Genome graph overview"
          nodeColor={minimapColor}
          nodeStrokeWidth={0}
          nodeBorderRadius={6}
        />
      </ReactFlow>
      <p className="pointer-events-none absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted/80">
        claims ← features → branches · Ctrl + scroll to zoom · Tab to a node, Enter for evidence
      </p>
    </div>
  );
}
