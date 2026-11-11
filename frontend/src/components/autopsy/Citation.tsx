import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import {
  SOURCE_TYPE_LABEL,
  formatLineRange,
  formatLocation,
  formatScore,
  numberSnippetLines,
  shortSha,
} from "@/lib/autopsy";
import type { CitationOut, ColorToken, EvidenceSourceType } from "@/lib/types";

/** Source types are knowledge, not life: outlined chips with a glyph. Code is emerald, branch work amber. */
const SOURCE_TYPE_STYLE: Record<EvidenceSourceType, { token: ColorToken; glyph: string }> = {
  AST_IMPLEMENTATION: { token: "alive", glyph: "{}" },
  UNMERGED_BRANCH_COMMIT: { token: "dormant", glyph: "⎇" },
  ISSUE_SPEC: { token: "neutral", glyph: "§" },
  TODO_COMMENT: { token: "dormant", glyph: "✎" },
  README_CLAIM: { token: "neutral", glyph: "¶" },
};

export function SourceTypeBadge({ type }: { type: EvidenceSourceType }) {
  const style = SOURCE_TYPE_STYLE[type] ?? { token: "neutral" as const, glyph: "·" };
  return (
    <TokenBadge
      token={style.token}
      label={SOURCE_TYPE_LABEL[type] ?? type}
      glyph={style.glyph}
      appearance="outlined"
      size="sm"
      title={`Source type: ${type}`}
    />
  );
}

/**
 * The cited text, verbatim, in a mono block whose gutter starts at `line_start`. Without a line
 * range (a branch diff) the gutter is blank instead of guessing numbers.
 */
export function SnippetBlock({
  snippet,
  lineStart,
  lineEnd,
  filePath,
  className,
}: {
  snippet: string;
  lineStart: number | null;
  lineEnd: number | null;
  filePath: string;
  className?: string;
}) {
  const lines = numberSnippetLines(snippet, lineStart);
  const width = String(lines[lines.length - 1]?.number ?? "").length;
  const range = formatLineRange(lineStart, lineEnd);
  return (
    <pre
      tabIndex={0}
      aria-label={`Snippet from ${filePath}${range ? `, lines ${range.slice(1)}` : ", no line range"}`}
      className={cx(
        "max-h-72 overflow-auto rounded-md border border-line bg-obsidian/90 py-2 font-mono text-[12px] leading-[1.6] text-ink",
        className,
      )}
    >
      <code className="block min-w-max">
        {lines.map((line, index) => (
          <span key={index} className="flex">
            <span
              aria-hidden
              className="sticky left-0 shrink-0 select-none border-r border-line bg-obsidian/95 px-2 text-right tabular-nums text-ink-muted/80"
              style={{ minWidth: `${Math.max(width, 2) + 2}ch` }}
            >
              {line.number ?? " "}
            </span>
            <span className="whitespace-pre pl-3 pr-4">{line.text === "" ? " " : line.text}</span>
          </span>
        ))}
      </code>
    </pre>
  );
}

/** One EvidenceCitation: source type, `file:line_start–line_end`, commit, branch, weight and the snippet. */
export function CitationCard({ citation, highlight = false }: { citation: CitationOut; highlight?: boolean }) {
  const location = formatLocation(citation.file_path, citation.line_start, citation.line_end);
  return (
    <article
      id={`citation-${citation.id}`}
      aria-label={`${SOURCE_TYPE_LABEL[citation.source_type] ?? citation.source_type} at ${location}`}
      className={cx(
        "scroll-mt-24 rounded-md border bg-slate-raised/60 p-3",
        highlight ? "border-conflict/60 shadow-glow-conflict" : "border-line",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <SourceTypeBadge type={citation.source_type} />
        {citation.is_branch_only && (
          <TokenBadge token="dormant" label="Branch-only" glyph="◐" size="sm" title="Found only on an unmerged branch" />
        )}
        {citation.signal_index !== null && (
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">signal #{citation.signal_index}</span>
        )}
      </div>
      <p className="mt-2 break-all font-mono text-[13px] text-ink">{location}</p>
      <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-ink-muted">
        <div className="flex gap-1">
          <dt>commit</dt>
          <dd className="text-ink" title={citation.commit_sha || "working tree"}>
            {shortSha(citation.commit_sha)}
          </dd>
        </div>
        <div className="flex gap-1">
          <dt>branch</dt>
          <dd className={citation.is_branch_only ? "text-dormant-text" : "text-ink"}>{citation.branch_name ?? "unknown"}</dd>
        </div>
        <div className="flex gap-1">
          <dt>weight</dt>
          <dd className="tabular-nums text-ink">{formatScore(citation.weight)}</dd>
        </div>
        {citation.line_start === null && (
          <div className="flex gap-1">
            <dt>lines</dt>
            <dd className="text-ink">none (diff hunk)</dd>
          </div>
        )}
      </dl>
      {citation.snippet ? (
        <SnippetBlock
          snippet={citation.snippet}
          lineStart={citation.line_start}
          lineEnd={citation.line_end}
          filePath={citation.file_path}
          className="mt-3"
        />
      ) : (
        <p className="mt-3 font-mono text-xs text-ink-muted">(empty snippet)</p>
      )}
    </article>
  );
}
