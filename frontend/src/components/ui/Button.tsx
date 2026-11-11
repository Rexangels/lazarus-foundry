import Link from "next/link";
import type { ButtonHTMLAttributes, ComponentProps, ReactNode, Ref } from "react";

import { cx } from "./cx";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "charge";
export type ButtonSize = "sm" | "md" | "lg";

const BASE =
  "relative inline-flex select-none items-center justify-center gap-2 rounded-md font-display font-semibold uppercase tracking-[0.12em] transition-[background-color,box-shadow,color,transform] duration-200 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-emerald-bio disabled:cursor-not-allowed disabled:opacity-50";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-emerald-bio text-obsidian hover:bg-emerald-400 shadow-glow-alive",
  secondary: "border border-line bg-slate-raised text-ink hover:border-emerald-bio/60 hover:text-white",
  ghost: "text-ink-muted hover:bg-slate-raised hover:text-ink",
  danger: "border border-ghost/60 bg-ghost/10 text-ghost-text hover:bg-ghost/20",
  // The defibrillator charge: the one big call to action on a screen.
  charge:
    "group overflow-hidden border border-emerald-bio/70 bg-linear-to-b from-emerald-bio/25 to-emerald-bio/5 text-white shadow-glow-alive hover:from-emerald-bio/40 hover:to-emerald-bio/10 active:scale-[0.99] aria-busy:animate-charge",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-5 text-sm",
  lg: "h-16 px-8 text-base md:text-lg",
};

export function buttonClasses(variant: ButtonVariant = "secondary", size: ButtonSize = "md", className?: string) {
  return cx(BASE, VARIANTS[variant], SIZES[size], className);
}

function ChargeSheen() {
  return (
    <span
      aria-hidden
      className="pointer-events-none absolute inset-y-0 -left-1/3 w-1/3 -skew-x-12 bg-linear-to-r from-transparent via-white/25 to-transparent opacity-0 transition-opacity group-hover:opacity-100 group-hover:animate-sheen group-aria-busy:opacity-100 group-aria-busy:animate-sheen"
    />
  );
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a busy state (aria-busy) and keeps the label readable. */
  busy?: boolean;
  icon?: ReactNode;
  /** React 19 passes `ref` as a prop; it reaches the <button> (focus management). */
  ref?: Ref<HTMLButtonElement>;
}

export function Button({
  variant = "secondary",
  size = "md",
  busy = false,
  icon,
  className,
  children,
  type = "button",
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      className={buttonClasses(variant, size, className)}
      {...rest}
    >
      {variant === "charge" && <ChargeSheen />}
      {icon && <span aria-hidden className="shrink-0">{icon}</span>}
      <span className="relative">{children}</span>
    </button>
  );
}

export interface ButtonLinkProps extends ComponentProps<typeof Link> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function ButtonLink({ variant = "secondary", size = "md", className, children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={buttonClasses(variant, size, className)} {...rest}>
      {variant === "charge" && <ChargeSheen />}
      <span className="relative">{children}</span>
    </Link>
  );
}
