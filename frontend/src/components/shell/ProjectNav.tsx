"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cx } from "@/components/ui/cx";

export const PROJECT_VIEWS = [
  { slug: "autopsy", index: "01", label: "Autopsy", hint: "Genome graph and evidence" },
  { slug: "defibrillator", index: "02", label: "Defibrillator", hint: "Strategies and approval" },
  { slug: "heartbeat", index: "03", label: "Heartbeat", hint: "Live foundry and rebirth" },
] as const;

export type ProjectView = (typeof PROJECT_VIEWS)[number]["slug"];

export function projectViewHref(projectId: string, view: ProjectView): string {
  return `/foundry/${projectId}/${view}`;
}

/** Landing -> Autopsy -> Defibrillator -> Heartbeat. The current view gets aria-current="page". */
export function ProjectNav({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Project views">
      <ol className="flex items-center gap-1 overflow-x-auto">
        {PROJECT_VIEWS.map((view, position) => {
          const href = projectViewHref(projectId, view.slug);
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={view.slug} className="flex items-center">
              {position > 0 && (
                <span aria-hidden className="mx-1 h-px w-4 bg-line" />
              )}
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                title={view.hint}
                className={cx(
                  "flex items-center gap-2 rounded-md px-3 py-1.5 font-display text-xs font-semibold uppercase tracking-[0.18em] transition-colors",
                  active
                    ? "bg-emerald-bio/10 text-emerald-bio shadow-glow-alive"
                    : "text-ink-muted hover:bg-slate-raised hover:text-ink",
                )}
              >
                <span className="font-mono text-[10px] opacity-80">{view.index}</span>
                {view.label}
              </Link>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
