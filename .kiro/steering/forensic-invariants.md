---
inclusion: always
---
# Forensic Invariants & Mathematical Scoring Rules

## 1. Evidence Citation Invariant
No `IntentNode` may be saved without at least one linked `EvidenceCitation` containing these exact fields:
- `source_type` (`AST_IMPLEMENTATION`, `UNMERGED_BRANCH_COMMIT`, `ISSUE_SPEC`, `TODO_COMMENT`, `README_CLAIM`)
- `file_path` (`str`)
- `commit_sha` (`str`, optional for working tree files)
- `line_start` (`int | None`)
- `line_end` (`int | None`)
- `snippet` (`str`)
- `weight` (`float`)

## 2. Deterministic Confidence Score Formula $C(f) \in (0.0, 1.0)$
Implemented in `backend/apps/genome/scoring.py` (pure; every constant lives in `ScoringParameters` / `DEFAULT_PARAMETERS`):

$$\text{term}_i = w_{t(i)} \cdot s(f, e_i) \cdot e^{-\lambda \Delta t_i}$$
$$A_t = \min\Big(\kappa \cdot w_t,\ \sum_{k=1}^{n_t} \rho^{\,k-1} \cdot \text{term}_{t,(k)}\Big) \quad \text{(terms of source type } t \text{, sorted largest first)}$$
$$z = \sum_{t} A_t - \beta_0 - \gamma \cdot \text{Conflict}(f), \qquad C(f) = \text{round}\big(\sigma(z), 4\big)$$
Where:
- $\sigma(z) = \frac{1}{1 + e^{-z}}$ is the standard logistic sigmoid.
- Authority weights $w_t$: `AST_IMPLEMENTATION = 1.2`, `UNMERGED_BRANCH_COMMIT = 0.9`, `ISSUE_SPEC = 0.6`, `TODO_COMMENT = 0.45`, `README_CLAIM = 0.35`.
- $s(f, e_i) \in [0.0, 1.0]$: match strength, supplied by the caller that matched the evidence to the feature (scoring never guesses it). Named constants: `MATCH_DIRECT_SYMBOL = 1.0` (the feature's own symbol / AST node), `MATCH_BRANCH_DIFF = 0.8` (the feature's code in an unmerged branch diff), `MATCH_TEXT_MENTION = 0.6` (prose or a comment names the feature), `MATCH_STUB = 0.3` (a symbol whose body only raises `NotImplementedError`: name and interface, no behavior). A stub signal may not exceed `MATCH_STUB`.
- $\lambda = 0.0015$ per day. $\Delta t_i \ge 0$ is the age, relative to the repository's latest commit, of the **revision at which the evidence was observed**. Evidence observed at the default branch's HEAD (current code, current README) has $\Delta t = 0$ however old the file is, because it is still there now. Evidence found only on an unmerged branch uses that branch tip's dormancy (PayPulse `feat/webhook-retry-engine`: 543 days, decay $e^{-0.8145} = 0.4429$). Decay penalizes stale places, not old-but-current code.
- Per-type diminishing returns: within one source type the strongest item counts fully and the $k$-th strongest counts $\rho^{k-1}$ with $\rho = 0.5$; the type's total is capped at $\kappa \cdot w_t$ with $\kappa = 1.75$ (three full-strength items saturate it). Many weak items of one kind cannot add up to strong evidence, and a module's symbol count cannot inflate the score.
- $\beta_0 = 0.50$ is the prior: an unevidenced feature is doubtful by default, so zero evidence gives $C = \sigma(-0.5) = 0.3775$.
- $\gamma = 0.40$, and $\text{Conflict}(f) = 1.0$ if a `ConflictRecord` exists for feature $f$ (else `0.0`). $C(f)$ is still computed for conflicted features and is always lower than without the conflict.
- Bounds: with the default parameters $z \in [-0.9,\ 5.625]$, so $C(f) \in [0.2891, 0.9964]$, inside the database's $[0, 1]$ check. Classification compares the rounded, stored value.

