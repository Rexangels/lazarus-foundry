import type { ReactNode } from "react";

import { colorTokenClasses } from "@/lib/palette";
import type { ColorToken } from "@/lib/types";

import { cx } from "./cx";

/** A monospace "forensic" label/value pair, e.g. `C(f) 0.8594`. */
export function Readout({
  label,
  value,
  token,
  className,
}: {
  label: string;
  value: ReactNode;
  token?: ColorToken;
  className?: string;
}) {
  return (
    <div className={cx("min-w-0", className)}>
      <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-muted">{label}</dt>
      <dd className={cx("mt-0.5 font-mono text-lg tabular-nums", token ? colorTokenClasses(token).text : "text-ink")}>
        {value}
      </dd>
    </div>
  );
}

/** A row of readouts as one definition list. */
export function ReadoutGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cx("grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4", className)}>{children}</dl>;
}
