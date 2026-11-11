/**
 * Recorded PayPulse ("Patient Zero") data for the offline demo mode.
 *
 * Values come from the backend's deterministic results, asserted in
 * backend/tests/test_mcp_server.py, test_genome_reconciler.py and test_revival_planner.py
 * (design.md, "PayPulse genome" and "PayPulse" strategy tables): six features with their exact
 * C(f), the three README conflicts at README.md:10, :11 and :12, and the abandoned branch
 * `feat/webhook-retry-engine` (ahead 4, behind 5, dormant 543 days).
 *
 * Node, edge and citation counts match the real genome. The `citations` lists are an abridged,
 * representative sample of the real ones (exact files, lines and snippets from the fixture);
 * `citation_count` keeps the real total.
 */
import type {
  BranchSummaryOut,
  CitationOut,
  ConflictOut,
  FeatureNode,
  GenomeGraphOut,
  ScoreBreakdownOut,
} from "../types";

export const MOCK_PROJECT_ID = "0b5e7d1a-6c2f-4e8a-9f3d-2a1c0de0a001";
export const MOCK_PROJECT_NAME = "PayPulse (Patient Zero)";
export const MOCK_DEFAULT_BRANCH = "main";
export const MOCK_MAIN_HEAD = "047fc3e28b95ec860e91089679aa7e01d04fc87a";
export const MOCK_BRANCH_NAME = "feat/webhook-retry-engine";
export const MOCK_BRANCH_HEAD = "93ec6384c39828ad660e316997371eabac417977";

const F = {
  ledger: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0101",
  invoices: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0102",
  webhooks: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0103",
  retries: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0104",
  currency: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0105",
  pdf: "3f0c9a52-1b7e-4d3a-8c61-0a9e2b7d0106",
} as const;
export const MOCK_FEATURE_IDS = F;

const BRANCH_NODE_ID = `branch:${MOCK_BRANCH_NAME}`;
const README_NODE_ID = "document:README.md";

function feature(
  id: string,
  position: { x: number; y: number },
  data: Omit<FeatureNode["data"], "feature_id" | "color_token">,
): FeatureNode {
  const color_token =
    data.vital_status === "VERIFIED_WORKING" ? "alive" : data.vital_status === "ABANDONED_IN_PROGRESS" ? "dormant" : "ghost";
  return { id, type: "feature", position, data: { ...data, feature_id: id, color_token } };
}

