"use client";

/**
 * The Foundry's sandbox terminal: SANDBOX_LOG stdout and stderr plus the stage, test, error and
 * rebirth events (lib/heartbeat.ts terminalLines). A polite `role="log"` region whose rows are
 * committed in throttled batches; it follows new lines until the user scrolls up, then offers
 * "Jump to live". At most MAX_TERMINAL_DOM_ROWS rows are rendered (the newest).
 */
import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { cx } from "@/components/ui/cx";
import { PIPELINE_STAGES, filterTerminal, terminalLines, type PipelineStage, type TerminalKind, type TerminalLine } from "@/lib/heartbeat";
import { MAX_TERMINAL_DOM_ROWS, TERMINAL_THROTTLE_MS, isNearBottom, tailSlice } from "@/lib/heartbeatView";
import type { TelemetryFrame } from "@/lib/types";

import { useThrottledValue } from "./useThrottledValue";

const KIND_STYLE: Record<TerminalKind, { tag: string; row: string }> = {
  stdout: { tag: "out", row: "text-ink/85" },
  stderr: { tag: "err", row: "text-dormant-text" },
  command: { tag: "$", row: "font-semibold text-ink" },
  stage: { tag: "»", row: "text-conflict-text" },
  "test-pass": { tag: "✓", row: "text-alive-text" },
  "test-fail": { tag: "✕", row: "text-ghost-text" },
  error: { tag: "!", row: "bg-ghost/10 text-ghost-text" },
  success: { tag: "♥", row: "bg-emerald-bio/10 font-semibold text-alive-text" },
};

const TAG_TEXT: Record<TerminalKind, string> = {
  stdout: "stdout",
  stderr: "stderr",
  command: "command",
  stage: "stage",
  "test-pass": "tests passed",
  "test-fail": "tests failed",
  error: "error",
  success: "rebirth",
};

const STAGE_SHORT: Record<PipelineStage, string> = {
  ENVIRONMENT: "ENV",
  ARCHITECTURE: "ARCH",
  ENGINEERING: "ENG",
  VERIFICATION: "VERIFY",
};

const Row = memo(function Row({ line }: { line: TerminalLine }) {
  const style = KIND_STYLE[line.kind];
  return (
    <div className={cx("flex gap-3 px-3 leading-5", style.row)}>
      <span className="w-6 shrink-0 select-none text-right text-ink-muted/70">
        <span aria-hidden>{style.tag}</span>
        <span className="sr-only">{TAG_TEXT[line.kind]}:</span>
      </span>
      <span className="hidden w-12 shrink-0 select-none text-ink-muted/60 sm:inline" aria-hidden>
        {line.stage ? STAGE_SHORT[line.stage] : ""}
      </span>
      <span className="min-w-0 whitespace-pre-wrap break-all">{line.text || " "}</span>
    </div>
  );
});

