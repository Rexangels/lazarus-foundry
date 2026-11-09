"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";
import { TokenBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/components/ui/cx";
import { describeApiError, getApiClient, getMockApiClient, isApiError, isUuid, type ApiClient } from "@/lib/api";

type Backend = "checking" | "online" | "offline" | "mock";

type Phase =
  | { kind: "idle" }
  | { kind: "charging"; offline: boolean }
  | { kind: "resurrecting"; projectId: string; name: string; offline: boolean }
  | { kind: "error"; message: string; offline: boolean; queueDown: boolean };

const BACKEND_BADGE: Record<Backend, { token: "alive" | "dormant" | "ghost" | "conflict"; label: string; glyph: string }> = {
  checking: { token: "dormant", label: "Checking backend", glyph: "◌" },
  online: { token: "alive", label: "Backend online", glyph: "◉" },
  offline: { token: "ghost", label: "Backend offline", glyph: "✕" },
  mock: { token: "conflict", label: "Offline demo mode", glyph: "▶" },
};

/**
 * The 1-click "Resurrect Patient Zero (PayPulse)" trigger. It calls
 * `POST /api/projects/ingest {"demo": "paypulse"}` and opens the project's Autopsy, where the
 * telemetry stream shows ingestion and genome progress.
 */
export function ResurrectTrigger() {
  const router = useRouter();
  const [backend, setBackend] = useState<Backend>("checking");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  useEffect(() => {
    const client = getApiClient();
    if (client.mode === "mock") {
      setBackend("mock");
      return;
    }
    let cancelled = false;
    client
      .health()
      .then(() => !cancelled && setBackend("online"))
      .catch(() => !cancelled && setBackend("offline"));
    return () => {
      cancelled = true;
    };
  }, []);

  async function resurrect(client: ApiClient, offline: boolean) {
    setPhase({ kind: "charging", offline });
    try {
      const accepted = await client.resurrectPatientZero();
      if (!isUuid(accepted.project_id)) throw new Error("The backend returned an invalid project id.");
      setPhase({ kind: "resurrecting", projectId: accepted.project_id, name: accepted.name, offline });
      router.push(`/foundry/${accepted.project_id}/autopsy`);
    } catch (error) {
      const queueDown = isApiError(error) && error.status === 503;
      const unreachable = isApiError(error) && (error.kind === "offline" || error.kind === "timeout");
      if (unreachable) setBackend("offline");
      setPhase({
        kind: "error",
        message: queueDown
          ? `${describeApiError(error)} The project was recorded as FAILED.`
          : describeApiError(error),
        offline: unreachable,
        queueDown,
      });
    }
  }

  const busy = phase.kind === "charging" || phase.kind === "resurrecting";
  const pulsing = phase.kind === "charging" || phase.kind === "resurrecting";
  const badge = BACKEND_BADGE[backend];

  return (
    <div className="relative">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="font-mono text-[11px] uppercase tracking-[0.25em] text-ink-muted">Vital signs · Patient Zero</p>
        <TokenBadge token={badge.token} label={badge.label} glyph={badge.glyph} appearance="outlined" pulse={backend === "online"} />
      </div>

      <div
        className={cx(
          "rounded-md border bg-obsidian/70 px-3 py-2 transition-colors duration-700",
          pulsing ? "border-emerald-bio/60 shadow-glow-alive" : phase.kind === "error" ? "border-ghost/50" : "border-line",
        )}
      >
        <HeartbeatLine
          state={pulsing ? "pulse" : "flat"}
          token={phase.kind === "error" ? "ghost" : "alive"}
          period={phase.kind === "charging" ? 0.9 : 1.4}
          label={pulsing ? "Heartbeat: the resurrection has started" : "Flatline: PayPulse has no vital signs"}
          className="h-20 sm:h-24"
        />
        <div className="flex items-center justify-between font-mono text-[11px] uppercase tracking-[0.2em]">
          <span className={pulsing ? "text-emerald-bio" : "text-ink-muted"}>{pulsing ? "Sinus rhythm" : "Asystole"}</span>
          <span className="tabular-nums text-ink-muted">{pulsing ? "72 BPM" : "0 BPM"}</span>
        </div>
      </div>

      <Button
        variant="charge"
        size="lg"
        busy={busy}
        className="mt-5 w-full"
        onClick={() => resurrect(getApiClient(), backend === "mock")}
        icon={<span className="text-xl leading-none">⚡</span>}
      >
        {phase.kind === "charging"
          ? "Charging… clear!"
          : phase.kind === "resurrecting"
            ? "Heartbeat detected · opening the Autopsy"
            : "Resurrect Patient Zero (PayPulse)"}
      </Button>

      <div aria-live="polite" className="mt-4 min-h-6 text-sm">
        {phase.kind === "resurrecting" && (
          <p className="text-emerald-bio">
            {phase.name} admitted as <span className="font-mono text-xs">{phase.projectId}</span>. Streaming telemetry…
          </p>
        )}
        {phase.kind === "error" && (
          <div role="alert" className="rounded-md border border-ghost/50 bg-ghost/10 p-3 text-ink">
            <p>
              <strong className="font-semibold text-ghost-text">
                {phase.offline ? "No response from the lab. " : phase.queueDown ? "The worker queue is down. " : "The resurrection failed. "}
              </strong>
              {phase.message}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" onClick={() => resurrect(getApiClient(), false)}>
                Try again
              </Button>
              <Button size="sm" variant="primary" onClick={() => resurrect(getMockApiClient(), true)}>
                Run the offline demo
              </Button>
            </div>
          </div>
        )}
        {phase.kind === "idle" && backend === "offline" && (
          <p className="text-ink-muted">
            The backend at the configured API URL is not answering. You can still{" "}
            <button
              type="button"
              className="rounded text-emerald-bio underline decoration-emerald-bio/40 underline-offset-4 hover:decoration-emerald-bio"
              onClick={() => resurrect(getMockApiClient(), true)}
            >
              run the offline demo
            </button>{" "}
            with the recorded PayPulse results.
          </p>
        )}
        {phase.kind === "idle" && backend === "mock" && (
          <p className="text-ink-muted">Offline demo mode: the run replays the recorded PayPulse results, no backend needed.</p>
        )}
      </div>
    </div>
  );
}
