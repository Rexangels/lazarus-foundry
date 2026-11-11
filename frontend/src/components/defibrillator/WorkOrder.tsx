import { InlineMarkdown } from "@/components/ui/Markdown";
import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import {
  DEFERRAL_REASON_LABEL,
  deferredItems,
  formatDecimal,
  inScopeFeatures,
  planItems,
  type PlanItemView,
} from "@/lib/defibrillator";
import type { ColorToken, StrategyOut } from "@/lib/types";

const STAGE_TOKEN: Record<string, ColorToken> = {
  ENVIRONMENT: "dormant",
  ARCHITECTURE: "conflict",
  ENGINEERING: "alive",
};

function Files({ label, files, mark }: { label: string; files: string[]; mark: string }) {
  if (files.length === 0) return null;
  return (
    <div>
      <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">{label}</dt>
      <dd>
        <ul className="mt-0.5 space-y-0.5 font-mono text-[11px] text-ink">
          {files.map((file) => (
            <li key={file} className="break-all">
              <span aria-hidden className="mr-1 text-ink-muted">{mark}</span>
              {file}
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

function PlanItemRow({ item }: { item: PlanItemView }) {
  return (
    <li className="rounded-md border border-line bg-obsidian/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-ink-muted">{item.id}</span>
        <TokenBadge token={STAGE_TOKEN[item.stage] ?? "neutral"} label={item.stage} appearance="outlined" size="sm" title="Foundry stage that executes it" />
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">{item.kind.replace(/_/g, " ")}</span>
        <span className="ml-auto font-mono text-[11px] tabular-nums text-ink-muted">
          effort {item.effort} · feasibility <span className="text-ink">{formatDecimal(item.feasibility)}</span>
        </span>
      </div>
      <p className="mt-1.5 text-sm font-medium text-ink">
        <InlineMarkdown text={item.title} />
      </p>
      {item.rationale && (
        <p className="mt-1 text-xs text-ink-muted">
          <InlineMarkdown text={item.rationale} />
        </p>
      )}
      <details className="group mt-2">
        <summary className="cursor-pointer select-none font-mono text-[11px] text-ink-muted hover:text-ink">
          Files, tests and constraints
          {item.evidenceCount > 0 && ` · ${item.evidenceCount} evidence ref(s)`}
        </summary>
        <dl className="mt-2 grid gap-2 sm:grid-cols-2">
          <Files label="Modify" files={item.filesToModify} mark="~" />
          <Files label="Create" files={item.filesToCreate} mark="+" />
          <Files label="Target tests (never edited)" files={item.targetTests} mark="✓" />
          <Files label="Copies" files={item.copies.map((c) => `${c.source} → ${c.destination}`)} mark="⧉" />
          {item.branch && (
            <div>
              <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Branch</dt>
              <dd className="font-mono text-[11px] text-dormant-text">{item.branch}</dd>
            </div>
          )}
          {item.dependency && (
            <div>
              <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Dependency pin</dt>
              <dd className="font-mono text-[11px] text-ink">
                {item.dependency.name}
                {item.dependency.currentSpecifier} · {item.dependency.manifest}
                {item.dependency.line !== null && `:${item.dependency.line}`}
                {item.requiresResolution && <span className="text-dormant-text"> · target resolved in the sandbox</span>}
              </dd>
            </div>
          )}
          {item.dependsOn.length > 0 && (
            <div>
              <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Depends on</dt>
              <dd className="font-mono text-[11px] text-ink">{item.dependsOn.join(", ")}</dd>
            </div>
          )}
          {item.factors.length > 0 && (
            <div className="sm:col-span-2">
              <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Feasibility factors (product)</dt>
              <dd className="font-mono text-[11px] text-ink">
                {item.factors.map((f) => `${f.name} ${formatDecimal(f.value)}`).join(" × ")} = {formatDecimal(item.feasibility)}
              </dd>
            </div>
          )}
          {[...item.constraints, ...item.openMarkers.map((m) => `Open marker: ${m}`), ...item.externalRequirements.map((e) => `Needs ${e}`)].length > 0 && (
            <div className="sm:col-span-2">
              <dt className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">Constraints and markers</dt>
              <dd>
                <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-xs text-ink-muted marker:text-ink-muted/60">
                  {item.constraints.map((c) => (
                    <li key={`c-${c}`}>{c}</li>
                  ))}
                  {item.openMarkers.map((m) => (
                    <li key={`m-${m}`} className="font-mono text-[11px] text-dormant-text">{m}</li>
                  ))}
                  {item.externalRequirements.map((e) => (
                    <li key={`e-${e}`}>Needs {e}</li>
                  ))}
                </ul>
              </dd>
            </div>
          )}
        </dl>
      </details>
    </li>
  );
}

/** The strategy's work order: in-scope features, plan items in execution order, and the deferrals. */
export function WorkOrder({ strategy }: { strategy: StrategyOut }) {
  const items = planItems(strategy);
  const deferred = deferredItems(strategy);
  const scope = inScopeFeatures(strategy);
  return (
    <div className="space-y-5">
      {scope.length > 0 && (
        <div>
          <h4 className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">In scope · {scope.length} feature(s)</h4>
          <ul className="mt-2 flex flex-wrap gap-2">
            {scope.map((f) => (
              <li key={f.featureId ?? f.title} className="rounded border border-alive/40 bg-alive/5 px-2 py-1 text-xs text-ink">
                {f.title}
                <span className="ml-2 font-mono text-[10px] tabular-nums text-alive-text">{formatDecimal(f.feasibility)}</span>
                {f.partial && <span className="ml-1 font-mono text-[10px] text-dormant-text">partial</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h4 className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">
          Plan · {items.length} item(s) in execution order · {strategy.action_plan.files.length} file(s)
          {strategy.action_plan.extract_root && ` · into ${strategy.action_plan.extract_root}/`}
        </h4>
        {items.length === 0 ? (
          <p className="mt-2 rounded border border-ghost/40 bg-ghost/10 px-3 py-2 text-sm text-ink">
            No plan items: there is nothing for the Foundry to execute.
          </p>
        ) : (
          <ol className="mt-2 space-y-2">
            {items.map((item) => (
              <PlanItemRow key={item.id} item={item} />
            ))}
          </ol>
        )}
      </div>

      <div>
        <h4 className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">Deferred · {deferred.length}</h4>
        {deferred.length === 0 ? (
          <p className="mt-2 text-xs text-ink-muted">Nothing is deferred.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {deferred.map((d) => (
              <li key={`${d.featureId}-${d.reason}`} className={cx("rounded-md border border-dashed p-3", d.partial ? "border-dormant/50" : "border-line")}>
                <div className="flex flex-wrap items-center gap-2">
                  <span aria-hidden className="text-ink-muted">○</span>
                  <span className="text-sm text-ink">{d.title}</span>
                  <TokenBadge
                    token={d.partial ? "dormant" : "neutral"}
                    label={DEFERRAL_REASON_LABEL[d.reason] ?? d.reason}
                    appearance="outlined"
                    dashed
                    size="sm"
                    title={d.reason}
                  />
                  {d.partial && <span className="font-mono text-[10px] text-dormant-text">partial: the rest is in scope</span>}
                </div>
                {d.detail && <p className="mt-1 text-xs text-ink-muted">{d.detail}</p>}
                {d.wouldTouch.length > 0 && (
                  <p className="mt-1 break-all font-mono text-[10px] text-ink-muted">would touch {d.wouldTouch.join(", ")}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
