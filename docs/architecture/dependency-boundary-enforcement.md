# Static architectural dependency checks

The [accepted modular-monolith ADR](modular-monolith-boundaries.md) is enforced
by [the architecture tests](../../tests/test_architecture_boundaries.py), added
for [#133](https://github.com/DevStarrk1137/ayvu/issues/133). This enforcement
does not accept other proposed decisions or change production behavior.

Run the focused checks with:

```bash
uv run pytest tests/test_architecture_boundaries.py
uv run pytest
git diff --check
```

## Classification and rules

`OWNERS` lists every production module by exact module name, logical layer and
capability. `ALLOWED_INTERNAL_EDGES` separately lists every reviewed import
between production modules. Classification is reviewed against behavior and ADR
ownership; directory names do not confer permission. New modules, including
package initializers, require explicit classification and every new internal
edge requires an exact allowlist entry. Missing modules and stale allowlist
entries fail, so a rename cannot silently discard protection.

| Modules | Layer | Capability |
| --- | --- | --- |
| `ayvu` | Independent package surface | Package metadata |
| `domain`, `chunking` | Domain | Shared values, Translation |
| `translation_memory` | Transitional domain | Knowledge/TM |
| `preflight`, `library` | Transitional application | Translation, Library |
| `cli`, `cli_progress` | Interface | CLI |
| `epub_io`, `html_translate`, `validation` | Format adapter | EPUB, EPUB/Quality |
| `translator` | Provider adapter with transitional embedded port | Providers |
| `cache`, `glossary` | Knowledge adapter | Exact cache, Glossary |
| `config`, `resume`, `review_export`, `review_import` | Infrastructure | Preferences, Jobs, Review |

Application imports domain/application contracts; domain imports domain APIs.
The internal graph must be acyclic. Interface imports application/domain values
and presenters from the same interface. Format, provider, knowledge and
infrastructure adapters can import application/domain ports they implement.
Format and provider adapters cannot import one another. Knowledge adapters
cannot import interfaces, formats or infrastructure. Infrastructure cannot
import interfaces or formats. Package initializers remain independent of
functional modules to prevent reexports from hiding concrete adapters.
The layer matrix is an upper bound: it cannot authorize an import without an
exact module edge in `ALLOWED_INTERNAL_EDGES`. An allowed edge cannot override a
forbidden layer direction.

External package roots are explicitly recognized without importing or requiring
their installation. CLI presenters can import Typer/Rich; Desktop presenters
can import recognized Qt bindings. Formats can import ebooklib/Beautiful
Soup/lxml; providers can import requests. Domain/application cannot import any
recognized vendor library. Concrete standard-library SQL, subprocess, HTTP,
XML, archive, CSV, filesystem, socket and persistent-store libraries are also
rejected in inward layers and interfaces. Standard-library presentation
libraries (including tkinter/curses) are restricted to interfaces/composition.
Neutral values such as `pathlib.Path` remain compatible with the
existing ADR baseline. Current mixed modules use exact exceptions instead of a
permissive legacy layer.

## Temporary exceptions

`EXCEPTIONS` is the authoritative reviewed list. Every entry identifies an exact
source/destination module edge, capability owner, positive removal issue number,
and observable expiry condition. Wildcards, incomplete entries, and exceptions
that no longer waive a forbidden edge fail. Exceptions cannot waive unknown
imports, syntax/read errors, cycles or missing ownership.

The existing CLI edges expire as application use cases are extracted in #128,
#145, #146, #149, #150, #152, #157 and composition is consolidated in #158.
Preflight exceptions expire in #145. EPUB/HTML pipeline coupling expires in
#143/#144, after #141 separates the provider contract from its HTTP adapter.
Review coupling expires in #149. Fuzzy memory's exact-cache and vendor coupling
expires in #161. Library configuration/process coupling expires in #151.
The exact edge and condition for each of these are recorded individually in
the test data; these references do not imply that those issues are completed.

To request an exception, explain the existing mixed responsibility, add only
the exact edge, name the capability owner and removal issue, specify when the
edge disappears, and obtain normal human change review. A new architecture
choice needs its own decision review. When an extraction removes an edge,
remove its exception in the same change. Never exempt an entire layer, package
prefix or future family of modules.

## Evidence and limits

The analyzer parses every production Python file with `ast`; it does not execute
fixtures, production modules, Qt, or network clients. Absolute, relative,
multilevel and aliased imports resolve to exact module edges and are tested
directly. Imports inside functions and `TYPE_CHECKING` blocks are checked too. Star imports, unknown
internal modules/vendor roots, unreadable files/directories, invalid syntax,
symlinks and ambiguous module/package names fail closed.

[Negative fixtures](../../tests/fixtures/architecture_boundaries/negative.json)
contain synthetic source snippets and expected rejection categories. They are
written to temporary complete package trees and scanned by the same analyzer
as `src/ayvu`. Further tests cover alias equivalence, relative resolution,
package reexports, missing ownership, cycles, read failures and exception expiry.
The normal suite needs only the existing dev dependencies, with no Desktop
extra or new runtime framework.

Static imports cannot prove that an adapter respects product permissions,
consent, retention, or fallback policy; that composition contains only
construction; or that values never carry concrete framework objects. They also
cannot detect computed imports, reflective loading, runtime plugin discovery,
or all attribute access hidden behind a module alias. An allowed module edge
also cannot prove that every imported symbol is a port rather than a concrete
implementation; public API review and contract tests remain required. Such
behavior still requires code review and the relevant use-case, policy and
security tests.
No passing import check constitutes a sandbox or proves full ADR compliance.
