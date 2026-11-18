# Product threat register

The threats of the [product threat assessment decision](../decisions/product-threat-model.md) (Accepted). Controls that were proposed are still described as proposed.

## Threat register and implementation ownership

Priority here is assessment priority, not a change to GitHub labels. **High**
means plausible source loss, unintended disclosure, authority escalation or
unbounded local resource use; **Medium** means integrity/privacy impact with a
more constrained trigger. Proposed high-impact paths are gated before exposure.
Every row records prevention (P), detection (D), recovery (R) and validation (V).
Issue links assign proposed implementation/test ownership, not completion.

### T01 — Source overwrite, aliases and partial publication (High)

**B01/B06/B07; Policies + Formats + Infrastructure for current adapters; Artifacts
for future artifact identity, publication and reconciliation, with Provenance
owning lineage records.** A destination equal to the source, or its
symlink/hardlink, can pass existing-output confirmation and be opened
destructively by `_copy_epub_with_replacements`. Concurrent replacement and
interrupted direct writes can also corrupt output/state. This is a
source-inspection finding; normal source-preservation tests do not refute it.

P: reject canonical/identity aliases before mutation, use contained no-follow
access, source revision checks and restricted staging. D: validate the complete
artifact before ready publication. R: preserve prior valid state, reconcile
rename-before-commit explicitly, never claim cross-store atomicity.
V: same-path/symlink/hardlink, substitution race, disk/permission failure and
crash-before/after-rename tests. Owners:
[#134](https://github.com/MichaelAlexandreDev/ayvu/issues/134) for source/project
identity, [#137](https://github.com/MichaelAlexandreDev/ayvu/issues/137) for artifact
reconciliation and [#144](https://github.com/MichaelAlexandreDev/ayvu/issues/144)
for adapter safety. [Job artifacts](../decisions/job-artifacts.md) now cover contained no-clobber
publication and reconciliation, but no current writer uses them yet.
Legacy exposure remains until caller migrations
[#145](https://github.com/MichaelAlexandreDev/ayvu/issues/145),
[#147](https://github.com/MichaelAlexandreDev/ayvu/issues/147) and
[#149](https://github.com/MichaelAlexandreDev/ayvu/issues/149).

### T02 — Hostile archives, imports and parser resource exhaustion (High)

**B02/B05/B07; Formats + Content + Policies.** Crafted ZIP expansion, member
counts, duplicate/canonical names, malformed XML/HTML, oversized review files or
cache-interchange JSON can exhaust RAM/CPU/disk or make references ambiguous.
Legacy review and v2 package readers enforce raw-byte, row, column, cell and
decoded-content budgets; cache import still reads, parses and materializes its
payload before applying rows. The current ZIP path reads members and repackages them; this
is not evidence of host-path Zip Slip. A future extractor must nevertheless
reject traversal before writing any member.

P: bound archive/member/path/expansion/parse budgets and cache-import bytes,
entries and text sizes before expensive reads or materialization; reject
ambiguity, unsupported encryption and invalid structure. D: explicit
resource-limit/corrupt outcomes with opaque locators and no partial import. R:
cancel bounded work and discard only owned staging. V: adversarial tiny synthetic
archives, duplicate names, traversal, malformed container/OPF and cancellation
tests in #144; legacy-v1 cache input/entry/text limits, N/N+1 boundaries and
unchanged-database refusal tests in [#150](https://github.com/MichaelAlexandreDev/ayvu/issues/150);
JSON-v2 envelope and streaming limits in
[#155](https://github.com/MichaelAlexandreDev/ayvu/issues/155);
HTML/parser bounds and structural validation in
[#143](https://github.com/MichaelAlexandreDev/ayvu/issues/143); resource isolation
decision in [#167](https://github.com/MichaelAlexandreDev/ayvu/issues/167).
Threads today provide concurrency, not isolation. #144 alone does not protect
legacy entrypoints; the T01 migration gate also applies here.

### T03 — Unintended egress and confused network authority (High)

**B03/B06; Providers + Policies.** URL choice, edited resume URL, redirect or
ambient proxy could change who receives text in the assessed baseline. #159 now
enforces HTTP loopback/none in `LibreTranslateTranslator`. P: normalize and
authorize the destination before transport, deny redirects and ambient
proxies/credentials in restricted modes, bind future secrets to the destination.
D: sanitized reason codes and denied-transport counters. R: stop with no silent
remote fallback and require a new valid decision. V: zero DNS/socket in offline,
loopback variants, redirects, proxy/environment, malformed URL and error
redaction tests in #159. HTTPS transport alone does not establish purpose,
operator retention or downstream behavior. #379 adds the explicit `remote` policy for remote AI
providers: declared HTTPS hosts only, re-decided redirects, ignored proxies, opaque credentials and a
recorded, revocable consent; its assets, mitigations and residual risk are the
[remote egress entry](../decisions/product-threat-model.md#remote-egress-379).

### T04 — Reflected errors, paths and secret disclosure (High)

**B03/B06/B09; Providers + Interfaces + Policies.** #159 removes URL, raw transport
exception and response body from HTTP errors. HTML statistics and EPUB reports carry
errors to terminal/Markdown; connection errors and checkpoints can retain URL
credentials or identifying paths. P: typed failures with allowlisted diagnostic
fields and opaque references. D: synthetic secret/content sentinels through
repr, event, report and error paths. R: stop exporting affected diagnostics;
revoke compromised credentials outside Ayvu and remove owned copies without
claiming control of third-party copies. V/owners: #159 for HTTP sanitization,
[#138](https://github.com/MichaelAlexandreDev/ayvu/issues/138) for provenance
redaction and #145/#149 for migrated outcomes. A complete diagnostic-bundle
lifecycle remains gated under G03 below; hiding a terminal detail is insufficient.
The [provider contracts](../decisions/provider-contracts.md) classify failures without reading those
messages; HTTP consumers now receive bounded diagnostics as well.

### T05 — Cache poisoning, cross-configuration reuse and covert corpus (High)

**B04; Knowledge + Policies + Provenance.** Cache identity is language pair and
text hash; imported translations are not authenticated and fuzzy lookup reuses
the same store. Reuse can cross provider/pipeline assumptions. Possessing cache,
review or glossary content confers no training/publication rights.
P: versioned complete identity, explicit import conflict decisions and separate
approved TM/corpus stores. D: identity/permission checks and provenance. R:
invalidate scoped derived results, preserve unrelated cache and revoke future
authorized uses. V: provider/profile/pipeline isolation and migration failures in
[#153](https://github.com/MichaelAlexandreDev/ayvu/issues/153),
[#154](https://github.com/MichaelAlexandreDev/ayvu/issues/154),
[#155](https://github.com/MichaelAlexandreDev/ayvu/issues/155); approved-memory
consent/revocation in [#161](https://github.com/MichaelAlexandreDev/ayvu/issues/161);
purpose/rights denial in [#165](https://github.com/MichaelAlexandreDev/ayvu/issues/165).
No present-day cache operation is an automatic contribution to corpus.

### T06 — Review tampering, spreadsheet formulas and active content (High)

**B02/B05/B08; Review + Formats + Interfaces.** A modified legacy CSV may target
the wrong source/revision or contain spreadsheet formulas. The v2 codec now
binds identity/revision, applies reversible formula escaping, and validates
constrained protected-part reconstruction before returning a plan. V1 apply is
rejected. Existing EPUB active markup/assets may still execute or fetch
resources in a reader even when Ayvu skipped them for translation. P: integrate
the v2 package with application export/apply and publication policy. D: v2
rejects foreign IDs, unknown placeholders, structural changes and oversized
rows. R: parsing/mapping return no partial plan; current CLI creates no output
for v1. V: adversarial codec, identity, placeholder, redaction, production
extraction and staged reconstruction tests are in #148; final CLI export/apply
and output preservation remain outside that issue's scope. This is not a
sanitizer for the whole book or a sandbox for an external reader.

### T07 — Forged/stale checkpoints and interrupted state (High)

**B01/B06/B07; Projects + Jobs + Infrastructure.** A syntactically valid edited
checkpoint can redirect source, output or provider and reuse `overwrite=True`;
a crash can interrupt a direct JSON write. P: validated project/source identity,
expected revisions, leases and bounded schemas with policy decisions obtained
again at execution. D: changed-source, unsupported-version and stale-conflict
outcomes. R: backup/recovery of last coherent state; never invent a successful
job or delete original/output as cleanup. V/owners:
[#135](https://github.com/MichaelAlexandreDev/ayvu/issues/135),
[#136](https://github.com/MichaelAlexandreDev/ayvu/issues/136), #137 and
[#146](https://github.com/MichaelAlexandreDev/ayvu/issues/146), including
concurrency/interruption and zero provider/output effects on rejected recovery.

### T08 — Configured executable and reader side effects (High)

**B01/B08; Library + Policies + Infrastructure.** The library service resolves an
opaque entry against its latest scan and the local adapter revalidates canonical
containment and identity before launch. The reader adapter supplies an argv list
without a shell and appends the EPUB path as one argument. A same-user process can
still change the path after validation and before the reader opens it. A configured
executable or inherited environment can read files or send data; parser exploits
and external-reader options remain an external boundary. Launch outcomes are
normalized and failure does not silently retry a different configured executable.
See [library discovery](../modules/library.md) for the cancellation and platform
limits. #159 owns the limit on offline claims; OS isolation selection belongs to
#167. A general-purpose reader is not contained by Ayvu.

### T09 — Prompt injection and excessive AI/MCP authority (High, proposed)

**F02–F05 below; Policies + AI/Integration + Application.** A document, tool
description, retrieved passage or model output may ask to upload a book, read
another project, approve itself, run a command or change provider. P: content
stays typed data; callers cannot self-assert authorization; fixed capability
allowlists, local schemas and purpose-bound decisions sit outside model control.
D: reject authority-bearing fields and log bounded denial codes. R: revoke
scoped tool access, stop the worker, preserve original and last committed state.
V: hostile content/output must produce zero extra file/network/tool/mutation
effects. [#166](https://github.com/MichaelAlexandreDev/ayvu/issues/166) owns the
proposed AI-role/authority matrix and abuse cases; #165 owns purpose rights and
#167 isolation evidence. Concrete MCP/AI runtime enforcement remains gated by
G01/G02; these decision documents do not implement a safe runtime.

### T10 — Supply-chain code execution and model/plugin replacement (High)

**B10/F05; Maintainer + Infrastructure.** Installed dependencies or future model
loaders/plugins execute with significant authority; a lockfile/hash identifies
bytes but cannot prove they are benign. P: reviewed pinned dependencies, limited
CI tokens, explicit origin/license/integrity checks and no silent downloads.
D: reproducible build/install checks and dependency alerts. R: preserve a known
good version, revoke compromised integration, isolate/quarantine suspect assets.
V/owners: [#168](https://github.com/MichaelAlexandreDev/ayvu/issues/168) delivered
CI/quality gates (not model/plugin safety); #167 owns isolation decisions;
G02 gates loaders/catalogs, signature policy and install/rollback tests.

### T11 — Retention, linking and ineffective revocation (High, partly proposed)

**B04–B09/F06; Knowledge + Policies + Provenance.** Cleartext copies, identifying
metadata and stable hashes can link a user's work; deleting one store does not
remove CSV/JSON/backups or copies held by recipients. P: independent purposes for
translation, approved TM and corpus, minimal data and explicit retention.
D: inventory and lineage of authorized copies; distinguish known recipients
from unknown external retention. R: revoke future use, propagate deletion where
technically/contractually supported, report unfulfilled deletion; never promise
model unlearning or secure erase. V/owners: #165 purpose/consent matrices, #161
memory revocation and #138 lineage; G03/G04 gate complete lifecycle and corpus
recipient/deletion tests. This model makes no legal determination about a book.

### T12 — Retry amplification and resource/cost exhaustion (Medium)

**B02/B03/B04; Translation + Providers + Jobs.** Retry/backoff and shared rate
limits bound parts of current transport, but a sequence of timeouts, oversized
responses, parallel work or repeated remote execution can still consume
resources. P: per-job size/time/concurrency budgets and explicit provider retry/
idempotency capabilities; no automatic billable retry without a safe contract.
D: bounded usage metrics. R: cooperative cancellation and reconciliation of
unknown completion before retry. V/owners:
[#103](https://github.com/MichaelAlexandreDev/ayvu/issues/103),
[#141](https://github.com/MichaelAlexandreDev/ayvu/issues/141), #159 and #167.
Timeout alone is not an end-to-end deadline or response-size bound.
