import { TokenBadge } from "@/components/ui/StatusBadge";
import type { ColorToken } from "@/lib/types";
import type { ConnectionState } from "@/lib/ws";

const STYLE: Record<ConnectionState, { token: ColorToken; label: string; glyph: string; pulse: boolean }> = {
  idle: { token: "neutral", label: "Telemetry idle", glyph: "○", pulse: false },
  connecting: { token: "dormant", label: "Connecting", glyph: "◌", pulse: true },
  open: { token: "alive", label: "Telemetry live", glyph: "◉", pulse: true },
  reconnecting: { token: "dormant", label: "Reconnecting", glyph: "◌", pulse: true },
  closed: { token: "neutral", label: "Telemetry closed", glyph: "○", pulse: false },
  not_found: { token: "ghost", label: "Unknown project", glyph: "✕", pulse: false },
  replay: { token: "conflict", label: "Offline demo replay", glyph: "▶", pulse: true },
};

export function ConnectionIndicator({ state, attempts = 0 }: { state: ConnectionState; attempts?: number }) {
  const style = STYLE[state];
  const label = state === "reconnecting" && attempts > 0 ? `${style.label} (${attempts})` : style.label;
  return <TokenBadge token={style.token} label={label} glyph={style.glyph} pulse={style.pulse} appearance="outlined" />;
}