## 3. Epistemic Status Rules
Checked in order (`StatusReason` records which rule decided):
- `CONFLICTED`: $\text{Conflict}(f) = 1.0$, regardless of everything else.
- `KNOWN`: $C(f) \ge 0.75$ **and** at least one `AST_IMPLEMENTATION` item observed on the default branch that is neither a stub nor a test. Otherwise a score $\ge 0.75$ falls through to the `INFERRED` rule.
- `INFERRED`: $C(f) \ge 0.45$ **and** implementation-bearing evidence: any `AST_IMPLEMENTATION` item (stubs and tests included), any `UNMERGED_BRANCH_COMMIT`, or a detailed spec (an `ISSUE_SPEC` with $s \ge 1.0$, i.e. it names the feature's concrete symbols or interfaces).
- `MISSING_CONTEXT`: everything else. A feature evidenced only by `TODO_COMMENT` / `README_CLAIM` items (or only by spec mentions) is `MISSING_CONTEXT` regardless of count, even when its $C(f)$ exceeds 0.45.
- Section 1 still applies: a persisted `IntentNode` is scored with `score_intent_node`, which refuses an empty evidence list.

## 4. Bounded Revival Strategy Feasibility Score Formula $\text{Score}(S_k) \in [0.0, 100.0]$
Implemented in `backend/apps/revival/scoring.py` (pure; every constant lives in `RevivalScoringParameters` / `DEFAULT_PARAMETERS`). V, D and R are derived from the plan that `backend/apps/revival/planner.py` builds from the genome:
$$\text{Score}(S_k) = \text{round}\Big(100 \cdot \big(\alpha \cdot V(S_k) + \beta \cdot (1.0 - D(S_k)) + \delta \cdot (1.0 - R(S_k))\big), 1\Big)$$
Where:
- Weights sum to $1.0$: $\alpha = 0.50$, $\beta = 0.30$, $\delta = 0.20$.
- $V(S_k) \in [0.0, 1.0]$: projected fraction of features (`IntentNode`s) that are `VERIFIED_WORKING` after the plan.
  - `RESTORE` / `MODERNIZE`: $V = \big(n_\text{verified} + \sum_{f \in \text{in scope}} p_f\big) / n_\text{features}$. Each in-scope target feature counts its feasibility $p_f \in [0, 1]$, the lowest `item_feasibility` among its plan items, so a speculative item never counts as a sure win.
  - `EXTRACT`: $V = n_\text{extracted} / n_\text{verified}$, the extracted cluster's share of the currently verified features.
  - $V = 0$ when the denominator is 0.
  - Item feasibility is the product of named factors (`PlannerParameters`): the kind's base (merge 0.85, complete stub 0.75, fix failing test 0.80, implement from spec 0.50, upgrade 0.90, extract 0.85, add tests 0.90); merge distance $1 / (1 + \text{commits\_behind} / 20)$; open TODO/FIXME markers $\max(0.80, 0.95^n)$; external service 0.60; undeclared dependency 0.70; and $0.90$ per file an extract pulls in from outside its cluster.
- $D(S_k) \in [0.0, 1.0]$: technical debt remediation cost, $\min(1.0, |\text{files\_to\_modify} \cup \text{files\_to\_create}| / 15.0)$ over all plan items.
- $R(S_k) \in [0.0, 1.0]$: dependency breakage risk. The base is `0.25` for `RESTORE`, `0.60` for `MODERNIZE` and `0.35` for `EXTRACT`. Named evidence factors (`RiskFactor`) add $\min(\text{cap}, \text{per\_unit} \cdot \text{count})$ each, and the sum is clamped to $[0, 1]$:
  - `MERGE_DISTANCE` (`RESTORE`, `MODERNIZE`): sum of `commits_behind` of the branches the plan merges, 0.01 each, cap 0.15.
  - `PINNED_UPGRADES` (`MODERNIZE`): exact pins the plan upgrades, 0.025 each, cap 0.20.
  - `EXTRACT_BOUNDARY` (`EXTRACT`): files outside the extracted cluster that its imports pull in, 0.05 each, cap 0.20.
  - With no such evidence (all counts 0), R is exactly the base.
- V, D and R are rounded to 4 decimals before the formula, so the stored `value_score`, `debt_cost_score` and `risk_score` reproduce the stored `feasibility_score`. Rounding is monotone, so the score is non-decreasing in V and non-increasing in D and R.
- Because $\alpha + \beta + \delta = 1.0$ and all inputs are bounded in $[0.0, 1.0]$, $\text{Score}(S_k)$ is strictly guaranteed to lie within $[0.0, 100.0]$.
- Every plan item cites at least one piece of evidence, as section 1 requires of every `IntentNode`: a genome citation id, or the manifest line of a dependency pin.
