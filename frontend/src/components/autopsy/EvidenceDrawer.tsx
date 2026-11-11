"use client";

/**
 * The Evidence Citations drawer. It opens for the selected graph node:
 * - feature: reconstructed intent, actual implementation state, the C(f) breakdown, its conflicts
 *   and every citation with its exact snippet and lines;
 * - branch: dormancy, divergence and the branch-only evidence it holds;
 * - document: the contradicted claims it holds.
 * It is a non-modal dialog: focus moves to its heading when it opens or its subject changes, Esc
 * closes it from anywhere, and focus returns to whatever opened it.
 */
import { useEffect, useId, useRef, type ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { Readout, ReadoutGrid } from "@/components/ui/Readout";
import { EpistemicBadge, TokenBadge, VitalBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import {
  citationsForFeature,
  claimCitation,
  conflictsForFeature,
  findFeature,
  formatScore,
  shortSha,
  type GraphFocus,
} from "@/lib/autopsy";
import { normalizeColorToken } from "@/lib/palette";
import type { BranchNode, DocumentNode, FeatureNode, GenomeGraphOut } from "@/lib/types";

import { CitationCard } from "./Citation";
import { ConflictRecordView } from "./ConflictInspector";
import { ScoreBreakdown } from "./ScoreBreakdown";

function Section({ title, children, id }: { title: string; children: ReactNode; id?: string }) {
  return (
    <section aria-labelledby={id} className="border-t border-line/70 pt-4">
      <h3 id={id} className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em] text-ink-muted">
        {title}
      </h3>
      {children}
    </section>
  );
}

function FeatureContent({
  genome,
  feature,
  conflictId,
  onFocus,
}: {
  genome: GenomeGraphOut;
  feature: FeatureNode;
  conflictId: string | null;
  onFocus: (focus: GraphFocus) => void;
}) {
  const data = feature.data;
  const token = normalizeColorToken(data.color_token);
  const citations = citationsForFeature(genome, feature);
  const conflicts = conflictsForFeature(genome, feature);
  const breakdown = genome.score_breakdowns[data.feature_id] ?? genome.score_breakdowns[feature.id];
  const focusedConflict = conflicts.find((record) => record.id === conflictId) ?? null;
  const highlightedCitation = focusedConflict ? claimCitation(genome, focusedConflict)?.id ?? null : null;
  const uid = useId();

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <VitalBadge status={data.vital_status} />
        <EpistemicBadge status={data.epistemic_status} />
        <TokenBadge token="neutral" label={data.category.replace(/_/g, " ")} appearance="outlined" />
      </div>
      {data.feature_key && <p className="break-all font-mono text-[11px] text-ink-muted">{data.feature_key}</p>}

      <ReadoutGrid className="sm:grid-cols-3">
        <Readout label="Confidence C(f)" value={formatScore(data.confidence_score)} token={token} />
        <Readout label="Citations" value={data.citation_count} />
        <Readout label="Conflicts" value={data.conflict_count} token={data.conflict_count ? "conflict" : undefined} />
      </ReadoutGrid>

      <Section title="Reconstructed intent" id={`${uid}-intent`}>
        <p className="text-sm leading-relaxed text-ink">{data.reconstructed_intent}</p>
      </Section>
      <Section title="Actual implementation state" id={`${uid}-state`}>
        <p className="text-sm leading-relaxed text-ink">{data.actual_implementation_state}</p>
      </Section>

      <Section title="Why C(f) is what it is" id={`${uid}-score`}>
        <ScoreBreakdown breakdown={breakdown} confidence={data.confidence_score} citations={citations} />
      </Section>

      {conflicts.length > 0 && (
        <Section title={`Conflicts · ${conflicts.length}`} id={`${uid}-conflicts`}>
          <ul className="space-y-3">
            {conflicts.map((record) => {
              const active = record.id === conflictId;
              return (
                <li
                  key={record.id}
                  className={cx("rounded-md border p-3", active ? "border-conflict shadow-glow-conflict" : "border-line")}
                >
                  <ConflictRecordView genome={genome} conflict={record} compact />
                  <Button
                    size="sm"
                    variant={active ? "primary" : "secondary"}
                    className="mt-3"
                    aria-pressed={active}
                    onClick={() => onFocus({ selection: { kind: "feature", id: feature.id }, conflictId: active ? null : record.id })}
                  >
                    {active ? "Edge highlighted" : "Highlight conflict edge"}
                  </Button>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      <Section title={`Evidence citations · ${citations.length}`} id={`${uid}-citations`}>
        {citations.length === 0 ? (
          <p role="alert" className="rounded-md border border-dormant/50 bg-dormant/10 px-3 py-2 text-sm text-ink">
            No citation for this feature is in this response, so its claims cannot be checked here.
          </p>
        ) : (
          <>
            {citations.length < data.citation_count && (
              <p className="mb-3 text-xs text-ink-muted">
                This response includes {citations.length} of the {data.citation_count} citations on record.
              </p>
            )}
            <ol className="space-y-3">
              {citations.map((citation) => (
                <li key={citation.id}>
                  <CitationCard citation={citation} highlight={citation.id === highlightedCitation} />
                </li>
              ))}
            </ol>
          </>
        )}
      </Section>
    </div>
  );
}

function FeatureLinks({
  genome,
  featureIds,
  onFocus,
}: {
  genome: GenomeGraphOut;
  featureIds: readonly string[];
  onFocus: (focus: GraphFocus) => void;
}) {
  const features = featureIds.map((id) => findFeature(genome, id)).filter((node): node is FeatureNode => Boolean(node));
  if (features.length === 0) return <p className="text-sm text-ink-muted">No feature cites it.</p>;
  return (
    <ul className="space-y-2">
      {features.map((node) => (
        <li key={node.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line px-3 py-2">
          <span className="min-w-0">
            <span className="block text-sm text-ink">{node.data.title}</span>
            <span className="mt-1 flex flex-wrap gap-1.5">
              <VitalBadge status={node.data.vital_status} size="sm" />
              <EpistemicBadge status={node.data.epistemic_status} size="sm" />
            </span>
          </span>
          <Button size="sm" variant="secondary" onClick={() => onFocus({ selection: { kind: "feature", id: node.id }, conflictId: null })}>
            Open evidence
          </Button>
        </li>
      ))}
    </ul>
  );
}

function BranchContent({
  genome,
  branch,
  onFocus,
}: {
  genome: GenomeGraphOut;
  branch: BranchNode;
  onFocus: (focus: GraphFocus) => void;
}) {
  const data = branch.data;
  const summary = genome.branches.find((candidate) => candidate.name === data.name);
  const uid = useId();
  const branchCitations = Object.values(genome.citations)
    .flat()
    .filter((citation) => citation.branch_name === data.name);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        {data.is_abandoned && <TokenBadge token="dormant" label="Abandoned" glyph="◐" />}
        <TokenBadge token="neutral" label={data.is_merged ? "Merged" : "Unmerged"} appearance="outlined" dashed={!data.is_merged} />
        {summary?.is_default && <TokenBadge token="neutral" label="Default branch" appearance="outlined" />}
      </div>
      <ReadoutGrid className="sm:grid-cols-3">
        <Readout label="Dormant" value={`${data.dormancy_days} d`} token={data.is_abandoned ? "dormant" : undefined} />
        <Readout label="Ahead" value={`+${data.commits_ahead}`} token="alive" />
        <Readout label="Behind" value={`−${data.commits_behind}`} token="ghost" />
        <Readout label="Head" value={<span className="text-sm">{shortSha(data.head_sha)}</span>} />
        <Readout
          label="Last commit"
          value={<span className="text-sm">{data.last_commit_date ? data.last_commit_date.slice(0, 10) : "unknown"}</span>}
        />
        <Readout label="Citations" value={data.citation_count} />
      </ReadoutGrid>
      <Section title="Features evidenced on this branch" id={`${uid}-features`}>
        <FeatureLinks genome={genome} featureIds={data.feature_ids} onFocus={onFocus} />
      </Section>
      <Section title={`Evidence on this branch · ${branchCitations.length}`} id={`${uid}-citations`}>
        {branchCitations.length === 0 ? (
          <p className="text-sm text-ink-muted">
            {data.citation_count > 0
              ? `${data.citation_count} citations are on record, but none is included in this response.`
              : "No citation draws on this branch; it is shown because it is abandoned."}
          </p>
        ) : (
          <ol className="space-y-3">
            {branchCitations.map((citation) => (
              <li key={citation.id}>
                <CitationCard citation={citation} />
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}

function DocumentContent({
  genome,
  document: doc,
  conflictId,
  onFocus,
}: {
  genome: GenomeGraphOut;
  document: DocumentNode;
  conflictId: string | null;
  onFocus: (focus: GraphFocus) => void;
}) {
  const uid = useId();
  const conflicts = genome.conflicts.filter(
    (record) => record.document_node_id === doc.id || (record.document_node_id === null && record.claim_file_path === doc.data.file_path),
  );
  return (
    <div className="space-y-5">
      <ReadoutGrid className="sm:grid-cols-2">
        <Readout label="Conflicting claims" value={doc.data.conflict_count} token="conflict" />
        <Readout label="Features" value={doc.data.feature_ids.length} />
      </ReadoutGrid>
      <Section title={`Contradicted claims · ${conflicts.length}`} id={`${uid}-claims`}>
        <ul className="space-y-3">
          {conflicts.map((record) => {
            const feature = findFeature(genome, record.feature_id);
            const active = record.id === conflictId;
            return (
              <li key={record.id} className={cx("rounded-md border p-3", active ? "border-conflict shadow-glow-conflict" : "border-line")}>
                <p className="mb-2 font-display text-sm font-semibold tracking-wide text-ink">{feature?.data.title ?? "Unknown feature"}</p>
                <ConflictRecordView genome={genome} conflict={record} compact />
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant={active ? "primary" : "secondary"}
                    aria-pressed={active}
                    onClick={() => onFocus({ selection: { kind: "document", id: doc.id }, conflictId: active ? null : record.id })}
                  >
                    {active ? "Edge highlighted" : "Highlight conflict edge"}
                  </Button>
                  {feature && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onFocus({ selection: { kind: "feature", id: feature.id }, conflictId: record.id })}
                    >
                      Open feature evidence
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </Section>
    </div>
  );
}

function DrawerBody({
  genome,
  focus,
  onClose,
  onFocus,
}: {
  genome: GenomeGraphOut;
  focus: GraphFocus & { selection: NonNullable<GraphFocus["selection"]> };
  onClose: () => void;
  onFocus: (focus: GraphFocus) => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const headingId = useId();
  const { selection } = focus;

  // Remember what opened the drawer and give focus back to it on close.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (opener && opener.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  // Move focus into the drawer whenever its subject changes.
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
    scrollerRef.current?.scrollTo({ top: 0 });
  }, [selection.kind, selection.id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const node = genome.nodes.find((candidate) => candidate.id === selection.id);
  let eyebrow = "Evidence citations";
  let title = "Not in this genome";
  let content: ReactNode = <p className="text-sm text-ink-muted">This node is not part of the current genome.</p>;
  if (node?.type === "feature") {
    eyebrow = "Evidence citations · feature";
    title = node.data.title;
    content = <FeatureContent genome={genome} feature={node} conflictId={focus.conflictId} onFocus={onFocus} />;
  } else if (node?.type === "branch") {
    eyebrow = "Branch forensics";
    title = node.data.name;
    content = <BranchContent genome={genome} branch={node} onFocus={onFocus} />;
  } else if (node?.type === "document") {
    eyebrow = "Document claims";
    title = node.data.file_path;
    content = <DocumentContent genome={genome} document={node} conflictId={focus.conflictId} onFocus={onFocus} />;
  }

  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-labelledby={headingId}
      className="fixed bottom-0 right-0 top-14 z-[35] flex w-full flex-col border-l border-line bg-slate-core/[0.97] shadow-[-24px_0_60px_-20px_rgb(0_0_0/0.9)] backdrop-blur-md motion-safe:animate-drawer-in sm:w-[min(42rem,94vw)]"
    >
      <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-emerald-bio">{eyebrow}</p>
          <h2
            ref={headingRef}
            id={headingId}
            tabIndex={-1}
            className="mt-1 break-words font-display text-xl font-semibold tracking-wide text-ink focus:outline-none focus-visible:outline-2 focus-visible:outline-emerald-bio"
          >
            {title}
          </h2>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close the evidence drawer (Esc)">
          <span aria-hidden>✕</span> Esc
        </Button>
      </header>
      <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        {content}
      </div>
    </div>
  );
}

export function EvidenceDrawer({
  genome,
  focus,
  onClose,
  onFocus,
}: {
  genome: GenomeGraphOut;
  focus: GraphFocus;
  onClose: () => void;
  onFocus: (focus: GraphFocus) => void;
}) {
  if (!focus.selection) return null;
  return (
    <DrawerBody
      genome={genome}
      focus={{ selection: focus.selection, conflictId: focus.conflictId }}
      onClose={onClose}
      onFocus={onFocus}
    />
  );
}
