---
name: ayvu-place-code
description: Decide where new or moved Ayvu code belongs (layer, owning module, public API, registrations) before writing it. Use before creating a module, moving code, splitting a file for the size ratchet, or adding an import across packages. Do not use for edits inside one existing function.
---

# Place Ayvu Code

Decide the owner first; write second. The architecture tests and `import-linter` reject a wrong
placement late; this skill catches it before the first line.

## Steps

1. **Name the rule.** State in one sentence what the code decides or does (a rule, an adapter, a use
   case, presentation). Rules belong to the module that owns the concept, never to the caller that needs it.
2. **Find the owner.** Use `graphify query "<concept>"`, then the layer map in
   `docs/architecture/cross-cutting/where-code-goes.md`:
   - presentation (prompts, tables, messages) → `interfaces/cli` or `interfaces/desktop`;
   - selecting or constructing concrete adapters → `composition` (builders); the CLI gets a named
     factory from `ayvu.composition.cli`;
   - use cases and orchestration → the owning module's application layer (`translation`, `review`,
     `recovery`, `formats.use_cases`, ...);
   - I/O adapters (SQLite, files, HTTP) → the owning module's store or adapter module;
   - values used by at least three modules and importing nothing → `kernel` (D4).
   Never `v1` (D7).
3. **Check the direction.** The new module may import only lower layers and same-module code (D3).
   If it needs something above it, the design is wrong: inject a port from composition instead.
4. **Use public APIs.** Import other packages through their `__init__` exports (D2). If another package
   must import the new module directly, it is an adapter module and goes into `ADAPTER_MODULES`.
5. **Size by responsibility.** Files up to 400 lines and functions up to 80 (D10). A ratchet-listed file
   may not grow: extract a cohesive piece with a reason to change, in its own commit, never squeeze lines.
6. **Moves are mechanical.** A move updates every importer in the same change and leaves no re-export
   (D8), and does not change behaviour in the same commit (D13). Use `git mv` and
   `scripts/size_ratchet.py --move OLD NEW` for ratchet-listed files.
7. **Register the new module:**
   - an owner in `tests/support/architecture_owners.py`;
   - its import edges in `tests/support/architecture_*edges.py`;
   - `ADAPTER_MODULES` when step 4 applies;
   - a line in its module contract under `docs/architecture/modules/`.
8. **Prove it.** Run `scripts/check-fast.sh`; its failures name what is missing.

## Output

Before editing, report the owner, the layer, the imports the code will need, and the registrations
step 7 requires. Then follow the approval gate of the calling workflow.
