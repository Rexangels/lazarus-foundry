import type { ReactNode } from "react";

import { colorTokenClasses } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

import { cx } from "./cx";

export interface PanelProps {
  as?: "section" | "div" | "article" | "aside";
  /** Small mono label above the title, e.g. "SYSTEM 01". */
  eyebrow?: string;
  title?: ReactNode;
  /** Right side of the header (buttons, badges). */
  actions?: ReactNode;
  /** Colors the corner brackets and adds the matching glow. */
  tone?: ColorToken;
  glow?: boolean;
  padded?: boolean;
  className?: string;
  children?: ReactNode;
  /** id of the heading, for aria-labelledby on the region. */
  headingId?: string;
}

/**
 * The basic surface: Slate on Obsidian with a hairline border and forensic corner brackets.
 * With a `title` it renders a heading and labels the region with it.
 */
export function Panel({
  as: Tag = "section",
  eyebrow,
  title,
  actions,
  tone,
  glow = false,
  padded = true,
  className,
  children,
  headingId,
}: PanelProps) {
  const toneClasses = tone ? colorTokenClasses(tone) : null;
  const bracket = cx("pointer-events-none absolute h-3 w-3", toneClasses ? toneClasses.border : "border-ink-muted/40");
  return (
    <Tag
      aria-labelledby={title && headingId ? headingId : undefined}
      className={cx(
        "relative rounded-lg border border-line bg-slate-core/90 backdrop-blur-sm",
        glow && toneClasses?.glow,
        className,
      )}
    >
      <span aria-hidden className={cx(bracket, "-left-px -top-px rounded-tl-lg border-l-2 border-t-2")} />
      <span aria-hidden className={cx(bracket, "-right-px -top-px rounded-tr-lg border-r-2 border-t-2")} />
      <span aria-hidden className={cx(bracket, "-bottom-px -left-px rounded-bl-lg border-b-2 border-l-2")} />
      <span aria-hidden className={cx(bracket, "-bottom-px -right-px rounded-br-lg border-b-2 border-r-2")} />
      {(eyebrow || title || actions) && (
        <header className={cx("flex items-start justify-between gap-4", padded ? "px-5 pt-4" : "px-4 pt-3")}>
          <div className="min-w-0">
            {eyebrow && (
              <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-ink-muted">{eyebrow}</p>
            )}
            {title && (
              <h2 id={headingId} className="mt-1 font-display text-lg font-semibold tracking-wide text-ink">
                {title}
              </h2>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx(padded ? "p-5" : "p-3", (eyebrow || title || actions) && "pt-3")}>{children}</div>
    </Tag>
  );
}
