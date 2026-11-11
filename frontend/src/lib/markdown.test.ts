import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { footnoteLabels, inlineText, parseInline, parseMarkdown, safeHref, type MdBlock, type MdList } from "./markdown";
import { MOCK_REBIRTH } from "./mock/rebirth";
import { MOCK_STRATEGIES } from "./mock/strategies";

describe("parseInline", () => {
  it("reads code spans, strong text and footnote references", () => {
    expect(parseInline("THE test `tests/test_retry.py` SHALL pass. [^E2]")).toEqual([
      { type: "text", text: "THE test " },
      { type: "code", text: "tests/test_retry.py" },
      { type: "text", text: " SHALL pass. " },
      { type: "footnoteRef", label: "E2" },
    ]);
    expect(parseInline("**User Story:** As a maintainer")).toEqual([
      { type: "strong", children: [{ type: "text", text: "User Story:" }] },
      { type: "text", text: " As a maintainer" },
    ]);
  });

  it("applies backslash escapes (inline_text escapes \\ [ ] < > |) and keeps HTML as text", () => {
    expect(parseInline("a \\[x\\] \\<b\\> \\| c")).toEqual([{ type: "text", text: "a [x] <b> | c" }]);
    expect(parseInline("<script>alert(1)</script>")).toEqual([{ type: "text", text: "<script>alert(1)</script>" }]);
  });

  it("leaves an unclosed backtick or ** as literal text", () => {
    expect(inlineText(parseInline("a `b and **c"))).toBe("a `b and **c");
  });

  it("returns plain text unchanged when it has no markup characters", () => {
    fc.assert(
      fc.property(fc.string({ unit: fc.constantFrom(..."abc XYZ 019.,:;-_/()<>&\"'é→".split("")) }), (text) => {
        const nodes = parseInline(text);
        expect(inlineText(nodes)).toBe(text);
        expect(nodes.every((node) => node.type === "text")).toBe(true);
      }),
    );
  });

  it("never throws on markup-heavy input", () => {
    fc.assert(
      fc.property(fc.string({ unit: fc.constantFrom(..."`*[]^\\:-# 1.\nE".split("")) }), (text) => {
        expect(() => parseMarkdown(text)).not.toThrow();
      }),
    );
  });
});

describe("parseMarkdown", () => {
  it("parses headings, paragraphs, numbered criteria and nested checkbox tasks", () => {
    const blocks = parseMarkdown(
      [
        "# Requirements — X",
        "## Introduction",
        "Line one",
        "line two.",
        "#### Acceptance Criteria",
        "1. THE x SHALL y. [^E1]",
        "2. WHEN a, THE b SHALL c.",
        "- [ ] 1. Merge it",
        "  - Plan item `restore-01`",
        "  - [ ] 1.1 Merge `feat/x`",
        "  - [x] 1.2 Verify: run tests",
        "- [ ] 2. Next",
        "",
        "[^E1]: `README.md:11` · README_CLAIM",
      ].join("\n"),
    );
    expect(blocks.map((b) => b.type)).toEqual(["heading", "heading", "paragraph", "heading", "list", "list", "footnoteDef"]);
    expect(blocks[2]).toEqual({ type: "paragraph", children: [{ type: "text", text: "Line one line two." }] });
    const criteria = blocks[4] as MdList;
    expect(criteria.ordered).toBe(true);
    expect(criteria.items).toHaveLength(2);
    const tasks = blocks[5] as MdList;
    expect(tasks.items.map((item) => [item.checked, inlineText(item.children)])).toEqual([
      [false, "1. Merge it"],
      [false, "2. Next"],
    ]);
    const sub = tasks.items[0]?.sublists[0];
    expect(sub?.items.map((item) => item.checked)).toEqual([null, false, true]);
    expect(blocks[6]).toMatchObject({ type: "footnoteDef", label: "E1" });
  });

  it("parses every recorded spec document, and each footnote it cites is defined in the same file", () => {
    for (const strategy of MOCK_STRATEGIES) {
      for (const key of ["requirements_md", "design_md", "tasks_md"] as const) {
        const source = strategy.spec[key];
        const blocks = parseMarkdown(source);
        const defined = new Set(
          blocks.filter((b): b is Extract<MdBlock, { type: "footnoteDef" }> => b.type === "footnoteDef").map((b) => b.label),
        );
        const cited = footnoteLabels(blocks);
        expect(cited.length, `${strategy.strategy_type} ${key} cites evidence`).toBeGreaterThan(0);
        for (const label of cited) expect(defined.has(label), `${strategy.strategy_type} ${key} ${label}`).toBe(true);
        expect(blocks[0]).toMatchObject({ type: "heading", level: 1 });
      }
      // Every checkbox line of tasks.md becomes a checkbox item.
      const tasks = parseMarkdown(strategy.spec.tasks_md);
      const count = (lists: MdList[]): number =>
        lists.reduce((n, list) => n + list.items.reduce((m, item) => m + (item.checked !== null ? 1 : 0) + count(item.sublists), 0), 0);
      const lists = tasks.filter((b): b is MdList => b.type === "list");
      expect(count(lists)).toBe((strategy.spec.tasks_md.match(/^\s*- \[ \] /gm) ?? []).length);
    }
  });
});

describe("tables and links (rebirth PR body)", () => {
  it("parses a GFM table with alignment, escaped pipes and short rows", () => {
    const [table] = parseMarkdown(["| Run | Tests |", "| --- | ---: |", "| a \\| b | 16 |", "| only |"].join("\n"));
    expect(table?.type).toBe("table");
    if (table?.type !== "table") return;
    expect(table.align).toEqual([null, "right"]);
    expect(table.header.map(inlineText)).toEqual(["Run", "Tests"]);
    expect(table.rows.map((row) => row.map(inlineText))).toEqual([
      ["a | b", "16"],
      ["only", ""],
    ]);
  });

  it("parses both tables of the recorded PR body", () => {
    const tables = parseMarkdown(MOCK_REBIRTH.pr_body_markdown).filter((b) => b.type === "table");
    expect(tables.length).toBeGreaterThanOrEqual(2);
    const tests = tables[0];
    if (tests?.type !== "table") return;
    expect(tests.rows.map((row) => row.slice(0, 3).map(inlineText))).toEqual([
      ["Before (main)", "16", "15"],
      ["After (lazarus/restore-7e1d2c3b)", "39", "39"],
    ]);
  });

  it("renders only absolute http(s) URLs as links", () => {
    expect(parseInline("see [docs](https://example.com/a) or <http://example.org>")).toEqual([
      { type: "text", text: "see " },
      { type: "link", href: "https://example.com/a", children: [{ type: "text", text: "docs" }] },
      { type: "text", text: " or " },
      { type: "link", href: "http://example.org/", children: [{ type: "text", text: "http://example.org" }] },
    ]);
    for (const url of ["javascript:alert(1)", "data:text/html,x", "/relative", "mailto:a@b.c", "ftp://x.y"]) {
      const nodes = parseInline(`[x](${url}) <${url}>`);
      expect(nodes.some((n) => n.type === "link")).toBe(false);
    }
    expect(safeHref("https://example.com")).toBe("https://example.com/");
    expect(safeHref("JavaScript:alert(1)")).toBeNull();
  });
});