export const MOCK_FEATURE_NODES: FeatureNode[] = [
  feature(F.ledger, { x: 0, y: 0 }, {
    feature_key: "claim:double-entry-ledger-with-balanced-atomic-postings",
    title: "Double-entry ledger with balanced, atomic postings",
    category: "CORE_FEATURE",
    vital_status: "VERIFIED_WORKING",
    epistemic_status: "KNOWN",
    confidence_score: 0.8594,
    reconstructed_intent:
      "A double-entry ledger where every transaction posts balanced debits and credits atomically, as README.md:8 claims.",
    actual_implementation_state:
      "Implemented on main in paypulse/ledger.py (Ledger.post_transaction validates balance before touching any account) and covered by tests/test_ledger.py.",
    citation_count: 9,
    conflict_count: 0,
  }),
  feature(F.invoices, { x: 0, y: 180 }, {
    feature_key: "claim:invoices-with-line-items-tax-and-a-draft-issued-paid-void-workflow",
    title: "Invoices with line items, tax and a draft/issued/paid/void workflow",
    category: "CORE_FEATURE",
    vital_status: "VERIFIED_WORKING",
    epistemic_status: "KNOWN",
    confidence_score: 0.8594,
    reconstructed_intent: "Invoices with line items, tax and a status workflow that post to the ledger.",
    actual_implementation_state: "Implemented on main in paypulse/invoices.py and covered by tests/test_invoices.py.",
    citation_count: 9,
    conflict_count: 0,
  }),
  feature(F.webhooks, { x: 0, y: 360 }, {
    feature_key: "module:paypulse/webhooks.py",
    title: "Outbound webhooks for invoice lifecycle events",
    category: "INTEGRATION",
    vital_status: "VERIFIED_WORKING",
    epistemic_status: "KNOWN",
    confidence_score: 0.8665,
    reconstructed_intent: "Signed outbound webhooks for invoice events (module paypulse/webhooks.py).",
    actual_implementation_state:
      "WebhookDispatcher signs and delivers payloads on main; delivery is a single attempt (FIXME at paypulse/webhooks.py:70).",
    citation_count: 9,
    conflict_count: 0,
  }),
  feature(F.retries, { x: 360, y: 0 }, {
    feature_key: "claim:automated-exponential-backoff-webhook-retries",
    title: "Automated Exponential Backoff Webhook Retries",
    category: "INTEGRATION",
    vital_status: "ABANDONED_IN_PROGRESS",
    epistemic_status: "CONFLICTED",
    confidence_score: 0.7951,
    reconstructed_intent:
      "Retry failed webhook deliveries (network errors and 5xx) with exponential backoff, optional jitter and a dead-letter list.",
    actual_implementation_state:
      "About 80% of the retry engine lives on the unmerged branch feat/webhook-retry-engine (paypulse/retry.py), dormant 543 days; main still delivers once and its spec test fails.",
    citation_count: 15,
    conflict_count: 1,
  }),
  feature(F.currency, { x: 360, y: 180 }, {
    feature_key: "claim:multi-currency-conversion-usd-eur-gbp-with-daily-ecb-rates",
    title: "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates",
    category: "CORE_FEATURE",
    vital_status: "ABANDONED_IN_PROGRESS",
    epistemic_status: "CONFLICTED",
    confidence_score: 0.898,
    reconstructed_intent: "Bill invoices in USD, EUR or GBP using cached ECB daily reference rates.",
    actual_implementation_state:
      "currency.convert() raises NotImplementedError for any cross-currency pair (paypulse/currency.py:36-43); quantize and normalize_code work.",
    citation_count: 12,
    conflict_count: 1,
  }),
  feature(F.pdf, { x: 720, y: 0 }, {
    feature_key: "claim:pdf-invoice-export",
    title: "PDF Invoice Export",
    category: "CORE_FEATURE",
    vital_status: "GHOST_FEATURE",
    epistemic_status: "CONFLICTED",
    confidence_score: 0.4182,
    reconstructed_intent: "Render issued invoices to PDF (ReportLab) with a payment QR code, per docs/architecture-notes.md.",
    actual_implementation_state: `No implementation found on main at ${MOCK_MAIN_HEAD.slice(0, 12)} or on any unmerged branch.`,
    citation_count: 2,
    conflict_count: 1,
  }),
];

function citation(
  featureId: string,
  index: number,
  fields: Omit<CitationOut, "id" | "feature_id" | "artifact_id" | "signal_index">,
): CitationOut {
  return {
    id: `${featureId.slice(0, 8)}-c${index}`,
    feature_id: featureId,
    artifact_id: null,
    signal_index: index,
    ...fields,
  };
}

const onMain = { commit_sha: MOCK_MAIN_HEAD, branch_name: MOCK_DEFAULT_BRANCH, is_branch_only: false } as const;
const onBranch = { commit_sha: MOCK_BRANCH_HEAD, branch_name: MOCK_BRANCH_NAME, is_branch_only: true } as const;

