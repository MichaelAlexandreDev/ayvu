# Product threat assessment decision

Status: Accepted
Decision-Date: `2026-09-29`
Owners: Project maintainer (acceptance)
Issue: [#129 — DESKTOP-008](https://github.com/MichaelAlexandreDev/ayvu/issues/129)

Accepted: 2026-11-05 by the project maintainer, through explicit approval in the development session

Evidence baseline: `main` at `1a28b84` (PR #186). This is a source-based threat
assessment, not a penetration test, security certification, or acceptance of
residual risk. Controls described as proposed are not implemented by this
document. Accepting this model accepts its threat assessment and the ownership
of the gaps it names; it does not accept any other proposed architecture
decision.

## Scope and decision boundary

The delivered product is the local Python CLI described in the
[product scope](../../product-scope.md). It reads EPUBs, translates through an HTTP
service, maintains SQLite cache and JSON settings/checkpoints, exports Markdown
and CSV, inspects legacy review files, validates v2 review packages, and can
launch an external reader. The CLI no longer applies positional v1 review text. The
[accepted module boundaries](modular-monolith-boundaries.md) supply ownership;
the [shared use-case matrix](../modules/interfaces-cli.md) describes future seams.

Desktop, local API, inbound/outbound MCP, plugins, AI assistance, managed models,
durable projects/jobs, approved translation memory, and contributed training
corpus are proposed boundaries. #159 now enforces the request-scoped
[HTTP none/loopback boundary](../cross-cutting/network-policy.md); remote URLs and HTTPS are denied
unless a run selects the explicit, consented `remote` policy of #379 (see [Remote egress](#remote-egress-379)).
Broader purpose-specific authorization remains future work. The current CLI has
no account or multi-tenant boundary.

The original must remain immutable, derived outputs must preserve the supported
EPUB structure, and only selected reader-visible text should reach translation.
The [characterization baseline](../../translation-workflow-migration-baseline.md)
tests normal workflows, not every hostile file or output alias. In particular,
the source/destination alias gap in T01 remains an implementation risk even
though immutability is a product invariant.

No book, private glossary, credential, cache, or user checkpoint was used as
evidence. Paths below describe documented defaults or repository sources, never
an inspected user's data. There is no permission here to remove DRM, acquire
books, distribute protected content, upload data, or implement the mitigations.

## Where the model lives

Assets, data flows, storage, the proposed policy and interfaces and the STRIDE/LINDDUN walkthrough are in [product security, privacy, and data flows](../cross-cutting/product-threat-model.md); the threats T01–T12 are in the [threat register](../cross-cutting/threat-register.md).

## Evidence, validation and acceptance limits

The current [`test_workflow_translation_resume.py`](../../../tests/interfaces/cli/test_workflow_translation_resume.py), [`test_workflow_route_review.py`](../../../tests/interfaces/cli/test_workflow_route_review.py), [`test_workflow_translation_reporting.py`](../../../tests/interfaces/cli/test_workflow_translation_reporting.py), [`test_workflow_batch_preview.py`](../../../tests/interfaces/cli/test_workflow_batch_preview.py)
exercise the real CLI through fake HTTP and temporary EPUB/cache/checkpoint
files: normal source preservation, archive/visible-text filtering, cache then
glossary, interrupted resume, intermediate routing, v2 review mapping with
staged reconstruction, and legacy v1 apply refusal.
[epub io paths tests](../../../tests/formats/epub/test_epub_io_paths.py),
[epub translation values tests](../../../tests/v1/test_epub_translation_values.py),
[epub translation execution tests](../../../tests/v1/test_epub_translation_execution.py),
[epub translation metadata tests](../../../tests/v1/test_epub_translation_metadata.py),
[epub io language tests](../../../tests/formats/epub/test_epub_io_language.py),
[epub translation selection tests](../../../tests/v1/test_epub_translation_selection.py),
[epub io chapter selection tests](../../../tests/formats/epub/test_epub_io_chapter_selection.py),
[epub chapter selection tests](../../../tests/kernel/test_epub_chapter_selection.py),
[epub review legacy refusal tests](../../../tests/v1/test_epub_review_legacy_refusal.py),
[epub translation title tests](../../../tests/v1/test_epub_translation_title.py),
[html translate markers tests](../../../tests/v1/test_html_translate_markers.py),
[html translate structure tests](../../../tests/v1/test_html_translate_structure.py),
[html translate cache memory tests](../../../tests/v1/test_html_translate_cache_memory.py),
[html translate terms alt tests](../../../tests/v1/test_html_translate_terms_alt.py),
[html apply reviewed tests](../../../tests/v1/test_html_apply_reviewed.py),
[html translate markup literals tests](../../../tests/v1/test_html_translate_markup_literals.py),
[preflight resource tests](../../../tests/translation/test_preflight_resources.py),
[preflight provider route tests](../../../tests/translation/test_preflight_provider_routes.py),
[translator HTTP tests](../../../tests/providers/libretranslate/test_translator_http.py),
[rate-limit tests](../../../tests/providers/libretranslate/test_translator_rate_limit.py),
[language tests](../../../tests/providers/libretranslate/test_translator_languages.py),
[route tests](../../../tests/providers/libretranslate/test_translator_routes.py),
[cache tests](../../../tests/v1/test_cache.py),
[resume tests](../../../tests/recovery/test_resume.py),
[library tests](../../../tests/library/test_library.py) and
[cli output plan tests](../../../tests/kernel/test_cli_output_plan.py),
[cli translation options tests](../../../tests/kernel/test_cli_translation_options.py),
[cli root processing tests](../../../tests/interfaces/cli/test_cli_root_processing.py),
[cli root resume tests](../../../tests/interfaces/cli/test_cli_root_resume.py),
[cli translate output configuration tests](../../../tests/interfaces/cli/test_cli_translate_output_configuration.py),
[cli translate review export tests](../../../tests/interfaces/cli/test_cli_translate_review_export.py),
[cli translate markdown reports tests](../../../tests/interfaces/cli/test_cli_translate_markdown_reports.py),
[cli translate cache controls tests](../../../tests/interfaces/cli/test_cli_translate_cache_controls.py),
[cli translate language tests](../../../tests/interfaces/cli/test_cli_translate_language.py),
[cli translate execution profiles tests](../../../tests/interfaces/cli/test_cli_translate_execution_profiles.py),
[cli translate target profiles tests](../../../tests/interfaces/cli/test_cli_translate_target_profiles.py),
[cli console security tests](../../../tests/interfaces/cli/test_cli_console_security.py) supply narrower evidence. They are not proof
of budgets, alias containment, privacy redaction or proposed runtime isolation.

[Requests documentation](https://requests.readthedocs.io/en/latest/user/advanced/)
describes environment proxies and default response buffering; Ayvu's current
session does not add destination/response-size controls. Threat T03 is an
inference from that transport behavior and the source call sites, not a recorded
real-world exfiltration incident. No malicious endpoint was contacted.

Document acceptance checks:

1. Trace each B/F boundary from its entrypoint to recipient/store and accountable
   module; distinguish implemented checks from proposed contracts.
2. For each High risk, verify prevention/detection/recovery, linked mitigation
   and validation ownership, and any explicit runtime gate with no published
   implementation issue yet.
3. Walk the synthetic cases in both lenses; confirm document/tool/AI content
   never gains authority in the proposed design.
4. Check repository links with `uv run pytest tests/architecture/test_documentation_links.py`,
   inspect/render the Mermaid flow where tooling is available, and run
   `git diff --check`. Run `uv run pytest` for changes to the link test.
5. Reassess after provider/interface/format/IPC changes, new stores or recipients,
   dependency advisories, new exploits or changed permissions/retention.

Runtime mitigations require meaningful production-boundary tests in their own
issues, using synthetic fixtures, fake transports/process launchers and
`tmp_path`. Green documentation or import-boundary tests do not prove security.

## Remote egress (#379)

Added on 2026-11-18 with [#379](https://github.com/MichaelAlexandreDev/ayvu/issues/379), whose design the
maintainer accepted that day. It narrows T03 for remote AI providers; it does not accept any provider.

**Assets.** The book's visible text and the glossary terms sent with it; the provider credential; the
project's consent record; the integrity of the translation that comes back.

**Threats.**

- *Text leakage* to an unintended recipient: a wrong host, a lookalike name, a redirect, an environment
  proxy, or a silent fallback from a local translator.
- *Credential leakage* into configuration, checkpoints, reports, logs, provenance, the cache, the
  consent record or terminal output, or to another host through a redirect.
- *Redirect and proxy hijack:* a 3xx or a `HTTPS_PROXY`/`ALL_PROXY`/`.netrc`/CA override that moves the
  request or its key elsewhere.
- *DNS manipulation* that points a declared name at another address.
- *Consent erosion:* sending before the owner agreed, after a "no", after revocation, or under a changed
  consent text; a developer run that sends without an explicit flag.

**Mitigations (implemented and tested).**

- `remote` is never a default and is selected per run; `none` and `loopback` are unchanged.
- HTTPS only, to the `ALLOWED_HOSTS` the adapter declares in code; no userinfo, query, fragment or
  undeclared address literal; plain HTTP off the machine is refused.
- `trust_env=False` and verified TLS; the adapter re-decides each prepared URL and refuses any proxy,
  client certificate or TLS override; only 307/308 to a declared host are followed, and credential
  headers never leave the authorized host.
- The credential is read from the provider's standard environment variable when the run starts and
  passed as an opaque `RemoteCredential` that does not print or serialize; a fixture secret is searched
  for in every written file and in all output.
- Consent per project and provider (provider, UTC time, text version `remote-egress-v1`) in project schema
  version 5, asked once in plain words before the book is read; "no" exits 1 with zero requests; developer
  mode needs `--consent-remote`; `ayvu consent revoke` removes it; a new text version asks again. Without a
  project (preview, dry run, cache-only, legacy resume) a remote provider is refused.
- Each adapter declares `RETENTION_CLAIM`, shown in the consent question, the plan and the report.

**Residual risk.** Once sent, the text is under the provider's control: Ayvu shows the provider's own
retention claim but cannot verify or enforce it, and revoking consent does not recall what was sent.
DNS is resolved by the platform; a manipulated answer is stopped by TLS certificate validation, not by
Ayvu, so a mis-issued certificate or a compromised trust store remains a risk. The environment variable
is visible to other processes of the same user, and shell history may keep it; the secret store of
[#160](https://github.com/MichaelAlexandreDev/ayvu/issues/160) replaces it later. Translations that come
back still pass the strict result check, but their quality is the provider's.

## Residual risks and human decisions

- This model was accepted on 2026-11-05 by the project maintainer. Acceptance
  records the assessment and the owners of each gap; it does not close the
  current alias, archive, egress or privacy gaps, which stay open in their
  owning issues.
- #144 explicitly keeps legacy facades unchanged. G05 needs maintainer priority
  review; a separate urgent patch may be warranted before the migration chain.
- Budgets, retention periods, remote provider approval, rights evidence and
  permissions are decisions to validate in their owning issues, not guessed
  numeric limits or universal legal guarantees in this document.
- External translators, readers, OS swap/backups and already-exported copies
  constrain confidentiality, offline assurances, deletion and revocation.
  Stronger guarantees require a documented supported isolation environment.
- G01–G04 stay closed until concrete runtime/lifecycle issues and validation
  exist. Missing follow-up publication is visible work, not an implicit waiver.
- New security-sensitive claims require updated code/test evidence and human
  review. This document accepts no Desktop toolkit, remote service, plugin
  architecture, training policy or future store implementation.
