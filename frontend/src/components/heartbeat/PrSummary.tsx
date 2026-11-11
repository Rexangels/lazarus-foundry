import { Markdown } from "@/components/ui/Markdown";
import { shortSha } from "@/lib/autopsy";
import type { RebirthOut } from "@/lib/types";

/**
 * The pull request Lazarus would open: `pr_title` and `pr_body_markdown` through the safe renderer
 * (text nodes only; only absolute http(s) links), with the branch and commits it describes.
 */
export function PrSummary({ rebirth }: { rebirth: RebirthOut }) {
  return (
    <article aria-labelledby="pr-title" className="space-y-4">
      <header className="space-y-2">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-muted">
          <span className="rounded border border-emerald-bio/60 px-1.5 py-px uppercase tracking-wider text-alive-text">
            <span aria-hidden className="mr-1">⑂</span>Ready to open
          </span>
          <span>
            <code className="text-ink">{rebirth.revival_branch}</code> into{" "}
            <code className="text-ink">{rebirth.project.default_branch || "the default branch"}</code>
          </span>
          <span title={rebirth.base_sha}>base {shortSha(rebirth.base_sha)}</span>
          <span title={rebirth.rebirth_commit_sha}>head {shortSha(rebirth.rebirth_commit_sha)}</span>
        </p>
        <h3 id="pr-title" className="font-display text-lg font-semibold tracking-wide text-ink">
          {rebirth.pr_title || "Untitled pull request"}
        </h3>
      </header>
      {rebirth.pr_body_markdown.trim() ? (
        <Markdown source={rebirth.pr_body_markdown} idPrefix="pr" headingOffset={2} className="max-w-none" />
      ) : (
        <p className="text-sm text-ink-muted">The rebirth stored no PR body.</p>
      )}
    </article>
  );
}
