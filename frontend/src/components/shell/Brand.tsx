import Link from "next/link";

/** The Lazarus mark: a cross-shaped vital line inside a ring. */
export function LazarusMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <circle cx="16" cy="16" r="14.5" fill="none" stroke="currentColor" strokeOpacity="0.35" />
      <path
        d="M3 17h7l2-5 3 11 3-15 2 9h9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Brand() {
  return (
    <Link href="/" className="group inline-flex items-center gap-2.5 rounded" aria-label="Lazarus Foundry home">
      <LazarusMark className="h-7 w-7 text-emerald-bio drop-shadow-[0_0_6px_rgb(16_185_129/0.6)]" />
      <span className="font-display text-sm font-semibold uppercase tracking-[0.28em] text-ink group-hover:text-white">
        Lazarus <span className="text-emerald-bio">Foundry</span>
      </span>
    </Link>
  );
}
