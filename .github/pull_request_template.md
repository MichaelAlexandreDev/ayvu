## Objective

<!-- One or two sentences: what this PR achieves and why. -->

## What changes

-

## Out of scope

-

## Validation

- [ ] `scripts/check-fast.sh`
- [ ] `scripts/check.sh` (the full suite, plainly and with `GITHUB_ACTIONS=true`)
- [ ] Docker stage (`scripts/docker-tests.sh`), for behavioural changes
- [ ] Comparisons with `main` through real CLI runs, when `src/` changes user-visible behaviour

## Related issue

Refs #

## Review checklist

See [where new code goes](../docs/architecture/cross-cutting/where-code-goes.md#pr-checklist).

- [ ] The code lives in the module that owns the rule, through that module's public API and layer.
- [ ] Existing rules and implementations were reused; nothing was duplicated.
- [ ] Every split follows a reason to change, never only the size limit.
- [ ] The PR is mechanical or behavioural, never both (D13).
- [ ] Persisted identities did not change, or there is a migration, a new version and an explicit decision (D9).
- [ ] Documentation and the CHANGELOG are updated where they apply.