export const MOCK_CITATIONS: Record<string, CitationOut[]> = {
  [F.ledger]: [
    citation(F.ledger, 0, {
      ...onMain,
      source_type: "AST_IMPLEMENTATION",
      file_path: "paypulse/ledger.py",
      line_start: 156,
      line_end: 191,
      snippet:
        "def post_transaction(\n    self,\n    description: str,\n    entries: Iterable[Entry],\n    posted_at: Optional[datetime] = None,\n) -> Transaction:\n    posted = tuple(entries)\n    if len(posted) < 2:\n        raise UnbalancedTransactionError(\"a transaction needs at least two entries\")",
      weight: 1.2,
    }),
    citation(F.ledger, 1, {
      ...onMain,
      source_type: "README_CLAIM",
      file_path: "README.md",
      line_start: 8,
      line_end: 8,
      snippet: "- [x] Double-entry ledger with balanced, atomic postings",
      weight: 0.21,
    }),
  ],
  [F.invoices]: [
    citation(F.invoices, 0, {
      ...onMain,
      source_type: "AST_IMPLEMENTATION",
      file_path: "paypulse/invoices.py",
      line_start: 37,
      line_end: 111,
      snippet: "class Invoice:",
      weight: 1.2,
    }),
    citation(F.invoices, 1, {
      ...onMain,
      source_type: "README_CLAIM",
      file_path: "README.md",
      line_start: 9,
      line_end: 9,
      snippet: "- [x] Invoices with line items, tax and a draft/issued/paid/void workflow",
      weight: 0.21,
    }),
  ],
  [F.webhooks]: [
    citation(F.webhooks, 0, {
      ...onMain,
      source_type: "AST_IMPLEMENTATION",
      file_path: "paypulse/webhooks.py",
      line_start: 42,
      line_end: 76,
      snippet: "class WebhookDispatcher:",
      weight: 1.2,
    }),
    citation(F.webhooks, 1, {
      ...onMain,
      source_type: "TODO_COMMENT",
      file_path: "paypulse/webhooks.py",
      line_start: 70,
      line_end: 71,
      snippet:
        "# FIXME: single attempt only. The exponential backoff retry engine lives on\n# feat/webhook-retry-engine and still has to be wired into this dispatcher.",
      weight: 0.45,
    }),
  ],
  [F.retries]: [
    citation(F.retries, 0, {
      ...onBranch,
      source_type: "UNMERGED_BRANCH_COMMIT",
      file_path: "paypulse/retry.py",
      line_start: null,
      line_end: null,
      snippet: "@@ new file: paypulse/retry.py @@\n+class RetryPolicy:\n+    # TODO: complete backoff jitter and wire into dispatcher",
      weight: 0.3189,
    }),
    citation(F.retries, 1, {
      ...onMain,
      source_type: "AST_IMPLEMENTATION",
      file_path: "tests/test_webhooks.py",
      line_start: 44,
      line_end: 59,
      snippet:
        "def test_dispatch_retries_with_exponential_backoff():\n    # Spec from docs/architecture-notes.md: retry 5xx responses, doubling the delay.\n    transport = RecordingTransport([503, 503, 200])",
      weight: 1.2,
    }),
    citation(F.retries, 2, {
      ...onMain,
      source_type: "ISSUE_SPEC",
      file_path: "docs/architecture-notes.md",
      line_start: 15,
      line_end: 30,
      snippet:
        "## Webhook retry engine\n\nReceivers go down. A delivery that hits a network error or a 5xx response is\nretried with exponential backoff:",
      weight: 0.36,
    }),
    citation(F.retries, 3, {
      ...onMain,
      source_type: "README_CLAIM",
      file_path: "README.md",
      line_start: 11,
      line_end: 11,
      snippet: "- [x] Automated Exponential Backoff Webhook Retries",
      weight: 0.21,
    }),
  ],
  [F.currency]: [
    citation(F.currency, 0, {
      ...onMain,
      source_type: "AST_IMPLEMENTATION",
      file_path: "paypulse/currency.py",
      line_start: 36,
      line_end: 43,
      snippet:
        "def convert(amount: Decimal, from_currency: str, to_currency: str) -> Decimal:\n    \"\"\"Convert amount between currencies. Only same-currency conversion works.\"\"\"\n    source = normalize_code(from_currency)\n    target = normalize_code(to_currency)\n    if source == target:\n        return quantize(amount, target)\n    # TODO: fetch daily FX rates (ECB reference feed) and support EUR/GBP invoicing.\n    raise NotImplementedError(f\"currency conversion {source}->{target} is not implemented\")",
      weight: 0.36,
    }),
    citation(F.currency, 1, {
      ...onMain,
      source_type: "README_CLAIM",
      file_path: "README.md",
      line_start: 10,
      line_end: 10,
      snippet: "- [x] Multi-currency conversion (USD, EUR, GBP) with daily ECB rates",
      weight: 0.21,
    }),
  ],
  [F.pdf]: [
    citation(F.pdf, 0, {
      ...onMain,
      source_type: "ISSUE_SPEC",
      file_path: "docs/architecture-notes.md",
      line_start: 32,
      line_end: 36,
      snippet:
        "## PDF invoice export\n\n`Invoice.to_pdf()` renders an issued invoice to PDF through\n`paypulse/export/pdf.py` (ReportLab).",
      weight: 0.36,
    }),
    citation(F.pdf, 1, {
      ...onMain,
      source_type: "README_CLAIM",
      file_path: "README.md",
      line_start: 12,
      line_end: 12,
      snippet: "- [x] PDF Invoice Export",
      weight: 0.21,
    }),
  ],
};

