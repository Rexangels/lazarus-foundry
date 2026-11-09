import type { ReactNode } from "react";

/** Title block at the top of each project view. */
export function ViewHeader({
  index,
  title,
  metaphor,
  children,
}: {
  index: string;
  title: string;
  /** One line on what the view does, in the medical metaphor. */
  metaphor: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="font-mono text-[11px] uppercase tracking-[0.25em] text-emerald-bio">View {index}</p>
        <h1 className="mt-1 font-display text-3xl font-bold uppercase tracking-[0.12em] text-ink sm:text-4xl">{title}</h1>
        <p className="mt-2 max-w-2xl text-ink-muted">{metaphor}</p>
      </div>
      {children}
    </div>
  );
}
