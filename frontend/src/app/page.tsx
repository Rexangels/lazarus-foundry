import { ResurrectTrigger } from "@/components/landing/ResurrectTrigger";
import { AppShell } from "@/components/shell/AppShell";
import { PipelineTrack } from "@/components/telemetry/PipelineTrack";
import { Panel } from "@/components/ui/Panel";
import { EpistemicBadge, TokenBadge, VitalBadge } from "@/components/ui/StatusBadge";

const VIEWS = [
  {
    index: "01",
    name: "The Autopsy",
    tone: "text-emerald-bio",
    body: "A genome graph of every feature the project promised, each scored with a confidence C(f) and backed by cited files, lines and commits.",
  },
  {
    index: "02",
    name: "The Defibrillator",
    tone: "text-amber-pulse",
    body: "Restore, Modernize or Extract, each with a 0 to 100 feasibility score you can audit term by term, and a .kiro spec. A human picks one.",
  },
  {
    index: "03",
    name: "The Heartbeat",
    tone: "text-conflict-text",
    body: "Four agents repair the code in a sandbox and run pytest until it passes, then hand over the verified diff and a continuity roadmap.",
  },
] as const;

const CHART = [
  { label: "Patient", value: "PayPulse micro-ledger (Python, 2021)" },
  { label: "Last commit", value: "2023-01-18 on main" },
  { label: "Dead branch", value: "feat/webhook-retry-engine · 543 days" },
  { label: "Test suite", value: "15 passed · 1 failing" },
] as const;

export default function LandingPage() {
  return (
    <AppShell>
      <div aria-hidden className="bio-glow pointer-events-none absolute inset-x-0 top-0 h-[36rem]" />
      <div aria-hidden className="motion-scan pointer-events-none absolute inset-x-0 top-0 h-24 bg-linear-to-b from-emerald-bio/0 via-emerald-bio/[0.04] to-emerald-bio/0 animate-scan" />

      <section aria-labelledby="hero-heading" className="relative mx-auto max-w-7xl px-4 pb-16 pt-14 sm:px-6 lg:pt-20">
        <div className="grid items-start gap-10 lg:grid-cols-[1.1fr_0.9fr] lg:gap-14">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.3em] text-emerald-bio">
              Kiroween · Resurrection Track
            </p>
            <h1
              id="hero-heading"
              className="mt-4 font-display text-4xl font-bold uppercase leading-[1.05] tracking-[0.06em] text-ink sm:text-5xl lg:text-6xl"
            >
              Dead code
              <br />
              still has a <span className="text-emerald-bio drop-shadow-[0_0_18px_rgb(16_185_129/0.55)]">pulse</span>.
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-ink-muted">
              Lazarus Foundry is an AI software revival foundry. It reads a dormant repository, reconstructs what the
              software was meant to become, proposes scored ways to bring it back, and, once you approve one, repairs
              the code in a sandbox until the tests pass.
            </p>
            <div className="mt-6 flex flex-wrap gap-2" role="group" aria-label="Genome legend">
              <VitalBadge status="VERIFIED_WORKING" />
              <VitalBadge status="ABANDONED_IN_PROGRESS" />
              <VitalBadge status="GHOST_FEATURE" />
              <EpistemicBadge status="CONFLICTED" />
            </div>
            <dl className="mt-10 grid max-w-xl grid-cols-1 gap-x-8 gap-y-3 border-t border-line pt-6 sm:grid-cols-2">
              {CHART.map((row) => (
                <div key={row.label}>
                  <dt className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-muted">{row.label}</dt>
                  <dd className="mt-0.5 font-mono text-sm text-ink">{row.value}</dd>
                </div>
              ))}
            </dl>
          </div>

          <Panel
            eyebrow="Patient Zero"
            title="PayPulse is flatlining"
            headingId="patient-zero-heading"
            tone="alive"
            glow
            actions={<TokenBadge token="conflict" label="3 README lies" glyph="⚠" appearance="outlined" />}
          >
            <p className="mb-5 text-sm leading-relaxed text-ink-muted">
              Its README claims five finished features. The code says otherwise: webhook retries died on an unmerged
              branch, currency conversion is a stub, and PDF export never existed.
            </p>
            <ResurrectTrigger />
          </Panel>
        </div>
      </section>

      <section aria-labelledby="pipeline-heading" className="relative border-y border-line/70 bg-slate-core/60">
        <div className="mx-auto max-w-7xl px-4 py-14 sm:px-6">
          <p className="font-mono text-xs uppercase tracking-[0.3em] text-ink-muted">The procedure</p>
          <h2 id="pipeline-heading" className="mt-2 font-display text-2xl font-semibold uppercase tracking-[0.12em] text-ink">
            Five intelligence systems, one resurrection
          </h2>
          <PipelineTrack showDescriptions className="mt-10" />
        </div>
      </section>

      <section aria-labelledby="views-heading" className="mx-auto max-w-7xl px-4 py-14 sm:px-6">
        <h2 id="views-heading" className="font-display text-2xl font-semibold uppercase tracking-[0.12em] text-ink">
          Three screens in the lab
        </h2>
        <ul className="mt-8 grid gap-5 md:grid-cols-3">
          {VIEWS.map((view) => (
            <li key={view.name}>
              <Panel as="article" className="h-full">
                <p className={`font-mono text-xs tracking-[0.25em] ${view.tone}`}>{view.index}</p>
                <h3 className="mt-2 font-display text-xl font-semibold uppercase tracking-[0.1em] text-ink">{view.name}</h3>
                <p className="mt-3 text-sm leading-relaxed text-ink-muted">{view.body}</p>
              </Panel>
            </li>
          ))}
        </ul>
        <p className="mt-10 max-w-3xl font-mono text-xs leading-relaxed text-ink-muted">
          Every genome node cites real evidence (file, commit, line range, snippet). Confidence and feasibility are
          deterministic formulas, not model guesses. The Foundry cannot touch code until a human approves the exact
          strategy content they reviewed.
        </p>
      </section>
    </AppShell>
  );
}
