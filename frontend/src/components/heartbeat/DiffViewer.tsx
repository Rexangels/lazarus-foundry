"use client";

/**
 * The rebirth's unified diff (`git diff --no-renames <base> <rebirth commit>`), as text only: a
 * file list (A/M/D/T, +/−, the exported `.kiro/specs/` files badged) and one collapsible diff per
 * file with old/new line-number gutters, add/del coloring, hunk headers and the truncation notice.
 */
import { useCallback, useMemo, useState } from "react";

import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { parseUnifiedDiff, rebirthFileTotals, withFileStats, type DiffFile, type DiffLine } from "@/lib/heartbeat";
import { MAX_DIFF_ROWS_PER_FILE, diffStatusLabel, gutterDigits, headSlice } from "@/lib/heartbeatView";
import type { RebirthFileOut } from "@/lib/types";

const ROW_CLASS: Record<DiffLine["kind"], string> = {
  add: "bg-emerald-bio/10 text-ink",
  del: "bg-ghost/10 text-ink",
  context: "text-ink/75",
  hunk: "bg-conflict/10 text-conflict-text",
  meta: "italic text-ink-muted",
  note: "italic text-dormant-text",
};

const SIGN: Record<DiffLine["kind"], { mark: string; sr: string; tone: string }> = {
  add: { mark: "+", sr: "added", tone: "text-alive-text" },
  del: { mark: "-", sr: "removed", tone: "text-ghost-text" },
  context: { mark: " ", sr: "", tone: "" },
  hunk: { mark: "", sr: "", tone: "" },
  meta: { mark: "", sr: "", tone: "" },
  note: { mark: "", sr: "", tone: "" },
};

const HIDDEN_META = /^(diff --git |--- |\+\+\+ |index )/;

function splitPath(path: string): [string, string] {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? ["", path] : [path.slice(0, slash + 1), path.slice(slash + 1)];
}

