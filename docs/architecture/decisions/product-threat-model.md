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
[HTTP none/loopback boundary](../cross-cutting/network-policy.md); remote URLs and HTTPS are denied.
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
[translate project tests](../../../tests/translation/test_translate_project_plan_gates.py),
[html structure extraction tests](../../../tests/formats/epub/test_html_structure_extraction.py),
[html structure reconstruction tests](../../../tests/formats/epub/test_html_structure_reconstruction.py),
[translation pipeline tests](../../../tests/translation/test_translation_pipeline.py),
[review apply tests](../../../tests/review/test_review_application_apply.py),
[epub io language tests](../../../tests/formats/epub/test_epub_io_language.py),
[epub io chapter selection tests](../../../tests/formats/epub/test_epub_io_chapter_selection.py),
[epub chapter selection tests](../../../tests/kernel/test_epub_chapter_selection.py),
[preflight resource tests](../../../tests/translation/test_preflight_resources.py),
[preflight provider route tests](../../../tests/translation/test_preflight_provider_routes.py),
[translator HTTP tests](../../../tests/providers/libretranslate/test_translator_http.py),
[rate-limit tests](../../../tests/providers/libretranslate/test_translator_rate_limit.py),
[language tests](../../../tests/providers/libretranslate/test_translator_languages.py),
[route tests](../../../tests/providers/libretranslate/test_translator_routes.py),
[legacy cache storage tests](../../../tests/knowledge/cache/test_cache_admin_legacy_storage.py),
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
