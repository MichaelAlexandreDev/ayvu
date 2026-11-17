---
name: review-ayvu-change
description: Review an Ayvu diff or issue implementation for correctness, architecture, tests, security, privacy, and scope. Use for pre-commit reviews, issue completion audits, or regression analysis. Do not modify files unless the user separately asks for fixes.
---

# Review Ayvu Change

Perform an evidence-first review. Findings are the primary output; a green test
suite alone is not proof of completion.

## Setup

1. Read AGENTS.md, the live issue when one is in scope, and the contracts of the
   modules the diff touches (`docs/architecture/modules/`).
2. Capture branch, base, status, and the exact diff. Preserve unrelated
   preexisting changes.
3. Read references/review-lenses.md.
4. Read the changed production code, related tests, and relevant architecture
   sections. Inspect user documentation when behavior changes.

Keep the review read-only. Do not edit the issue, files, branch, or GitHub state.

## Independent review

For a non-trivial code change or a cross-boundary issue, delegate up to three
independent read-only passes and wait for all of them:

- ayvu_architect for boundaries, contracts, migrations, and compatibility;
- ayvu_test_reviewer for correctness evidence and test gaps;
- ayvu_security_reviewer for filesystem, network, secrets, privacy, and
  untrusted-content risks.

Use ayvu_issue_auditor instead of one of those when issue readiness or dependency
evidence is the main uncertainty. Give each agent the live issue, exact diff or
branch comparison, and minimum relevant artifacts. Do not tell it the expected
conclusion. If custom agents are unavailable, perform the same lenses locally.

## Output

Report findings first, ordered by severity:

- Blocker: violates the issue contract, an invariant, data integrity, security,
  or a hard dependency.
- High: likely correctness or compatibility regression.
- Medium: meaningful missing case, weak evidence, or maintainability risk.
- Low: limited issue that is still worth fixing.

For every finding, cite the file and line, explain the failure scenario and
impact, and state the smallest useful correction or missing test. Separate
findings from open questions and residual test gaps.

When there are no findings, say so explicitly and still list tests executed,
skips, and remaining coverage limits. Do not claim the issue complete unless
every acceptance criterion and required gate has direct evidence.
