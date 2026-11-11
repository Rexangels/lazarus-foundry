/**
 * A small, safe Markdown parser for the generated `.kiro` spec documents (The Defibrillator).
 *
 * It covers the subset `apps/revival/specs.py` writes: ATX headings, paragraphs, `-`/`1.` lists
 * nested by indentation, `[ ]`/`[x]` checkboxes, `**strong**`, `code spans`, backslash escapes,
 * footnote references `[^E1]` and footnote definitions `[^E1]: ...`, plus fenced code and rules.
 * For the rebirth PR body (apps/rebirth/builder.py) it also reads GFM tables (`| a | b |` with a
 * `| --- | ---: |` delimiter row, `\|` inside cells) and links: `[text](url)` and `<url>`, but only
 * for absolute http(s) URLs. Any other link (javascript:, data:, relative, mailto:) stays plain text.
 * The output is a plain tree that React renders as text nodes: there is no HTML pass-through and no
 * raw HTML, so repository-derived text can never inject markup.
 */

export type MdInline =
  | { type: "text"; text: string }
  | { type: "code"; text: string }
  | { type: "strong"; children: MdInline[] }
  | { type: "footnoteRef"; label: string }
  /** `href` is always an absolute http: or https: URL. */
  | { type: "link"; href: string; children: MdInline[] };

export type MdAlign = "left" | "center" | "right" | null;

export interface MdTable {
  type: "table";
  align: MdAlign[];
  header: MdInline[][];
  /** Every row has exactly `align.length` cells (short rows are padded, long rows cut, as in GFM). */
  rows: MdInline[][][];
}

export interface MdListItem {
  /** `null` for a plain bullet, otherwise the checkbox state. */
  checked: boolean | null;
  children: MdInline[];
  /** Nested lists under this item. */
  sublists: MdList[];
}

export interface MdList {
  type: "list";
  ordered: boolean;
  start: number;
  items: MdListItem[];
}

export type MdBlock =
  | { type: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; children: MdInline[] }
  | { type: "paragraph"; children: MdInline[] }
  | MdList
  | { type: "footnoteDef"; label: string; children: MdInline[] }
  | { type: "code"; text: string }
  | { type: "rule" }
  | MdTable;

const ESCAPABLE = /[!-/:-@[-`{-~]/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const LIST_ITEM = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const CHECKBOX = /^\[( |x|X)\]\s+(.*)$/;
const FOOTNOTE_DEF = /^\[\^([^\]\s]+)\]:\s?(.*)$/;
const FENCE = /^\s*(```|~~~)/;
const RULE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const FOOTNOTE_REF = /^\[\^([A-Za-z0-9_.-]+)\]/;

function pushText(out: MdInline[], text: string): void {
  if (!text) return;
  const last = out[out.length - 1];
  if (last?.type === "text") last.text += text;
  else out.push({ type: "text", text });
}

/** Index of the next `**` at or after `from` that is not backslash-escaped, or -1. */
function closingStrong(text: string, from: number): number {
  for (let i = from; i < text.length - 1; i += 1) {
    if (text[i] === "\\") {
      i += 1;
      continue;
    }
    if (text[i] === "*" && text[i + 1] === "*") return i;
  }
  return -1;
}

const AUTOLINK = /^<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>/;
const LINK_DESTINATION = /^\(\s*([^\s()<>]+)\s*\)/;

/** `raw` as a normalized URL when it is an absolute http: or https: URL; otherwise null. */
export function safeHref(raw: string): string | null {
  const value = raw.trim();
  if (!/^https?:\/\/[^/]/i.test(value) || /[\s<>"'`\\]/.test(value)) return null;
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return null;
  }
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** A link's label may not contain another link: nested links become their text. */
function unwrapLinks(nodes: MdInline[]): MdInline[] {
  return nodes.flatMap((node): MdInline[] => {
    if (node.type === "link") return unwrapLinks(node.children);
    if (node.type === "strong") return [{ type: "strong", children: unwrapLinks(node.children) }];
    return [node];
  });
}

/** `[label](http(s) url)` starting at `start`, or null (then the text stays as written). */
function parseLink(text: string, start: number): { node: MdInline; end: number } | null {
  if (text[start + 1] === "^") return null;
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "\\") {
      i += 1;
      continue;
    }
    if (ch === "[") depth += 1;
    else if (ch === "]") {
      depth -= 1;
      if (depth === 0) {
        const destination = LINK_DESTINATION.exec(text.slice(i + 1));
        const href = destination ? safeHref(destination[1] as string) : null;
        if (!destination || !href) return null;
        const label = text.slice(start + 1, i);
        return {
          node: { type: "link", href, children: unwrapLinks(parseInline(label)) },
          end: i + 1 + destination[0].length,
        };
      }
    }
  }
  return null;
}

export function parseInline(text: string): MdInline[] {
  const out: MdInline[] = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i] as string;
    const next = text[i + 1];
    if (ch === "\\" && next !== undefined && ESCAPABLE.test(next)) {
      pushText(out, next);
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 1;
      while (text[i + run] === "`") run += 1;
      const fence = "`".repeat(run);
      let close = text.indexOf(fence, i + run);
      // The closing run must have exactly the same length.
      while (close !== -1 && text[close + run] === "`") {
        let skip = close;
        while (text[skip] === "`") skip += 1;
        close = text.indexOf(fence, skip);
      }
      if (close === -1) {
        pushText(out, fence);
        i += run;
        continue;
      }
      let content = text.slice(i + run, close);
      if (content.length >= 2 && content.startsWith(" ") && content.endsWith(" ") && content.trim()) {
        content = content.slice(1, -1);
      }
      out.push({ type: "code", text: content });
      i = close + run;
      continue;
    }
    if (ch === "[" && next === "^") {
      const match = FOOTNOTE_REF.exec(text.slice(i));
      if (match?.[1]) {
        out.push({ type: "footnoteRef", label: match[1] });
        i += match[0].length;
        continue;
      }
    }
    if (ch === "[") {
      const link = parseLink(text, i);
      if (link) {
        out.push(link.node);
        i = link.end;
        continue;
      }
    }
    if (ch === "<") {
      const match = AUTOLINK.exec(text.slice(i));
      const href = match ? safeHref(match[1] as string) : null;
      if (match && href) {
        out.push({ type: "link", href, children: [{ type: "text", text: match[1] as string }] });
        i += match[0].length;
        continue;
      }
    }
    if (ch === "*" && next === "*") {
      const close = closingStrong(text, i + 2);
      if (close > i + 2) {
        out.push({ type: "strong", children: parseInline(text.slice(i + 2, close)) });
        i = close + 2;
        continue;
      }
    }
    pushText(out, ch);
    i += 1;
  }
  return out;
}