function FileDiff({ file, index, open, onToggle }: { file: DiffFile; index: number; open: boolean; onToggle: (index: number) => void }) {
  const [showAll, setShowAll] = useState(false);
  const rows = useMemo(() => file.hunks.flatMap((h) => h.lines), [file.hunks]);
  const { visible, hidden } = useMemo(() => (showAll ? { visible: rows, hidden: 0 } : headSlice(rows, MAX_DIFF_ROWS_PER_FILE)), [rows, showAll]);
  const digits = useMemo(() => gutterDigits(rows.reduce((max, r) => Math.max(max, r.oldLine ?? 0, r.newLine ?? 0), 0)), [rows]);
  const meta = file.meta.filter((line) => !HIDDEN_META.test(line));
  const status = diffStatusLabel(file.status);
  const [dir, base] = splitPath(file.path);
  const bodyId = `diff-body-${index}`;
  const gutter = { width: `${digits + 1}ch` };

  return (
    <section id={`diff-file-${index}`} aria-label={file.path} className="scroll-mt-24 overflow-hidden rounded-md border border-line bg-obsidian">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => onToggle(index)}
          className="flex w-full flex-wrap items-center gap-2 bg-slate-raised/70 px-3 py-2 text-left hover:bg-slate-raised"
        >
          <span aria-hidden className={cx("font-mono text-xs text-ink-muted transition-transform", open && "rotate-90")}>
            ▸
          </span>
          <TokenBadge token={status.token} label={status.letter} size="sm" title={status.word} appearance="outlined" />
          <span className="sr-only">{status.word}</span>
          <span className="min-w-0 break-all font-mono text-xs">
            <span className="text-ink-muted">{dir}</span>
            <span className="text-ink">{base}</span>
          </span>
          {file.isSpec && <TokenBadge token="conflict" glyph="◆" label="exported spec" size="sm" />}
          <span className="ml-auto font-mono text-[11px] tabular-nums">
            <span className="text-alive-text">+{file.additions}</span> <span className="text-ghost-text">−{file.deletions}</span>
          </span>
        </button>
      </h3>
      {open && (
        <div id={bodyId} className="border-t border-line">
          {meta.length > 0 && (
            <p className="border-b border-line/60 px-3 py-1 font-mono text-[11px] text-ink-muted">{meta.join(" · ")}</p>
          )}
          {file.binary ? (
            <p className="px-3 py-2 font-mono text-xs text-ink-muted">Binary file; no text diff.</p>
          ) : rows.length === 0 ? (
            <p className="px-3 py-2 font-mono text-xs text-ink-muted">No content change (mode or empty file).</p>
          ) : (
            <div tabIndex={0} role="region" aria-label={`Diff of ${file.path}`} className="overflow-x-auto font-mono text-[11px] leading-5">
              <div className="min-w-max">
                {visible.map((line, i) => {
                  const sign = SIGN[line.kind];
                  const full = line.kind === "hunk" || line.kind === "meta" || line.kind === "note";
                  return (
                    <div key={i} className={cx("flex", ROW_CLASS[line.kind])}>
                      <span aria-hidden style={gutter} className="shrink-0 select-none border-r border-line/60 pr-1 text-right text-ink-muted/60">
                        {line.oldLine ?? ""}
                      </span>
                      <span aria-hidden style={gutter} className="shrink-0 select-none border-r border-line/60 pr-1 text-right text-ink-muted/60">
                        {line.newLine ?? ""}
                      </span>
                      {full ? (
                        <span className="whitespace-pre px-2">{line.text}</span>
                      ) : (
                        <>
                          <span className={cx("w-4 shrink-0 select-none text-center", sign.tone)}>
                            <span aria-hidden>{sign.mark}</span>
                            {sign.sr && <span className="sr-only">{sign.sr}</span>}
                          </span>
                          <span className="whitespace-pre pr-4">{line.text || " "}</span>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {hidden > 0 && (
            <div className="border-t border-line px-3 py-2">
              <button
                type="button"
                onClick={() => setShowAll(true)}
                className="font-mono text-[11px] uppercase tracking-wider text-alive-text underline decoration-alive/40 underline-offset-4 hover:text-white"
              >
                Show all {rows.length.toLocaleString()} lines ({hidden.toLocaleString()} more)
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export function DiffViewer({ patch, files }: { patch: string; files: readonly RebirthFileOut[] }) {
  const parsed = useMemo(() => parseUnifiedDiff(patch), [patch]);
  const diffFiles = useMemo(() => withFileStats(parsed, files), [parsed, files]);
  const totals = useMemo(() => rebirthFileTotals(files), [files]);
  const missing = useMemo(() => {
    const inPatch = new Set(diffFiles.map((f) => f.path));
    return files.filter((f) => !inPatch.has(f.path));
  }, [diffFiles, files]);
  // Code files open by default; the exported spec documents start collapsed.
  const [open, setOpen] = useState<Set<number>>(() => new Set(diffFiles.flatMap((f, i) => (f.isSpec ? [] : [i]))));

  const toggle = useCallback((index: number) => {
    setOpen((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const reveal = useCallback((index: number) => {
    setOpen((previous) => (previous.has(index) ? previous : new Set(previous).add(index)));
    requestAnimationFrame(() => {
      const section = document.getElementById(`diff-file-${index}`);
      section?.scrollIntoView({ block: "start" });
      section?.querySelector<HTMLButtonElement>("button[aria-expanded]")?.focus({ preventScroll: true });
    });
  }, []);

  if (!patch.trim()) return <p className="text-sm text-ink-muted">The rebirth stored no diff.</p>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="font-mono text-xs text-ink">
          {totals.files} files changed, <span className="text-alive-text">+{totals.additions}</span>{" "}
          <span className="text-ghost-text">−{totals.deletions}</span>
          {totals.binary > 0 && <span className="text-ink-muted"> · {totals.binary} binary</span>}
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setOpen(new Set(diffFiles.map((_, i) => i)))}
            className="h-7 rounded border border-line px-2 font-mono text-[11px] uppercase tracking-wider text-ink-muted hover:text-ink"
          >
            Expand all
          </button>
          <button
            type="button"
            onClick={() => setOpen(new Set())}
            className="h-7 rounded border border-line px-2 font-mono text-[11px] uppercase tracking-wider text-ink-muted hover:text-ink"
          >
            Collapse all
          </button>
        </div>
      </div>

      {parsed.truncated && (
        <p role="note" className="rounded-md border border-dormant/60 bg-dormant/10 px-3 py-2 font-mono text-xs text-dormant-text">
          <span aria-hidden className="mr-1">⚠</span>
          {parsed.truncationNote ?? "The stored diff was truncated."}
        </p>
      )}

      <div className="overflow-x-auto rounded-md border border-line">
        <table className="w-full border-collapse font-mono text-xs">
          <caption className="sr-only">Files changed by the rebirth</caption>
          <thead className="bg-slate-raised text-left text-[10px] uppercase tracking-[0.18em] text-ink-muted">
            <tr>
              <th scope="col" className="px-3 py-1.5 font-medium">Status</th>
              <th scope="col" className="px-3 py-1.5 font-medium">File</th>
              <th scope="col" className="px-3 py-1.5 text-right font-medium">Added</th>
              <th scope="col" className="px-3 py-1.5 text-right font-medium">Removed</th>
            </tr>
          </thead>
          <tbody>
            {diffFiles.map((file, i) => {
              const status = diffStatusLabel(file.status);
              return (
                <tr key={file.path || i} className="border-t border-line/60 hover:bg-slate-raised/50">
                  <td className="px-3 py-1.5">
                    <TokenBadge token={status.token} label={status.letter} size="sm" appearance="outlined" title={status.word} />
                    <span className="sr-only">{status.word}</span>
                  </td>
                  <td className="px-3 py-1.5">
                    <button
                      type="button"
                      onClick={() => reveal(i)}
                      className="break-all text-left text-ink underline decoration-line underline-offset-4 hover:text-white hover:decoration-emerald-bio"
                    >
                      {file.path}
                    </button>
                    {file.isSpec && <TokenBadge token="conflict" glyph="◆" label="exported spec" size="sm" className="ml-2" />}
                    {file.binary && <span className="ml-2 text-ink-muted">binary</span>}
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-alive-text">+{file.additions}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-ghost-text">−{file.deletions}</td>
                </tr>
              );
            })}
            {missing.map((file) => (
              <tr key={`missing-${file.path}`} className="border-t border-line/60 text-ink-muted">
                <td className="px-3 py-1.5">{file.status}</td>
                <td className="px-3 py-1.5 break-all">
                  {file.path} <span className="text-dormant-text">(not in the stored patch)</span>
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">{file.additions === null ? "bin" : `+${file.additions}`}</td>
                <td className="px-3 py-1.5 text-right tabular-nums">{file.deletions === null ? "bin" : `−${file.deletions}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-3">
        {diffFiles.map((file, i) => (
          <FileDiff key={file.path || i} file={file} index={i} open={open.has(i)} onToggle={toggle} />
        ))}
      </div>
    </div>
  );
}
