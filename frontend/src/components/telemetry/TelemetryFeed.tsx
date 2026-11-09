"use client";

import { useEffect, useRef } from "react";

import { cx } from "@/components/ui/cx";
import { describeFrame } from "@/lib/progress";
import type { TelemetryEventType, TelemetryFrame } from "@/lib/types";

const EVENT_TONE: Partial<Record<TelemetryEventType, string>> = {
  BRANCH_DISCOVERED: "text-amber-pulse",
  GENOME_NODE_CREATED: "text-emerald-bio",
  CONFLICT_DETECTED: "text-conflict-text",
  GENOME_COMPLETE: "text-emerald-bio",
  INGESTION_COMPLETE: "text-emerald-bio",
  STRATEGY_GENERATED: "text-conflict-text",
  TEST_PASSED: "text-emerald-bio",
  TEST_FAILED: "text-ghost-text",
  ERROR: "text-ghost-text",
};

function clock(timestamp: string): string {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "--:--:--" : date.toLocaleTimeString([], { hour12: false });
}

/**
 * A forensic, monospace event log. `role="log"` makes screen readers announce new lines politely;
 * the region is focusable so keyboard users can scroll it.
 */
export function TelemetryFeed({
  frames,
  emptyText = "Waiting for the first telemetry frame…",
  className,
  maxLines = 200,
}: {
  frames: readonly TelemetryFrame[];
  emptyText?: string;
  className?: string;
  maxLines?: number;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const visible = frames.slice(-maxLines);

  useEffect(() => {
    const element = scroller.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [visible.length]);

  return (
    <div className={cx("relative", className)}>
      <div
        ref={scroller}
        tabIndex={0}
        role="log"
        aria-label="Telemetry events"
        className="h-full max-h-80 overflow-y-auto rounded-md border border-line bg-obsidian/80 p-3 font-mono text-xs leading-relaxed"
      >
        {visible.length === 0 ? (
          <p className="text-ink-muted">
            <span aria-hidden className="mr-2 text-emerald-bio">▍</span>
            {emptyText}
          </p>
        ) : (
          <ol className="space-y-0.5">
            {visible.map((frame) => (
              <li key={frame.event_id} className="flex gap-3 motion-safe:animate-feed-in">
                <time dateTime={frame.timestamp} className="shrink-0 tabular-nums text-ink-muted">
                  {clock(frame.timestamp)}
                </time>
                <span className={cx("w-44 shrink-0 truncate", EVENT_TONE[frame.event_type] ?? "text-ink-muted")}>
                  {frame.event_type}
                </span>
                <span className="min-w-0 break-words text-ink">{describeFrame(frame)}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
