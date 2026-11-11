import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { healingLabel, type PipelineState, type PipelineStage, type StageStatus } from "@/lib/heartbeat";
import type { ColorToken } from "@/lib/types";

/** What each agent does (design.md, apps.foundry). */
const STAGE_ROLE: Record<PipelineStage, string> = {
  ENVIRONMENT: "Sandbox, interpreter and dependencies; baseline pytest",
  ARCHITECTURE: "Revival branch and the abandoned-branch merges",
  ENGINEERING: "Code synthesis and test updates; self-heals on failure",
  VERIFICATION: "Full pytest suite on the revival branch",
};

const STAGE_TITLE: Record<PipelineStage, string> = {
  ENVIRONMENT: "Environment",
  ARCHITECTURE: "Architecture",
  ENGINEERING: "Engineering",
  VERIFICATION: "Verification",
};

type StepStatus = StageStatus | "reborn" | "verified";

const STATUS_STYLE: Record<StepStatus, { glyph: string; word: string; token: ColorToken | null; ring: string; text: string }> = {
  pending: { glyph: "○", word: "Waiting", token: null, ring: "border-line text-ink-muted", text: "text-ink-muted" },
  running: { glyph: "◐", word: "Running", token: "conflict", ring: "border-conflict text-conflict-text shadow-glow-conflict", text: "text-conflict-text" },
  healing: { glyph: "↻", word: "Self-healing", token: "dormant", ring: "border-dormant text-dormant-text shadow-glow-dormant", text: "text-dormant-text" },
  done: { glyph: "✓", word: "Done", token: "alive", ring: "border-emerald-bio text-alive-text", text: "text-alive-text" },
  failed: { glyph: "✕", word: "Failed", token: "ghost", ring: "border-ghost text-ghost-text shadow-glow-ghost", text: "text-ghost-text" },
  verified: { glyph: "◐", word: "Verified, writing the rebirth", token: "alive", ring: "border-emerald-bio text-alive-text", text: "text-alive-text" },
  reborn: { glyph: "♥", word: "Reborn", token: "alive", ring: "border-emerald-bio bg-emerald-bio/15 text-alive-text shadow-glow-alive", text: "text-alive-text" },
};

const OUTCOME_LABEL: Record<PipelineState["outcome"], { label: string; token: ColorToken; glyph: string }> = {
  idle: { label: "Idle", token: "neutral", glyph: "○" },
  queued: { label: "Queued", token: "dormant", glyph: "◔" },
  running: { label: "Running", token: "conflict", glyph: "◐" },
  verified: { label: "Verified", token: "alive", glyph: "✓" },
  reborn: { label: "Reborn", token: "alive", glyph: "♥" },
  failed: { label: "Failed", token: "ghost", glyph: "✕" },
};

const SCOPE_LABEL: Record<NonNullable<PipelineState["failure"]>["scope"], string> = {
  FOUNDRY: "Foundry agent failure",
  REBIRTH: "Verification passed, but the rebirth could not be built",
  STATUS: "The project status reports a failure",
};

function completeStatus(pipeline: PipelineState): StepStatus {
  if (pipeline.outcome === "reborn") return "reborn";
  if (pipeline.outcome === "verified") return "verified";
  if (pipeline.outcome === "failed" && pipeline.failure?.scope === "REBIRTH") return "failed";
  return "pending";
}

function Step({
  index,
  title,
  agent,
  role,
  status,
  current,
  last,
}: {
  index: number;
  title: string;
  agent: string;
  role: string;
  status: StepStatus;
  current: boolean;
  last: boolean;
}) {
  const style = STATUS_STYLE[status];
  const active = status === "running" || status === "healing" || status === "verified";
  return (
    <li aria-current={current ? "step" : undefined} className="relative flex min-w-0 flex-1 gap-3 lg:flex-col lg:gap-2">
      {!last && (
        <span
          aria-hidden
          className={cx(
            "absolute left-4 top-9 h-[calc(100%-1.5rem)] w-px lg:left-9 lg:top-4 lg:h-px lg:w-[calc(100%-2.25rem)]",
            status === "done" ? "bg-emerald-bio/70" : "bg-line",
          )}
        />
      )}
      <span
        aria-hidden
        className={cx(
          "relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2 bg-obsidian font-mono text-sm",
          style.ring,
          active && "motion-safe:animate-vital-pulse",
        )}
      >
        {status === "pending" ? `0${index}` : style.glyph}
      </span>
      <span className="min-w-0 pb-4 lg:pb-0 lg:pr-2">
        <span className="block font-display text-sm font-semibold uppercase tracking-[0.16em] text-ink">{title}</span>
        <span className="block font-mono text-[11px] text-ink-muted">{agent}</span>
        <span className={cx("mt-1 block font-mono text-[11px] uppercase tracking-wider", style.text)}>
          <span aria-hidden className="mr-1">{style.glyph}</span>
          {style.word}
        </span>
        <span className="mt-1 block text-xs text-ink-muted">{role}</span>
      </span>
    </li>
  );
}

