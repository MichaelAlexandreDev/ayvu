# Ayvu issue architecture

## Focused issue template

Use an English, imperative, outcome-oriented title, prefixed with the series and a
local number when the issue belongs to a series (for example `REF`, `AI`):

```text
[SERIES-NN] Verb-led specific outcome
```

Use this exact body order:

```markdown
## Context

State verified current behavior, the problem, constraints, and why it matters.
Label proposed behavior as an enhancement.

## Objective

State one observable outcome.

## Scope

- Concrete implementation boundary.
- Reused modules/interfaces/formats.
- Required states and documentation.

## Out of scope

- Adjacent work intentionally excluded.
- Capabilities owned by other issues.
- Existing behavior that must not change.

## Technical requirements

- Real files, interfaces, formats, and invariants.
- Security, privacy, concurrency, memory, and compatibility constraints.
- Error, progress, cancellation, and persistence behavior when applicable.

## Acceptance criteria

- [ ] Binary observable criterion.
- [ ] Edge/failure/compatibility criterion.
- [ ] Automated-test or measurable-budget criterion.
- [ ] Documentation criterion when applicable.

## Tests required

- Unit: named contracts and edge cases.
- Integration: real boundaries with fakes only for external services.
- GUI/E2E/platform: concrete workflow where applicable.

## Dependencies

None.

## Suggested branch

`feat/unique-kebab-case-name`

## Priority

`P0 — Blocking`

## Estimate

`M`

## Recommended model

- Codex: `GPT-5.6 Sol` (`medium`)
- Why: Risk-based reason.

## Labels

`desktop`, `topic`, `priority:p0`
```

## Branches

- `feat/` for implementation;
- `fix/` for defects;
- `refactor/` for behavior-preserving restructuring;
- `test/` when the primary deliverable is executable test enforcement;
- `docs/` for documentation-only work;
- `research/` for time-boxed decisions or measurement;
- `build/` or `ci/` for packaging/build or CI work.

The branch type must match the actual diff. Keep names unique and short.

## Dependencies

Add only hard prerequisites. In sequential roadmaps, depend only on earlier IDs.
Open related work is a coordination reference unless its output is required to
implement this issue. Never recreate a contract owned by an open dependency.

## Priority and estimate

- P0: architecture, security, core execution, or release blocker.
- P1: release-required but not foundational.
- P2: important enhancement or quality improvement.
- P3: optional polish.
- XS/S/M/L: one reviewable pull request of increasing size.
- XL: split before approval.

## Codex model selection

Recommend exactly one model that is currently available in Codex:

- `GPT-5.6 Sol` for complex professional coding, architecture, security, or
  high-stakes review where capability is the primary constraint;
- `GPT-5.6 Terra` when the task needs strong cross-module reasoning with a better
  balance of capability, latency, and cost;
- `GPT-5.6 Luna` for bounded, repetitive, or high-volume work where efficiency is
  important and the acceptance criteria are especially concrete.

Choose reasoning effort separately from the model. Start at `medium`; use
`high`, `xhigh`, `max`, or `ultra` only when the issue's risk or measured quality
gain justifies it, and use `low` only for tightly bounded work. Recheck both the
active Codex selector and the
[official OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)
before publishing because availability can change.

## Review checklist

- Current behavior is supported by code/tests/help, not memory.
- Objective has one outcome and scope fits one PR.
- Out of scope prevents overlap.
- Technical references exist.
- Criteria are binary and tests name behavior and level.
- Dependencies exist, are hard, and are acyclic.
- Branch is unique and type-correct.
- Credentials, long-running UI work, files, and resources have safe boundaries.
- Publication has separate explicit authorization.
- The Codex model and effort were rechecked against current official selectors/docs;
  do not copy this example indefinitely without verification.
