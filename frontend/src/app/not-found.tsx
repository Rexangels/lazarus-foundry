import { AppShell } from "@/components/shell/AppShell";
import { ButtonLink } from "@/components/ui/Button";
import { HeartbeatLine } from "@/components/ui/HeartbeatLine";

export default function NotFound() {
  return (
    <AppShell>
      <div className="mx-auto max-w-2xl px-4 py-24 text-center sm:px-6">
        <HeartbeatLine state="flat" token="ghost" label="Flatline" />
        <h1 className="mt-6 font-display text-3xl font-bold uppercase tracking-[0.12em] text-ink">No such patient</h1>
        <p className="mt-3 text-ink-muted">This page or project does not exist.</p>
        <ButtonLink href="/" variant="primary" className="mt-8">
          Back to the lab
        </ButtonLink>
      </div>
    </AppShell>
  );
}
