import type { ReactNode } from "react";

import { Brand } from "./Brand";

/**
 * The frame of every screen: brand bar, optional project navigation and status, the content and a
 * thin footer. Project screens pass `nav` (ProjectNav) and `status`.
 */
export function AppShell({ nav, status, children }: { nav?: ReactNode; status?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-30 border-b border-line/80 bg-obsidian/85 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4 sm:px-6">
          <Brand />
          {nav && <div className="hidden min-w-0 flex-1 md:block">{nav}</div>}
          <div className="ml-auto flex items-center gap-3">{status}</div>
        </div>
        {nav && <div className="border-t border-line/60 px-4 py-2 md:hidden">{nav}</div>}
      </header>
      <main id="main" className="relative flex-1">
        {children}
      </main>
      <footer className="border-t border-line/60">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2 px-4 py-4 font-mono text-[11px] uppercase tracking-[0.18em] text-ink-muted sm:px-6">
          <span>Lazarus Foundry · Kiroween Resurrection Track</span>
          <span>Every claim cites evidence · Humans approve every change</span>
        </div>
      </footer>
    </div>
  );
}