function conflict(
  featureId: string,
  line: number,
  text: string,
  reality: string,
  description: string,
  recommendation: string,
): ConflictOut {
  const id = `${featureId.slice(0, 8)}-x${line}`;
  const claimCitation = MOCK_CITATIONS[featureId]?.find((c) => c.source_type === "README_CLAIM") ?? null;
  return {
    id,
    feature_id: featureId,
    claim_source: `README.md:${line}: "[x] ${text}"`,
    reality_source: reality,
    conflict_description: description,
    resolution_recommendation: recommendation,
    created_at: "2026-01-01T00:00:00Z",
    claim_citation_id: claimCitation?.id ?? null,
    claim_file_path: "README.md",
    claim_line_start: line,
    claim_line_end: line,
    document_node_id: README_NODE_ID,
    edge_id: `conflict:${id}`,
  };
}

export const MOCK_CONFLICTS: ConflictOut[] = [
  conflict(
    F.retries,
    11,
    "Automated Exponential Backoff Webhook Retries",
    `Unmerged branch ${MOCK_BRANCH_NAME} (paypulse/retry.py, tests/test_retry.py), dormant 543 days; main delivers once.`,
    "README.md marks the retry engine complete, but it only exists on an abandoned branch and main's spec test fails.",
    `Merge ${MOCK_BRANCH_NAME}, finish the jitter TODO and wire RetryPolicy into WebhookDispatcher.`,
  ),
  conflict(
    F.currency,
    10,
    "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates",
    "paypulse/currency.py:36-43 convert() raises NotImplementedError for any cross-currency pair.",
    "README.md marks multi-currency complete, but conversion is a stub.",
    "Complete convert() against an injectable rate source and add tests; defer the live ECB feed.",
  ),
  conflict(
    F.pdf,
    12,
    "PDF Invoice Export",
    `No implementation found on main at ${MOCK_MAIN_HEAD.slice(0, 12)} or on any unmerged branch.`,
    "README.md marks PDF export complete, but no code for it exists anywhere in the repository.",
    "Treat PDF export as unbuilt: it needs a PDF and a QR code library before it can be implemented.",
  ),
];

/**
 * The rule that decided each recorded status. These follow from the classifiers alone
 * (scoring.classify_epistemic_status, reconciler.classify_vital): a conflict always decides
 * CONFLICTED; retries has unmerged-branch code and no head stub; currency's convert() is a head stub.
 */
const VITAL_REASON: Record<string, string> = {
  [F.ledger]: "HEAD_IMPLEMENTATION",
  [F.invoices]: "HEAD_IMPLEMENTATION",
  [F.webhooks]: "HEAD_IMPLEMENTATION",
  [F.retries]: "BRANCH_ONLY",
  [F.currency]: "HEAD_STUB",
  [F.pdf]: "NO_IMPLEMENTATION",
};

function breakdown(node: FeatureNode): ScoreBreakdownOut {
  return {
    confidence: node.data.confidence_score,
    z: Math.log(node.data.confidence_score / (1 - node.data.confidence_score)),
    evidence_total: null,
    prior_bias: 0.5,
    conflict: node.data.conflict_count > 0,
    conflict_penalty: node.data.conflict_count > 0 ? 0.4 : 0,
    contributions: [],
    epistemic_status: node.data.epistemic_status,
    status_reason: node.data.epistemic_status === "CONFLICTED" ? "CONFLICT" : "ABOVE_KNOWN_THRESHOLD",
    vital_status: node.data.vital_status,
    vital_status_reason: VITAL_REASON[node.id] ?? null,
    feature_key: node.data.feature_key,
    origin: null,
    matched_evidence_count: node.data.citation_count,
    citations: [],
    parameters: {},
  };
}