/**
 * The Foundry's four agents (Environment → Architecture → Engineering → Verification → Complete)
 * from `pipelineState`: each stage's status, the self-healing marker and the failure with its code.
 */
export function PipelineProgress({ pipeline }: { pipeline: PipelineState }) {
  const percent = Math.round(pipeline.progress * 100);
  const outcome = OUTCOME_LABEL[pipeline.outcome];
  const healing = healingLabel(pipeline);
  const failure = pipeline.failure;
  const complete = completeStatus(pipeline);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-[11px] text-ink-muted">
        <TokenBadge token={outcome.token} glyph={outcome.glyph} label={outcome.label} pulse={pipeline.outcome === "running"} />
        {pipeline.foundryRunId && <span title={pipeline.foundryRunId}>run {pipeline.foundryRunId.slice(0, 8)}</span>}
        <span>verification attempts {pipeline.verificationAttempts}</span>
        {pipeline.iterationsUsed !== null && (
          <span>
            self-healing used {pipeline.iterationsUsed}
            {pipeline.maxIterations !== null ? ` of ${pipeline.maxIterations}` : ""}
          </span>
        )}
      </div>

      <div
        role="progressbar"
        aria-label="Foundry progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`${percent} percent, ${outcome.label}`}
        className="h-1.5 overflow-hidden rounded-full bg-line"
      >
        <div
          className={cx(
            "h-full rounded-full transition-[width] duration-700",
            pipeline.outcome === "failed" ? "bg-ghost" : pipeline.healing && pipeline.outcome === "running" ? "bg-dormant" : "bg-emerald-bio",
          )}
          style={{ width: `${percent}%` }}
        />
      </div>

      <ol aria-label="Foundry agents" className="flex flex-col lg:flex-row lg:gap-2">
        {pipeline.stages.map((stage, i) => (
          <Step
            key={stage.stage}
            index={i + 1}
            title={STAGE_TITLE[stage.stage]}
            agent={stage.agent}
            role={STAGE_ROLE[stage.stage]}
            status={stage.status}
            current={pipeline.current === stage.stage}
            last={false}
          />
        ))}
        <Step
          index={5}
          title="Complete"
          agent="Rebirth artifact"
          role="Commit, unified diff, PR and Continuity Memory"
          status={complete}
          current={complete === "verified" || complete === "reborn"}
          last
        />
      </ol>

      {healing && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-dashed border-dormant/60 bg-dormant/10 px-3 py-2 text-sm">
          <span aria-hidden className="font-mono text-dormant-text motion-safe:animate-vital-pulse">↻</span>
          <span className="font-display text-xs font-semibold uppercase tracking-[0.16em] text-dormant-text">{healing}</span>
          {pipeline.healing?.repairStatus && <span className="font-mono text-[11px] text-ink-muted">repair {pipeline.healing.repairStatus}</span>}
          {pipeline.healing && pipeline.healing.files.length > 0 && (
            <span className="font-mono text-[11px] text-ink">{pipeline.healing.files.join(", ")}</span>
          )}
        </div>
      )}

      {failure && (
        <div role="alert" className="space-y-1 rounded-md border border-ghost/60 bg-ghost/10 px-3 py-2">
          <p className="flex flex-wrap items-center gap-2 font-display text-xs font-semibold uppercase tracking-[0.16em] text-ghost-text">
            <span aria-hidden>✕</span>
            {SCOPE_LABEL[failure.scope]}
            {failure.stage && <span className="font-mono normal-case tracking-normal">at {STAGE_TITLE[failure.stage]}</span>}
            {failure.code && (
              <code className="rounded bg-obsidian px-1.5 py-px font-mono text-[11px] normal-case tracking-normal text-ghost-text ring-1 ring-ghost/40">
                {failure.code}
              </code>
            )}
          </p>
          {failure.detail ? (
            <p className="whitespace-pre-wrap break-words font-mono text-xs text-ink">{failure.detail}</p>
          ) : (
            <p className="text-xs text-ink-muted">No detail was reported.</p>
          )}
        </div>
      )}

      {pipeline.fromStatusFallback && (
        <p className="text-xs text-ink-muted">
          Some telemetry frames were not received (the page opened after they were sent); the project status filled in the stages.
        </p>
      )}
    </div>
  );
}
