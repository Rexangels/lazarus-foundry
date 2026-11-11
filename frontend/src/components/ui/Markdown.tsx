import { Fragment, useMemo, type ReactNode } from "react";

import { cx } from "./cx";
import { parseInline, parseMarkdown, type MdBlock, type MdInline, type MdList } from "@/lib/markdown";

/** One evidence_index entry, used for the footnote tooltips. */
type EvidenceIndex = Record<string, Record<string, unknown>>;

function evidenceTitle(index: EvidenceIndex | undefined, label: string): string | undefined {
  const entry = index?.[label];
  if (!entry) return undefined;
  const parts = [entry.location, entry.source_type, entry.branch_name ? `on ${String(entry.branch_name)}` : null]
    .filter((part) => typeof part === "string" && part)
    .join(" · ");
  const snippet = typeof entry.snippet === "string" ? entry.snippet.trim() : "";
  return snippet ? `${parts}\n${snippet}` : parts || undefined;
}

interface Ctx {
  idPrefix: string;
  evidence?: EvidenceIndex;
}

function Inline({ nodes, ctx }: { nodes: readonly MdInline[]; ctx: Ctx }) {
  return (
    <>
      {nodes.map((node, i) => {
        switch (node.type) {
          case "text":
            return <Fragment key={i}>{node.text}</Fragment>;
          case "code":
            return (
              <code key={i} className="rounded bg-obsidian px-1 py-px font-mono text-[0.85em] text-alive-text ring-1 ring-line">
                {node.text}
              </code>
            );
          case "strong":
            return (
              <strong key={i} className="font-semibold text-ink">
                <Inline nodes={node.children} ctx={ctx} />
              </strong>
            );
          case "footnoteRef":
            return (
              <sup key={i} className="mx-px">
                <a
                  href={`#${ctx.idPrefix}-fn-${node.label}`}
                  title={evidenceTitle(ctx.evidence, node.label)}
                  className="rounded px-0.5 font-mono text-[10px] text-conflict-text no-underline hover:bg-conflict/15 hover:text-white"
                >
                  {node.label}
                </a>
              </sup>
            );
          case "link":
            // lib/markdown.ts only produces absolute http(s) hrefs.
            return (
              <a
                key={i}
                href={node.href}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="text-alive-text underline decoration-alive/40 underline-offset-2 hover:text-white"
              >
                <Inline nodes={node.children} ctx={ctx} />
              </a>
            );
        }
      })}
    </>
  );
}

/** One line of planner text with its `code spans` (rendered as text nodes, like the documents). */
export function InlineMarkdown({ text }: { text: string }) {
  const nodes = useMemo(() => parseInline(text), [text]);
  return <Inline nodes={nodes} ctx={{ idPrefix: "inline" }} />;
}

function List({ list, ctx, depth }: { list: MdList; ctx: Ctx; depth: number }) {
  const Tag = list.ordered ? "ol" : "ul";
  return (
    <Tag
      start={list.ordered && list.start !== 1 ? list.start : undefined}
      className={cx(
        "space-y-1",
        list.ordered ? "list-decimal pl-6 marker:font-mono marker:text-ink-muted" : "pl-5",
        depth > 0 && "mt-1",
      )}
    >
      {list.items.map((item, i) => (
        <li
          key={i}
          className={cx(
            !list.ordered && item.checked === null && "list-disc marker:text-ink-muted/70",
            !list.ordered && item.checked !== null && "-ml-5 list-none",
          )}
        >
          {item.checked !== null && (
            <span
              aria-label={item.checked ? "done" : "to do"}
              role="img"
              className={cx(
                "mr-2 inline-flex h-3.5 w-3.5 translate-y-0.5 items-center justify-center rounded-sm border font-mono text-[9px]",
                item.checked ? "border-alive bg-alive/20 text-alive-text" : "border-ink-muted/60",
              )}
            >
              {item.checked ? "✓" : ""}
            </span>
          )}
          <Inline nodes={item.children} ctx={ctx} />
          {item.sublists.map((sub, j) => (
            <List key={j} list={sub} ctx={ctx} depth={depth + 1} />
          ))}
        </li>
      ))}
    </Tag>
  );
}