function indentOf(line: string): number {
  return line.length - line.trimStart().length;
}

function isBlank(line: string | undefined): boolean {
  return line === undefined || line.trim() === "";
}

function startsBlock(line: string): boolean {
  return HEADING.test(line) || LIST_ITEM.test(line) || FOOTNOTE_DEF.test(line) || FENCE.test(line) || RULE.test(line);
}

interface RawItem {
  checked: boolean | null;
  text: string;
  sublists: MdList[];
}

function toItem(raw: RawItem): MdListItem {
  return { checked: raw.checked, children: parseInline(raw.text), sublists: raw.sublists };
}

/** Parses the list that starts at `start`; returns it and the index of the first line after it. */
function parseList(lines: readonly string[], start: number): [MdList, number] {
  const first = LIST_ITEM.exec(lines[start] as string) as RegExpExecArray;
  const baseIndent = (first[1] as string).length;
  const ordered = /\d/.test(first[2] as string);
  const startNumber = ordered ? parseInt(first[2] as string, 10) : 1;
  const items: RawItem[] = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (isBlank(line)) {
      // A blank line continues the list only if the next content line is one of its items.
      let j = i + 1;
      while (j < lines.length && isBlank(lines[j])) j += 1;
      const ahead = j < lines.length ? LIST_ITEM.exec(lines[j] as string) : null;
      if (ahead && (ahead[1] as string).length >= baseIndent) {
        i = j;
        continue;
      }
      break;
    }
    const match = LIST_ITEM.exec(line);
    if (match) {
      const indent = (match[1] as string).length;
      if (indent < baseIndent) break;
      const last = items[items.length - 1];
      if (indent > baseIndent && last) {
        const [sublist, after] = parseList(lines, i);
        last.sublists.push(sublist);
        i = after;
        continue;
      }
      if (/\d/.test(match[2] as string) !== ordered) break;
      const content = match[3] as string;
      const box = CHECKBOX.exec(content);
      items.push({
        checked: box ? (box[1] as string).toLowerCase() === "x" : null,
        text: box ? (box[2] as string) : content,
        sublists: [],
      });
      i += 1;
      continue;
    }
    // A lazy continuation line of the last item.
    const last = items[items.length - 1];
    if (last && indentOf(line) > baseIndent && !startsBlock(line.trimStart())) {
      last.text += ` ${line.trim()}`;
      i += 1;
      continue;
    }
    break;
  }
  return [{ type: "list", ordered, start: startNumber, items: items.map(toItem) }, i];
}

const TABLE_DELIMITER_CELL = /^:?-+:?$/;

