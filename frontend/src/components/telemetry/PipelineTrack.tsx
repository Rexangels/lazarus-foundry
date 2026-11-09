import { cx } from "@/components/ui/cx";
import { PIPELINE_STEPS, type PipelineStepId, type StepState } from "@/lib/progress";

const STATE_STYLE: Record<StepState, { ring: string; text: string; label: string }> = {
  pending: { ring: "border-line text-ink-muted", text: "text-ink-muted", label: "pending" },
  active: {
    ring: "border-emerald-bio text-emerald-bio shadow-glow-alive motion-safe:animate-vital-pulse",
    text: "text-ink",
    label: "in progress",
  },
  done: { ring: "border-emerald-bio bg-emerald-bio text-obsidian", text: "text-ink", label: "done" },
  failed: { ring: "border-ghost bg-ghost/15 text-ghost-text", text: "text-ghost-text", label: "failed" },
};

/**
 * The five-step pipeline. Without `states` it is the static explainer on the landing page;
 * with `states` it shows live progress.
 */
export function PipelineTrack({
  states,
  showDescriptions = false,
  className,
}: {
  states?: Record<PipelineStepId, StepState>;
  showDescriptions?: boolean;
  className?: string;
}) {
  return (
    <ol className={cx("grid gap-4 sm:grid-cols-5 sm:gap-0", className)} aria-label="Resurrection pipeline">
      {PIPELINE_STEPS.map((step, index) => {
        const state = states?.[step.id];
        const style = state ? STATE_STYLE[state] : null;
        const glyph = state === "done" ? "✓" : state === "failed" ? "✕" : String(step.index).padStart(2, "0");
        return (
          <li key={step.id} className="relative flex gap-3 sm:flex-col sm:items-center sm:gap-2 sm:px-2 sm:text-center">
            {index < PIPELINE_STEPS.length - 1 && (
              <span
                aria-hidden
                className={cx(
                  "absolute hidden h-px sm:left-[calc(50%+22px)] sm:right-[calc(-50%+22px)] sm:top-5 sm:block",
                  state === "done" ? "bg-emerald-bio/70" : "bg-line",
                )}
              />
            )}
            <span
              className={cx(
                "relative z-10 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border-2 bg-obsidian font-mono text-xs font-semibold",
                style ? style.ring : "border-emerald-bio/50 text-emerald-bio",
              )}
            >
              {glyph}
            </span>
            <div className="min-w-0">
              <p className={cx("font-display text-sm font-semibold uppercase tracking-[0.18em]", style ? style.text : "text-ink")}>
                {step.label}
                {style && <span className="sr-only">: {style.label}</span>}
              </p>
              <p className="font-mono text-[10px] text-ink-muted">{step.system}</p>
              {showDescriptions && <p className="mt-1.5 text-sm leading-snug text-ink-muted">{step.description}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