function Chip({ pressed, onClick, children, label }: { pressed: boolean; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      aria-label={label}
      onClick={onClick}
      className={cx(
        "h-7 rounded border px-2 font-mono text-[11px] uppercase tracking-wider transition-colors",
        pressed ? "border-emerald-bio/70 bg-emerald-bio/15 text-alive-text" : "border-line text-ink-muted hover:border-ink-muted/60 hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

export function LiveTerminal({ frames, live }: { frames: readonly TelemetryFrame[]; live: boolean }) {
  const shownFrames = useThrottledValue(frames, TERMINAL_THROTTLE_MS);
  const buffer = useMemo(() => terminalLines(shownFrames), [shownFrames]);
  const [stage, setStage] = useState<PipelineStage | "ALL">("ALL");
  const [stdout, setStdout] = useState(true);
  const [stderr, setStderr] = useState(true);
  const filtered = useMemo(() => filterTerminal(buffer.lines, { stage, stdout, stderr }), [buffer.lines, stage, stdout, stderr]);
  const { visible, hidden } = useMemo(() => tailSlice(filtered, MAX_TERMINAL_DOM_ROWS), [filtered]);
  const counts = useMemo(() => {
    const byStage = new Map<PipelineStage, number>();
    let streamOut = 0;
    let streamErr = 0;
    for (const line of buffer.lines) {
      if (line.stage) byStage.set(line.stage, (byStage.get(line.stage) ?? 0) + 1);
      if (line.kind === "stdout") streamOut += 1;
      if (line.kind === "stderr") streamErr += 1;
    }
    return { byStage, streamOut, streamErr };
  }, [buffer.lines]);

  const scroller = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  // Total lines seen when the user scrolled away, for the "n new" count.
  const [pausedAt, setPausedAt] = useState(0);
  const total = buffer.lines.length + buffer.dropped;

  useLayoutEffect(() => {
    const el = scroller.current;
    if (follow && el) el.scrollTop = el.scrollHeight;
  }, [visible, follow]);

  const followRef = useRef(true);
  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const near = isNearBottom(el);
    if (near === followRef.current) return;
    followRef.current = near;
    if (!near) setPausedAt(total);
    setFollow(near);
  }, [total]);

  const jumpToLive = useCallback(() => {
    followRef.current = true;
    setFollow(true);
    scroller.current?.focus({ preventScroll: true });
  }, []);

  const unseen = follow ? 0 : Math.max(0, total - pausedAt);

  return (
    <div className="overflow-hidden rounded-lg border border-line bg-obsidian">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-slate-raised/70 px-3 py-2">
        <div className="flex items-center gap-2">
          <span aria-hidden className="flex gap-1">
            <span className="h-2.5 w-2.5 rounded-full bg-ghost/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-dormant/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-emerald-bio/70" />
          </span>
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted">foundry sandbox</span>
          {live ? (
            <span className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.2em] text-alive-text">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-emerald-bio motion-safe:animate-vital-pulse" />
              {follow ? "live" : "paused"}
            </span>
          ) : (
            <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">recorded</span>
          )}
        </div>
        <div role="group" aria-label="Filter the terminal" className="flex flex-wrap items-center gap-1.5">
          <Chip pressed={stage === "ALL"} onClick={() => setStage("ALL")}>
            All
          </Chip>
          {PIPELINE_STAGES.map((s) => (
            <Chip key={s} pressed={stage === s} onClick={() => setStage(s)} label={`${s.toLowerCase()} stage, ${counts.byStage.get(s) ?? 0} lines`}>
              {STAGE_SHORT[s]} <span className="text-ink-muted">{counts.byStage.get(s) ?? 0}</span>
            </Chip>
          ))}
          <span aria-hidden className="mx-1 h-4 w-px bg-line" />
          <Chip pressed={stdout} onClick={() => setStdout((v) => !v)} label={`stdout, ${counts.streamOut} lines`}>
            stdout
          </Chip>
          <Chip pressed={stderr} onClick={() => setStderr((v) => !v)} label={`stderr, ${counts.streamErr} lines`}>
            <span className="text-dormant-text">stderr</span>
          </Chip>
        </div>
      </div>

      <div className="relative">
        <div
          ref={scroller}
          onScroll={onScroll}
          tabIndex={0}
          role="log"
          aria-live={live ? "polite" : "off"}
          aria-relevant="additions"
          aria-label="Foundry sandbox terminal"
          className="scanlines h-[26rem] overflow-y-auto py-2 font-mono text-xs tabular-nums focus-visible:outline-offset-[-2px]"
        >
          {(buffer.dropped > 0 || hidden > 0) && (
            <p className="px-3 pb-1 text-[11px] text-ink-muted">
              {buffer.dropped > 0 && `${buffer.dropped.toLocaleString()} older lines left the buffer. `}
              {hidden > 0 && `${hidden.toLocaleString()} earlier lines are not rendered; the newest ${MAX_TERMINAL_DOM_ROWS.toLocaleString()} are shown.`}
            </p>
          )}
          {visible.length === 0 ? (
            <p className="px-3 text-ink-muted">
              {buffer.lines.length === 0 ? (live ? "Waiting for the sandbox…" : "No sandbox output was received.") : "No lines match the filter."}
            </p>
          ) : (
            visible.map((line) => <Row key={line.id} line={line} />)
          )}
          {live && (
            <span aria-hidden className="block px-3 pl-12 text-alive-text motion-safe:animate-flatline-blink">
              ▌
            </span>
          )}
        </div>
        {!follow && (
          <button
            type="button"
            onClick={jumpToLive}
            className="absolute bottom-3 right-4 rounded-full border border-emerald-bio/70 bg-obsidian/95 px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-alive-text shadow-glow-alive hover:bg-emerald-bio/15"
          >
            Jump to {live ? "live" : "end"}
            {unseen > 0 && ` · ${unseen} new`} <span aria-hidden>↓</span>
          </button>
        )}
      </div>
      <p className="border-t border-line px-3 py-1.5 font-mono text-[10px] text-ink-muted">
        {filtered.length.toLocaleString()} of {buffer.lines.length.toLocaleString()} lines shown · stdout white, stderr amber · from SANDBOX_LOG and
        Foundry telemetry
      </p>
    </div>
  );
}
