/**
 * The product palette (tech.md) and the mapping from backend color tokens to it
 * (design.md, "Color tokens"). The backend never sends hex values.
 *
 * Tailwind classes are written out in full so Tailwind's scanner finds them; never build a
 * class name from a token at runtime.
 */
import type { ColorToken, EpistemicStatus, VitalStatus } from "./types";

export const PALETTE = {
  obsidian: "#07090E",
  slate: "#0F141C",
  emerald: "#10B981",
  amber: "#F59E0B",
  crimson: "#EF4444",
  violet: "#8B5CF6",
  /** Neutral gray for lines and nodes; Slate is the surface color, so it would vanish. */
  neutral: "#64748B",
  /** Text colors (WCAG AA on Obsidian and Slate). */
  ink: "#E6EDF3",
  inkMuted: "#94A3B8",
  /**
   * Lighter tints of the token hues for small text. The brand colors stay the fills, lines and glows;
   * on a 10 % tinted chip, Emerald and Crimson text drop below 4.5:1, so text uses these.
   */
  aliveText: "#34D399",
  dormantText: "#FBBF24",
  ghostText: "#F87171",
  violetText: "#A78BFA",
} as const;

/** Text hex per token (the tints above), for inline SVG labels. */
export const COLOR_TOKEN_TEXT_HEX: Record<ColorToken, string> = {
  alive: PALETTE.aliveText,
  dormant: PALETTE.dormantText,
  ghost: PALETTE.ghostText,
  conflict: PALETTE.violetText,
  neutral: PALETTE.inkMuted,
};

export const COLOR_TOKENS: readonly ColorToken[] = ["alive", "dormant", "ghost", "conflict", "neutral"];

/** Hex for each token, for SVG strokes and React Flow edges. */
export const COLOR_TOKEN_HEX: Record<ColorToken, string> = {
  alive: PALETTE.emerald,
  dormant: PALETTE.amber,
  ghost: PALETTE.crimson,
  conflict: PALETTE.violet,
  neutral: PALETTE.neutral,
};

export interface TokenClasses {
  /** Text color that passes WCAG AA (4.5:1) on Obsidian, Slate and the token's own `bg` tint. */
  text: string;
  /** Tinted background for chips and node bodies. */
  bg: string;
  border: string;
  /** Solid fill (dots, bars). */
  fill: string;
  /** Soft outer glow. */
  glow: string;
}

export const COLOR_TOKEN_CLASSES: Record<ColorToken, TokenClasses> = {
  alive: {
    text: "text-alive-text",
    bg: "bg-alive/10",
    border: "border-alive/50",
    fill: "bg-alive",
    glow: "shadow-glow-alive",
  },
  dormant: {
    text: "text-dormant-text",
    bg: "bg-dormant/10",
    border: "border-dormant/50",
    fill: "bg-dormant",
    glow: "shadow-glow-dormant",
  },
  ghost: {
    text: "text-ghost-text",
    bg: "bg-ghost/10",
    border: "border-ghost/50",
    fill: "bg-ghost",
    glow: "shadow-glow-ghost",
  },
  conflict: {
    // Electric Violet itself is about 4.4:1 on Slate, below AA for small text, so text uses the lighter tint.
    text: "text-conflict-text",
    bg: "bg-conflict/10",
    border: "border-conflict/60",
    fill: "bg-conflict",
    glow: "shadow-glow-conflict",
  },
  neutral: {
    text: "text-ink-muted",
    bg: "bg-neutral-token/10",
    border: "border-neutral-token/50",
    fill: "bg-neutral-token",
    glow: "",
  },
};

function isColorToken(value: unknown): value is ColorToken {
  return typeof value === "string" && (COLOR_TOKENS as readonly string[]).includes(value);
}

/** Unknown tokens (a newer backend) fall back to neutral rather than breaking the screen. */
export function normalizeColorToken(value: unknown): ColorToken {
  return isColorToken(value) ? value : "neutral";
}

export function colorTokenHex(value: unknown): string {
  return COLOR_TOKEN_HEX[normalizeColorToken(value)];
}

export function colorTokenClasses(value: unknown): TokenClasses {
  return COLOR_TOKEN_CLASSES[normalizeColorToken(value)];
}

/** The token the backend uses for each vital status. */
export const VITAL_STATUS_TOKEN: Record<VitalStatus, ColorToken> = {
  VERIFIED_WORKING: "alive",
  ABANDONED_IN_PROGRESS: "dormant",
  GHOST_FEATURE: "ghost",
};

export const VITAL_STATUS_LABEL: Record<VitalStatus, string> = {
  VERIFIED_WORKING: "Verified working",
  ABANDONED_IN_PROGRESS: "Abandoned in progress",
  GHOST_FEATURE: "Ghost feature",
};

/**
 * Epistemic statuses say how well the evidence is understood, not whether the feature lives, so
 * their badges are outlined and carry a glyph; color alone never carries the meaning.
 */
export const EPISTEMIC_STATUS_STYLE: Record<EpistemicStatus, { token: ColorToken; label: string; glyph: string; dashed: boolean }> = {
  KNOWN: { token: "alive", label: "Known", glyph: "✓", dashed: false },
  INFERRED: { token: "neutral", label: "Inferred", glyph: "≈", dashed: true },
  CONFLICTED: { token: "conflict", label: "Conflicted", glyph: "⚠", dashed: false },
  MISSING_CONTEXT: { token: "neutral", label: "Missing context", glyph: "?", dashed: true },
};

export const VITAL_STATUS_GLYPH: Record<VitalStatus, string> = {
  VERIFIED_WORKING: "●",
  ABANDONED_IN_PROGRESS: "◐",
  GHOST_FEATURE: "○",
};

/** WCAG relative luminance of a #RRGGBB color. */
export function relativeLuminance(hex: string): number {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!match?.[1]) throw new TypeError(`Not a #RRGGBB color: ${hex}`);
  const value = parseInt(match[1], 16);
  const channel = (shift: number) => {
    const c = ((value >> shift) & 0xff) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0);
}

/** WCAG contrast ratio between two #RRGGBB colors (1 to 21). */
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
