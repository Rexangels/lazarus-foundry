"use client";

import { useId, useRef, useState, type KeyboardEvent } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { Panel } from "@/components/ui/Panel";
import { TokenBadge } from "@/components/ui/StatusBadge";
import { Markdown } from "@/components/ui/Markdown";
import { cx } from "@/components/ui/cx";
import { SPEC_DOCS, documentStats, specPath, type SpecDocKey } from "@/lib/defibrillator";
import type { StrategyOut } from "@/lib/types";

const MODE_TOKEN: Record<string, "alive" | "conflict" | "dormant" | "neutral"> = {
  DETERMINISTIC: "alive",
  MODEL: "conflict",
  MODEL_FALLBACK: "dormant",
};

/** The `.kiro` spec bundle the strategy would write: requirements.md, design.md and tasks.md as tabs. */
export function SpecPreview({ strategy }: { strategy: StrategyOut }) {
  const { api } = useProjectSession();
  const [active, setActive] = useState<SpecDocKey>("requirements_md");
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  const baseId = useId().replace(/:/g, "");
  const spec = strategy.spec;
  const doc = SPEC_DOCS.find((d) => d.key === active) ?? SPEC_DOCS[0];

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = SPEC_DOCS.findIndex((d) => d.key === active);
    const last = SPEC_DOCS.length - 1;
    const next =
      event.key === "ArrowRight" ? (index === last ? 0 : index + 1)
      : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (next === null) return;
    event.preventDefault();
    const key = SPEC_DOCS[next]?.key;
    if (key) {
      setActive(key);
      tabs.current[key]?.focus();
    }
  };

  return (
    <Panel
      eyebrow=".kiro spec preview"
      title={<span className="break-all font-mono text-base">{spec.spec_name || "(unnamed spec)"}</span>}
      headingId={`${baseId}-spec-heading`}
      tone="conflict"
      actions={<TokenBadge token={MODE_TOKEN[spec.mode] ?? "neutral"} label={spec.mode || "unknown mode"} appearance="outlined" size="sm" title="How the spec prose was written" />}
    >
      <p className="font-mono text-xs text-ink-muted">
        <span className="text-ink">{specPath(spec.spec_name)}</span>
        {spec.generated_at && <> · generated {spec.generated_at}</>}
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        The Foundry executes this bundle and the revived repository inherits it. Footnotes cite the evidence behind every
        requirement and task.
      </p>

      <div role="tablist" aria-label="Spec documents" className="mt-4 flex gap-1 border-b border-line" onKeyDown={onKeyDown}>
        {SPEC_DOCS.map((d) => {
          const selected = d.key === active;
          const stats = documentStats(spec[d.key]);
          return (
            <button
              key={d.key}
              ref={(el) => {
                tabs.current[d.key] = el;
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${d.key}`}
              aria-selected={selected}
              aria-controls={`${baseId}-panel-${d.key}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(d.key)}
              className={cx(
                "-mb-px rounded-t-md border border-b-0 px-3 py-2 text-left font-mono text-xs transition-colors",
                selected ? "border-line bg-slate-raised text-ink" : "border-transparent text-ink-muted hover:text-ink",
              )}
            >
              <span className={selected ? "text-conflict-text" : undefined}>{d.file}</span>
              <span className="ml-2 text-[10px] text-ink-muted">{stats.lines} lines</span>
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`${baseId}-panel-${doc.key}`}
        aria-labelledby={`${baseId}-tab-${doc.key}`}
        tabIndex={0}
        className="max-h-[36rem] overflow-y-auto rounded-b-md border border-t-0 border-line bg-obsidian/60 p-4"
      >
        {spec[doc.key] ? (
          <Markdown source={spec[doc.key]} idPrefix={`${baseId}-${doc.key}`} evidence={spec.evidence_index} />
        ) : (
          <p className="text-sm text-ink-muted">This document is empty.</p>
        )}
      </div>
      {api.mode === "http" && (
        <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
          {SPEC_DOCS.map((d) => (
            <a
              key={d.key}
              href={`${api.specDocumentUrl(spec.document_urls[d.key])}?download=true`}
              className="text-ink-muted underline decoration-line underline-offset-4 hover:text-ink"
            >
              Download {d.file}
            </a>
          ))}
        </p>
      )}
    </Panel>
  );
}