const HEADING_CLASS: Record<number, string> = {
  1: "font-display text-lg font-semibold uppercase tracking-[0.08em] text-ink",
  2: "mt-5 border-b border-line pb-1 font-display text-base font-semibold uppercase tracking-[0.1em] text-alive-text",
  3: "mt-4 font-display text-sm font-semibold tracking-wide text-ink",
  4: "mt-3 font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted",
  5: "mt-3 text-sm font-semibold text-ink",
  6: "mt-3 text-sm text-ink-muted",
};

/**
 * Renders a generated spec document. Everything is a React text node: no `dangerouslySetInnerHTML`,
 * no raw HTML, and no links other than in-document footnote anchors and absolute http(s) links
 * (opened in a new tab with `rel="noopener noreferrer nofollow"`).
 * `headingOffset` shifts the document's `#` levels below the page's own headings.
 */
export function Markdown({
  source,
  idPrefix,
  evidence,
  headingOffset = 2,
  className,
}: {
  source: string;
  idPrefix: string;
  evidence?: EvidenceIndex;
  headingOffset?: number;
  className?: string;
}) {
  const blocks = useMemo(() => parseMarkdown(source), [source]);
  const ctx: Ctx = { idPrefix, evidence };
  const body: ReactNode[] = [];
  const footnotes: Extract<MdBlock, { type: "footnoteDef" }>[] = [];
  blocks.forEach((block, i) => {
    switch (block.type) {
      case "heading": {
        const level = Math.min(6, block.level + headingOffset) as 1 | 2 | 3 | 4 | 5 | 6;
        const H = `h${level}` as const;
        body.push(
          <H key={i} className={HEADING_CLASS[block.level]}>
            <Inline nodes={block.children} ctx={ctx} />
          </H>,
        );
        break;
      }
      case "paragraph":
        body.push(
          <p key={i}>
            <Inline nodes={block.children} ctx={ctx} />
          </p>,
        );
        break;
      case "list":
        body.push(<List key={i} list={block} ctx={ctx} depth={0} />);
        break;
      case "code":
        body.push(
          <pre key={i} className="overflow-x-auto rounded border border-line bg-obsidian p-3 font-mono text-xs text-ink">
            {block.text}
          </pre>,
        );
        break;
      case "rule":
        body.push(<hr key={i} className="border-line" />);
        break;
      case "footnoteDef":
        footnotes.push(block);
        break;
      case "table": {
        const alignClass = (j: number) =>
          ({ left: "text-left", center: "text-center", right: "text-right" } as const)[block.align[j] ?? "left"];
        body.push(
          <div key={i} className="overflow-x-auto rounded border border-line">
            <table className="w-full border-collapse font-mono text-xs">
              <thead className="bg-slate-raised text-ink-muted">
                <tr>
                  {block.header.map((cell, j) => (
                    <th key={j} scope="col" className={cx("border-b border-line px-2 py-1 font-semibold", alignClass(j))}>
                      <Inline nodes={cell} ctx={ctx} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={r} className="border-b border-line/60 last:border-0">
                    {row.map((cell, j) => (
                      <td key={j} className={cx("px-2 py-1", alignClass(j))}>
                        <Inline nodes={cell} ctx={ctx} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>,
        );
        break;
      }
    }
  });
  return (
    <div className={cx("space-y-2 text-sm leading-relaxed text-ink/90", className)}>
      {body}
      {footnotes.length > 0 && (
        <section aria-label="Evidence footnotes" className="mt-6 border-t border-line pt-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Evidence</p>
          <ol className="mt-2 space-y-1 font-mono text-[11px] text-ink-muted">
            {footnotes.map((note) => (
              <li key={note.label} id={`${idPrefix}-fn-${note.label}`} className="scroll-mt-24 target:rounded target:bg-conflict/10 target:text-ink">
                <span className="mr-2 text-conflict-text">{note.label}</span>
                <Inline nodes={note.children} ctx={ctx} />
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