export const MOCK_BRANCHES: BranchSummaryOut[] = [
  {
    name: MOCK_DEFAULT_BRANCH,
    head_sha: MOCK_MAIN_HEAD,
    last_commit_date: "2023-01-18T11:39:00Z",
    is_default: true,
    is_merged: true,
    is_abandoned: false,
    commits_ahead: 0,
    commits_behind: 0,
    dormancy_days: 0,
    node_id: null,
  },
  {
    name: MOCK_BRANCH_NAME,
    head_sha: MOCK_BRANCH_HEAD,
    last_commit_date: "2021-07-24T05:50:00Z",
    is_default: false,
    is_merged: false,
    is_abandoned: true,
    commits_ahead: 4,
    commits_behind: 5,
    dormancy_days: 543,
    node_id: BRANCH_NODE_ID,
  },
];

export function buildMockGenome(status: GenomeGraphOut["project"]["status"] = "AUTOPSY_COMPLETE"): GenomeGraphOut {
  return {
    project: {
      id: MOCK_PROJECT_ID,
      name: MOCK_PROJECT_NAME,
      status,
      status_detail: "",
      default_branch: MOCK_DEFAULT_BRANCH,
    },
    synthesis: {
      id: "9d1c4b7e-2f3a-4c5d-8e6f-7a8b9c0d1e2f",
      mode: "DETERMINISTIC",
      model_id: "",
      started_at: "2026-01-01T00:00:04Z",
      finished_at: "2026-01-01T00:00:10Z",
      catalog_size: 214,
      node_count: 6,
      conflict_count: 3,
      input_digest_prefix: "5f0e3c9a1b2d",
      validation: {
        dropped_citation_count: 0,
        unsupported_citation_count: 0,
        rejected_feature_count: 0,
        dropped_conflict_count: 0,
        vital_status_override_count: 0,
        fallback_reason: null,
      },
    },
    nodes: [
      { id: README_NODE_ID, type: "document", position: { x: -360, y: 60 }, data: {
        file_path: "README.md", conflict_count: 3, feature_ids: [F.retries, F.currency, F.pdf], color_token: "conflict",
      } },
      ...MOCK_FEATURE_NODES,
      { id: BRANCH_NODE_ID, type: "branch", position: { x: 1080, y: 0 }, data: {
        name: MOCK_BRANCH_NAME,
        head_sha: MOCK_BRANCH_HEAD,
        last_commit_date: "2021-07-24T05:50:00Z",
        is_abandoned: true,
        is_merged: false,
        commits_ahead: 4,
        commits_behind: 5,
        dormancy_days: 543,
        citation_count: 6,
        feature_ids: [F.retries],
        color_token: "dormant",
      } },
    ],
    edges: [
      {
        id: `evidence:${F.retries}->${BRANCH_NODE_ID}`,
        source: F.retries,
        target: BRANCH_NODE_ID,
        type: "evidence",
        data: { citation_count: 6, source_types: ["UNMERGED_BRANCH_COMMIT", "TODO_COMMENT"], color_token: "dormant" },
      },
      ...MOCK_CONFLICTS.map((record) => ({
        id: `conflict:${record.id}`,
        source: record.feature_id,
        target: README_NODE_ID,
        type: "conflict" as const,
        data: {
          conflict_id: record.id,
          claim_source: record.claim_source,
          file_path: "README.md",
          line_start: record.claim_line_start,
          line_end: record.claim_line_end,
          claim_citation_id: record.claim_citation_id,
          color_token: "conflict" as const,
        },
      })),
    ],
    citations: MOCK_CITATIONS,
    conflicts: MOCK_CONFLICTS,
    score_breakdowns: Object.fromEntries(MOCK_FEATURE_NODES.map((node) => [node.id, breakdown(node)])),
    branches: MOCK_BRANCHES,
  };
}
