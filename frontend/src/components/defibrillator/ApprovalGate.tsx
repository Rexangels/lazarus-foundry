"use client";

import { useEffect, useId, useRef, useState, type Dispatch, type KeyboardEvent } from "react";

import { useProjectSession } from "@/components/telemetry/ProjectSession";
import { Button, ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { Panel } from "@/components/ui/Panel";
import { ProjectStatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import {
  SPEC_DOCS,
  describeApprovalFailure,
  documentStats,
  formatScore,
  readScoreBreakdown,
  shortToken,
  specPath,
  type GateAction,
  type GateAvailability,
  type GatePhase,
} from "@/lib/defibrillator";
import type { ApprovalOut, StrategyOut } from "@/lib/types";

const APPROVER_MAX = 255;

function approverProblem(name: string, required: boolean): string | null {
  const value = name.trim();
  if (!value) return required ? "The backend requires an approver name." : null;
  if (value.length > APPROVER_MAX) return `At most ${APPROVER_MAX} characters.`;
  // Printable characters only (the backend's str.isprintable check).
  if (/[\u0000-\u001f\u007f-\u009f]/.test(value)) return "Use printable characters only.";
  return null;
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : `${date.toLocaleString()} (${iso})`;
}

/** What the approval locks in: exactly the reviewed spec bundle, plan and scores. */
function LockManifest({ strategy }: { strategy: StrategyOut }) {
  const view = readScoreBreakdown(strategy);
  return (
    <dl className="grid gap-3 text-xs sm:grid-cols-2 xl:grid-cols-4">
      <div className="rounded-md border border-line bg-obsidian/60 p-3">
        <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Strategy</dt>
        <dd className="mt-1 text-ink">
          <span className="font-mono text-alive-text">{strategy.strategy_type}</span> · Score{" "}
          <span className="font-mono">{formatScore(view.score, view.scoreDecimals)}</span>
          <p className="mt-0.5 text-ink-muted">{strategy.title}</p>
        </dd>
      </div>
      <div className="rounded-md border border-line bg-obsidian/60 p-3">
        <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Work order</dt>
        <dd className="mt-1 text-ink">
          {strategy.action_plan.items.length} plan item(s), {strategy.action_plan.files.length} file(s),{" "}
          {strategy.action_plan.deferred.length} deferred
          <p className="mt-0.5 text-ink-muted">Executed in order by the Environment, Architecture and Engineering agents.</p>
        </dd>
      </div>
      <div className="rounded-md border border-line bg-obsidian/60 p-3">
        <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Spec bundle</dt>
        <dd className="mt-1 font-mono text-ink">
          <span className="break-all">{specPath(strategy.spec.spec_name)}</span>
          <ul className="mt-0.5 text-[11px] text-ink-muted">
            {SPEC_DOCS.map((d) => (
              <li key={d.key}>
                {d.file} · {documentStats(strategy.spec[d.key]).lines} lines
              </li>
            ))}
          </ul>
        </dd>
      </div>
      <div className="rounded-md border border-line bg-obsidian/60 p-3">
        <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Approval token</dt>
        <dd className="mt-1">
          <code className="break-all font-mono text-conflict-text" title={strategy.approval.approval_token}>
            sha256 {shortToken(strategy.approval.approval_token)}
          </code>
          <p className="mt-0.5 text-ink-muted">
            Hash of exactly this plan, these four scores and these three documents. If any of them changed since this
            page loaded, the backend refuses the approval.
          </p>
        </dd>
      </div>
    </dl>
  );
}

/** The approved state: who approved what, and the way to The Heartbeat. */
function LockedIn({ strategy, result, projectId }: { strategy: StrategyOut; result: ApprovalOut | null; projectId: string }) {
  const approval = strategy.approval;
  return (
    <Panel tone="alive" glow eyebrow="Human Approval Gate · locked" title="Shock delivered. The spec is locked in." headingId="gate-locked-heading">
      <div className="flex flex-wrap items-center justify-between gap-6">
        <div className="min-w-0 flex-1 space-y-3">
          <dl className="grid gap-x-6 gap-y-2 font-mono text-xs sm:grid-cols-2">
            <div>
              <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">Approved strategy</dt>
              <dd className="text-ink">
                {strategy.strategy_type} · {strategy.spec.spec_name}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">approved_by</dt>
              <dd className="text-ink">
                {approval.approved_by || <span className="text-ink-muted">unnamed (no X-Lazarus-Approver header)</span>}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">approved_at</dt>
              <dd className="text-ink">{formatWhen(approval.approved_at)}</dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">Content</dt>
              <dd className={approval.approval_intact === false ? "text-ghost-text" : "text-alive-text"}>
                {approval.approval_intact === false
                  ? "⚠ altered after approval (APPROVED_CONTENT_CHANGED): the Foundry will refuse it"
                  : `✓ intact · sha256 ${shortToken(approval.approval_token)}`}
              </dd>
            </div>
            {result?.foundry_run_id && (
              <div>
                <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">Foundry run</dt>
                <dd className="break-all text-ink">{result.foundry_run_id} · queued at ENVIRONMENT</dd>
              </div>
            )}
            {result && (
              <div>
                <dt className="text-[10px] uppercase tracking-[0.2em] text-ink-muted">Project</dt>
                <dd>
                  <ProjectStatusBadge status={result.project_status} size="sm" />
                  {result.already_approved && <span className="ml-2 text-ink-muted">already approved; nothing changed</span>}
                </dd>
              </div>
            )}
          </dl>
          <HeartbeatLine state="pulse" className="h-10 max-w-lg" label="A heartbeat: the Foundry is starting" />
        </div>
        <ButtonLink href={`/foundry/${projectId}/heartbeat`} variant="charge" size="lg">
          Watch the Heartbeat <span aria-hidden>→</span>
        </ButtonLink>
      </div>
    </Panel>
  );
}

export interface ApprovalGateProps {
  strategy: StrategyOut;
  availability: GateAvailability;
  gate: GatePhase;
  dispatch: Dispatch<GateAction>;
  lastResult: ApprovalOut | null;
  onApproved: (result: ApprovalOut) => void;
  onReload: () => void;
  onSelect: (id: string) => void;
}

/**
 * The Human Approval Gate: a deliberate two-step interaction (charge, then shock) that sends
 * `POST .../approve {"approval_token"}` (plus `X-Lazarus-Approver` when a name is given) and handles
 * every answer: 200 (also an idempotent repeat), 403 APPROVER_REQUIRED, 404, the 409 codes, 422,
 * and an unreachable backend.
 */
export function ApprovalGate({ strategy, availability, gate, dispatch, lastResult, onApproved, onReload, onSelect }: ApprovalGateProps) {
  const { api, projectId, refresh } = useProjectSession();
  const [approver, setApprover] = useState("");
  const [approverRequired, setApproverRequired] = useState(false);
  const chargeRef = useRef<HTMLButtonElement>(null);
  const confirmHeadingRef = useRef<HTMLHeadingElement>(null);
  const failureRef = useRef<HTMLHeadingElement>(null);
  const approverRef = useRef<HTMLInputElement>(null);
  const baseId = useId().replace(/:/g, "");
  const problem = approverProblem(approver, approverRequired);

  const phase = gate.phase;
  const previousPhase = useRef(phase);
  useEffect(() => {
    const before = previousPhase.current;
    previousPhase.current = phase;
    if (phase === "armed" && before !== "armed") confirmHeadingRef.current?.focus();
    else if (phase === "idle" && before === "armed") chargeRef.current?.focus();
    else if (phase === "failed") failureRef.current?.focus();
  }, [phase]);

  if (availability.kind === "approved") {
    return <LockedIn strategy={strategy} result={lastResult?.strategy.id === strategy.id ? lastResult : null} projectId={projectId} />;
  }

  const shock = async () => {
    if (problem) {
      approverRef.current?.focus();
      return;
    }
    dispatch({ type: "shock" });
    try {
      const name = approver.trim();
      const result = await api.approveStrategy(projectId, strategy.id, strategy.approval.approval_token, name ? { approver: name } : {});
      dispatch({ type: "success", result });
      onApproved(result);
      refresh();
    } catch (cause) {
      const failure = describeApprovalFailure(cause);
      if (failure.recovery === "approver") setApproverRequired(true);
      dispatch({ type: "failure", failure });
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && phase === "armed") {
      event.stopPropagation();
      dispatch({ type: "disarm" });
    }
  };

  const blocked =
    availability.kind === "locked" ? (
      <>
        <p className="text-ink">
          <span aria-hidden>🔒 </span>
          {availability.approvedType ?? "Another strategy"} is already approved. A project has at most one approved strategy,
          so this one can no longer be approved.
        </p>
        <Button variant="secondary" size="sm" onClick={() => onSelect(availability.approvedId)}>
          View the approved strategy
        </Button>
      </>
    ) : availability.kind === "not_ready" ? (
      <p className="flex flex-wrap items-center gap-2 text-ink">
        <ProjectStatusBadge status={availability.status} size="sm" />
        Approval opens only while the project is AUTOPSY_COMPLETE.
      </p>
    ) : availability.kind === "not_actionable" ? (
      <p className="text-ink">
        This strategy has no plan items, so there is nothing for the Foundry to execute. It cannot be approved.
      </p>
    ) : availability.kind === "invalid_token" ? (
      <>
        <p className="text-ink">The backend sent no valid approval token for this strategy.</p>
        <Button variant="secondary" size="sm" onClick={onReload}>
          Reload the strategies
        </Button>
      </>
    ) : null;

  return (
    <Panel
      tone={phase === "armed" || phase === "shocking" ? "dormant" : "alive"}
      glow={phase === "armed" || phase === "shocking"}
      eyebrow="Human Approval Gate"
      title={`Approve ${strategy.strategy_type} and start the Foundry`}
      headingId={`${baseId}-gate-heading`}
    >
      <div className="space-y-4" onKeyDown={onKeyDown}>
        <p className="max-w-3xl text-sm text-ink-muted">
          Nothing changes in the repository until a human approves one strategy. Approval locks in exactly the work order
          and the <span className="font-mono text-ink">.kiro</span> spec bundle reviewed above, moves the project to{" "}
          <span className="font-mono text-ink">STRATEGY_APPROVED</span> and queues the Foundry at the{" "}
          <span className="font-mono text-ink">ENVIRONMENT</span> stage.
        </p>
        <LockManifest strategy={strategy} />

        {blocked ? (
          <div className="space-y-3 rounded-md border border-line bg-obsidian/60 p-4">{blocked}</div>
        ) : (
          <>
            <p className="flex items-start gap-2 rounded-md border border-ghost/40 bg-ghost/10 px-3 py-2 text-sm text-ink">
              <span aria-hidden className="text-ghost-text">⚠</span>
              <span>
                <strong className="font-semibold">This cannot be undone.</strong> There is no un-approve: the approved strategy
                is frozen (never regenerated) and the other strategies are locked out.
              </span>
            </p>

            <div className="max-w-md">
              <label htmlFor={`${baseId}-approver`} className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">
                Approver name {approverRequired ? "(required)" : "(optional)"}
              </label>
              <input
                ref={approverRef}
                id={`${baseId}-approver`}
                value={approver}
                onChange={(event) => setApprover(event.target.value)}
                maxLength={APPROVER_MAX + 1}
                autoComplete="name"
                aria-invalid={problem ? true : undefined}
                aria-describedby={`${baseId}-approver-help`}
                disabled={phase === "shocking"}
                className="mt-1 h-9 w-full rounded-md border border-line bg-obsidian px-3 text-sm text-ink placeholder:text-ink-muted/60 focus-visible:outline-2 focus-visible:outline-emerald-bio"
                placeholder="e.g. Dr. Frankenstein"
              />
              <p id={`${baseId}-approver-help`} className={cx("mt-1 text-[11px]", problem ? "text-ghost-text" : "text-ink-muted")}>
                {problem ?? "Sent as X-Lazarus-Approver and stored as approved_by. It is an audit name, not authentication."}
              </p>
            </div>

            {(phase === "idle" || phase === "failed") && (
              <div className="flex flex-wrap items-center gap-4">
                <Button ref={chargeRef} variant="charge" size="lg" onClick={() => dispatch({ type: "arm" })} aria-describedby={`${baseId}-charge-help`}>
                  <span aria-hidden>⚡</span> Charge the paddles
                </Button>
                <p id={`${baseId}-charge-help`} className="max-w-sm text-xs text-ink-muted">
                  Step 1 of 2. Charging only arms the gate; nothing is sent until you confirm.
                </p>
              </div>
            )}

            {(phase === "armed" || phase === "shocking") && (
              <div
                role="group"
                aria-labelledby={`${baseId}-clear-heading`}
                className="relative overflow-hidden rounded-lg border border-dormant/60 bg-dormant/10 p-4 shadow-glow-dormant"
              >
                <h3
                  id={`${baseId}-clear-heading`}
                  ref={confirmHeadingRef}
                  tabIndex={-1}
                  className="font-display text-2xl font-bold uppercase tracking-[0.3em] text-dormant-text motion-safe:animate-vital-pulse"
                >
                  Clear!
                </h3>
                <p className="mt-1 max-w-2xl text-sm text-ink">
                  Step 2 of 2. Confirm to approve <span className="font-mono">{strategy.strategy_type}</span> with token{" "}
                  <code className="font-mono text-conflict-text">{shortToken(strategy.approval.approval_token)}</code> and start
                  the Foundry. Press Esc to stand down.
                </p>
                <div className="mt-4 flex flex-wrap items-center gap-3">
                  <Button variant="charge" size="lg" busy={phase === "shocking"} onClick={shock}>
                    {phase === "shocking" ? "Delivering the shock…" : `Shock: approve ${strategy.strategy_type}`}
                  </Button>
                  <Button variant="ghost" size="md" onClick={() => dispatch({ type: "disarm" })} disabled={phase === "shocking"}>
                    Stand down
                  </Button>
                </div>
                <p className="sr-only" aria-live="assertive">
                  {phase === "shocking" ? "Sending the approval." : "Gate armed. Confirm to approve, or press Escape to stand down."}
                </p>
              </div>
            )}

            {gate.phase === "failed" && (
              <div role="alert" className="space-y-2 rounded-md border border-ghost/50 bg-ghost/10 p-4">
                <h3 ref={failureRef} tabIndex={-1} className="font-display text-sm font-semibold uppercase tracking-[0.12em] text-ghost-text">
                  {gate.failure.title}
                  {gate.failure.code && <span className="ml-2 font-mono text-[11px] normal-case tracking-normal">{gate.failure.code}</span>}
                  {gate.failure.status !== null && <span className="ml-2 font-mono text-[11px] tracking-normal">HTTP {gate.failure.status}</span>}
                </h3>
                <p className="text-sm text-ink">{gate.failure.message}</p>
                <div className="flex flex-wrap gap-3">
                  {(gate.failure.recovery === "refresh" || gate.failure.recovery === "locked") && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        dispatch({ type: "reset" });
                        onReload();
                      }}
                    >
                      Reload the strategies
                    </Button>
                  )}
                  {gate.failure.recovery === "approver" && (
                    <Button variant="secondary" size="sm" onClick={() => approverRef.current?.focus()}>
                      Enter an approver name
                    </Button>
                  )}
                </div>
              </div>
            )}

            <p className="text-[11px] text-ink-muted">
              Local development has no authentication: anyone who can reach the API can approve. Set
              REVIVAL_APPROVAL_REQUIRES_HEADER on a hosted demo to require an approver name.
            </p>
          </>
        )}
      </div>
    </Panel>
  );
}