/** The cells of one table row: outer pipes are optional, `\|` is a literal pipe (also in code spans). */
export function splitTableRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  const cells: string[] = [];
  let cell = "";
  for (let i = 0; i < row.length; i += 1) {
    const ch = row[i] as string;
    if (ch === "\\" && row[i + 1] === "|") {
      cell += "|";
      i += 1;
    } else if (ch === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += ch;
    }
  }
  cells.push(cell.trim());
  return cells;
}

function delimiterAlign(line: string): MdAlign[] | null {
  if (!line.includes("-")) return null;
  const cells = splitTableRow(line);
  if (!cells.every((cell) => TABLE_DELIMITER_CELL.test(cell))) return null;
  return cells.map((cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    return left && right ? "center" : right ? "right" : left ? "left" : null;
  });
}

/** A GFM table: a row with a pipe, then a delimiter row with the same number of cells. */
function isTableStart(lines: readonly string[], i: number): boolean {
  const header = lines[i];
  const delimiter = lines[i + 1];
  if (header === undefined || delimiter === undefined || !header.includes("|") || indentOf(header) >= 4) return false;
  const align = delimiterAlign(delimiter);
  return align !== null && align.length === splitTableRow(header).length;
}

function parseTable(lines: readonly string[], start: number): [MdTable, number] {
  const align = delimiterAlign(lines[start + 1] as string) as MdAlign[];
  const width = align.length;
  const cellsOf = (line: string): MdInline[][] => {
    const cells = splitTableRow(line).slice(0, width);
    while (cells.length < width) cells.push("");
    return cells.map((cell) => parseInline(cell));
  };
  const header = cellsOf(lines[start] as string);
  const rows: MdInline[][][] = [];
  let i = start + 2;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (isBlank(line) || startsBlock(line)) break;
    rows.push(cellsOf(line));
    i += 1;
  }
  return [{ type: "table", align, header, rows }, i];
}

export function parseMarkdown(source: string): MdBlock[] {
  const lines = source.replace(/\r\n?/g, "\n").replace(/\t/g, "    ").split("\n");
  const blocks: MdBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (isBlank(line)) {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1] as string;
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] as string).trimStart().startsWith(marker)) {
        body.push(lines[i] as string);
        i += 1;
      }
      blocks.push({ type: "code", text: body.join("\n") });
      i += 1; // the closing fence (or the end)
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading && indentOf(line) < 4) {
      const level = (heading[1] as string).length as 1 | 2 | 3 | 4 | 5 | 6;
      blocks.push({ type: "heading", level, children: parseInline(heading[2] as string) });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ type: "rule" });
      i += 1;
      continue;
    }
    const footnote = FOOTNOTE_DEF.exec(line);
    if (footnote) {
      let text = footnote[2] as string;
      i += 1;
      while (i < lines.length && !isBlank(lines[i]) && indentOf(lines[i] as string) >= 2) {
        text += ` ${(lines[i] as string).trim()}`;
        i += 1;
      }
      blocks.push({ type: "footnoteDef", label: footnote[1] as string, children: parseInline(text) });
      continue;
    }
    if (isTableStart(lines, i)) {
      const [table, after] = parseTable(lines, i);
      blocks.push(table);
      i = after;
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const [list, after] = parseList(lines, i);
      blocks.push(list);
      i = after;
      continue;
    }
    const paragraph: string[] = [line.trim()];
    i += 1;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i] as string) && !isTableStart(lines, i)) {
      paragraph.push((lines[i] as string).trim());
      i += 1;
    }
    blocks.push({ type: "paragraph", children: parseInline(paragraph.join(" ")) });
  }
  return blocks;
}

/** The plain text of an inline tree (for headings, titles and tests). */
export function inlineText(nodes: readonly MdInline[]): string {
  return nodes
    .map((node) => {
      switch (node.type) {
        case "text":
        case "code":
          return node.text;
        case "strong":
        case "link":
          return inlineText(node.children);
        case "footnoteRef":
          return `[^${node.label}]`;
      }
    })
    .join("");
}

/** Footnote labels in order of first reference. */
export function footnoteLabels(blocks: readonly MdBlock[]): string[] {
  const seen: string[] = [];
  const visit = (nodes: readonly MdInline[]) => {
    for (const node of nodes) {
      if (node.type === "footnoteRef" && !seen.includes(node.label)) seen.push(node.label);
      else if (node.type === "strong" || node.type === "link") visit(node.children);
    }
  };
  const visitList = (list: MdList) =>
    list.items.forEach((item) => {
      visit(item.children);
      item.sublists.forEach(visitList);
    });
  for (const block of blocks) {
    if (block.type === "list") visitList(block);
    else if (block.type === "heading" || block.type === "paragraph") visit(block.children);
    else if (block.type === "table") [block.header, ...block.rows].forEach((row) => row.forEach(visit));
  }
  return seen;
}
