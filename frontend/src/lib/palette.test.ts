import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  COLOR_TOKENS,
  COLOR_TOKEN_CLASSES,
  COLOR_TOKEN_HEX,
  COLOR_TOKEN_TEXT_HEX,
  PALETTE,
  VITAL_STATUS_TOKEN,
  colorTokenClasses,
  colorTokenHex,
  contrastRatio,
  normalizeColorToken,
} from "./palette";

describe("color token mapping (design.md, Color tokens)", () => {
  it("maps every backend token to the palette color in the design table", () => {
    expect(COLOR_TOKEN_HEX).toEqual({
      alive: "#10B981",
      dormant: "#F59E0B",
      ghost: "#EF4444",
      conflict: "#8B5CF6",
      neutral: PALETTE.neutral,
    });
  });

  it("uses a visible gray for neutral, not the Slate surface color", () => {
    expect(COLOR_TOKEN_HEX.neutral).not.toBe(PALETTE.slate);
    expect(contrastRatio(COLOR_TOKEN_HEX.neutral, PALETTE.slate)).toBeGreaterThanOrEqual(3);
  });

  it("matches the backend's vital-status tokens", () => {
    expect(VITAL_STATUS_TOKEN).toEqual({
      VERIFIED_WORKING: "alive",
      ABANDONED_IN_PROGRESS: "dormant",
      GHOST_FEATURE: "ghost",
    });
  });

  it("has literal Tailwind classes for every token", () => {
    for (const token of COLOR_TOKENS) {
      expect(COLOR_TOKEN_CLASSES[token].text).toMatch(/^text-/);
      expect(colorTokenClasses(token)).toBe(COLOR_TOKEN_CLASSES[token]);
    }
  });

  /** **Validates: design.md Color tokens (unknown tokens degrade to neutral)** */
  it("property: any string maps to a known token, and known tokens map to themselves", () => {
    fc.assert(
      fc.property(fc.oneof(fc.string(), fc.constantFrom(...COLOR_TOKENS)), (value) => {
        const token = normalizeColorToken(value);
        expect(COLOR_TOKENS).toContain(token);
        if ((COLOR_TOKENS as readonly string[]).includes(value)) expect(token).toBe(value);
        else expect(token).toBe("neutral");
        expect(colorTokenHex(value)).toBe(COLOR_TOKEN_HEX[token]);
      }),
    );
  });
});

/** `fg` at `alpha` over `bg`, as #RRGGBB (how a `bg-alive/10` chip looks on Slate). */
function blend(fg: string, bg: string, alpha: number): string {
  const channels = (hex: string) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const [f, b] = [channels(fg), channels(bg)];
  return `#${f.map((c, i) => Math.round(c * alpha + b[i]! * (1 - alpha)).toString(16).padStart(2, "0")).join("")}`;
}

describe("text contrast (WCAG AA, 4.5:1)", () => {
  const textColors = {
    ink: PALETTE.ink,
    inkMuted: PALETTE.inkMuted,
    emerald: PALETTE.emerald,
    amber: PALETTE.amber,
    crimson: PALETTE.crimson,
  };

  for (const [name, color] of Object.entries(textColors)) {
    it(`${name} text passes on Obsidian and Slate`, () => {
      expect(contrastRatio(color, PALETTE.obsidian)).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(color, PALETTE.slate)).toBeGreaterThanOrEqual(4.5);
    });
  }

  for (const token of COLOR_TOKENS) {
    it(`${token} badge text passes on its own 10 % tinted chip over Slate`, () => {
      const chip = blend(COLOR_TOKEN_HEX[token], PALETTE.slate, 0.1);
      expect(contrastRatio(COLOR_TOKEN_TEXT_HEX[token], chip)).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(COLOR_TOKEN_TEXT_HEX[token], PALETTE.obsidian)).toBeGreaterThanOrEqual(4.5);
    });
  }

  it("explains why conflict text uses a lighter violet", () => {
    expect(contrastRatio(PALETTE.violet, PALETTE.slate)).toBeLessThan(4.5);
  });

  it("primary buttons: Obsidian text on Emerald passes", () => {
    expect(contrastRatio(PALETTE.obsidian, PALETTE.emerald)).toBeGreaterThanOrEqual(4.5);
  });
});
