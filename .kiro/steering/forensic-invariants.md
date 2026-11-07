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

## 2. Deterministic Confidence Score Formula $C(f) \in [0.0, 1.0]$
Implemented in `backend/apps/genome/scoring.py`:
$$C(f) = \sigma \left( \sum_{i=1}^{N} w_i \cdot s(f, e_i) \cdot e^{-\lambda \Delta t_i} - \gamma \cdot \text{Conflict}(f) \right)$$
Where:
- $\sigma(z) = \frac{1}{1 + e^{-z}}$ is the standard logistic sigmoid function mapped to $[0.0, 1.0]$.
- Authority weights $w_i$: `AST_IMPLEMENTATION = 1.2`, `UNMERGED_BRANCH_COMMIT = 0.9`, `ISSUE_SPEC = 0.6`, `TODO_COMMENT = 0.45`, `README_CLAIM = 0.35`.
- $s(f, e_i) \in [0.0, 1.0]$: Relevance score (`1.0` for direct symbol/AST match, `0.8` for branch diff match, `0.6` for text mention).
- $\lambda = 0.0015$ (temporal decay constant) and $\Delta t_i \ge 0$ is commit age in days relative to the repo's latest commit (`0` for uncommitted docs).
- $\gamma = 0.40$, and $\text{Conflict}(f) = 1.0$ if a `ConflictRecord` exists for feature $f$ (else `0.0`).

## 3. Epistemic Status Thresholds
- `CONFLICTED`: If $\text{Conflict}(f) == 1.0$.
- `KNOWN`: $C(f) \ge 0.75$ and no conflict.
- `INFERRED`: $0.45 \le C(f) < 0.75$.
- `MISSING_CONTEXT`: $C(f) < 0.45$.

## 4. Bounded Revival Strategy Feasibility Score Formula $\text{Score}(S_k) \in [0.0, 100.0]$
Implemented in `backend/apps/revival/scoring.py`:
$$\text{Score}(S_k) = \text{round}\Big(100 \cdot \big(\alpha \cdot V(S_k) + \beta \cdot (1.0 - D(S_k)) + \delta \cdot (1.0 - R(S_k))\big), 1\Big)$$
Where:
- Weights sum to $1.0$: $\alpha = 0.50$, $\beta = 0.30$, $\delta = 0.20$.
- $V(S_k) \in [0.0, 1.0]$: Recovered product value ratio (fraction of `IntentNode` items brought to `VERIFIED_WORKING`).
- $D(S_k) \in [0.0, 1.0]$: Technical debt remediation cost ($\min(1.0, \text{files\_to\_modify} / 15.0)$).
- $R(S_k) \in [0.0, 1.0]$: Dependency breakage risk (`0.25` for `RESTORE`, `0.60` for `MODERNIZE`, `0.35` for `EXTRACT`).
- Because $\alpha + \beta + \delta = 1.0$ and all inputs are bounded in $[0.0, 1.0]$, $\text{Score}(S_k)$ is strictly guaranteed to lie within $[0.0, 100.0]$.
