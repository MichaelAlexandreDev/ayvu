# Review application use cases

Issue: DESKTOP-118 / #149.

## Boundary

`ayvu.review_application` defines versioned, framework-neutral command and
result values plus `ExportReviewPackage`, `ValidateReviewPackage`, and
`ApplyReviewPackage`. It imports no CLI, UI, CSV, EPUB/HTML parser, provider,
network, SQLite, or filesystem adapter. `ayvu.review_composition` wires those
ports to the v2 exchange codec/mapper, read-only project readers, the EPUB
`FormatService`, job/provenance repositories, and the DESKTOP-106 artifact
publication protocol.

`translate --review-output` uses the exact successful `TranslateProjectResult`.
The engine retains its extracted `ContentDocument`s, structural translations,
unit IDs, and machine origin status. It no longer builds the positional v1
review projection during translation. Export refuses facade results without a
real project/job/artifact revision or complete unit lineage.

## Validation and application

Validation first inspects the bounded package bytes and computes a digest over
the complete bytes. V1 returns bounded inspection counts and
`legacy_reexport_required`; application stops before creating a job or output.
For v2, the mapper resolves the exact project revision, source fingerprint,
machine artifact revision/digest, translation job, current document revisions,
and machine unit provenance. It reconstructs the machine version into a
digest-only sink and checks that digest against the published machine artifact.
No destination, job, repository row, artifact, provenance record, provider, or
network operation is created by validation. Its evidence digest describes one
preview; it is not a capability. Apply acquires its legal job context and runs
the full package and repository validation again before reconstruction.

All mapper conflicts are fatal before reconstruction. Reconstruction is handed
to the EPUB format adapter with the validated `ContentDocument` and
`UnitTranslation` values. It writes only into the D106-owned restricted staging
artifact. The full staged EPUB is format-validated, then the source, project,
machine artifact, and machine translation job are rechecked before publication.
The original source and machine artifact destination are rejected as output
targets. Translation provenance stores only a digest of its destination so an
apply request can detect an attempt to overwrite the machine output without
storing that path.

## Publication, recovery, and lineage

D106 owns rename-before-commit files and their reconciliation journal. The
filesystem rename and SQLite/provenance/event operations are not one
transaction. A rename that has not reached the artifact commit remains an
owned reconciliation state and is never returned as a ready result. A crash
after the final lineage marker but before the job transition can be completed
idempotently when the ready artifact and marker agree. A request without that
marker is never reported as successful human review.

Lineage records link the package fingerprint, exact source revision, machine
artifact, reviewed unit IDs, and final artifact. Per-unit human records say
`human_edit_validated`; the final operation marker says `review_applied` and is
written last. Thus a crash before the marker cannot leave a per-unit
`human_edit_applied` success claim. Export has corresponding staged rows and a
final `review_exported` marker. Human/provider/cache/TM/replay origins remain
distinct. Text, notes, document content, absolute paths, and credentials are
absent from default events and provenance projections.

Internal idempotency keys are based on the package/request fingerprints. They
are separate from CLI overwrite permission: an identical committed request
returns its original artifact, while a distinct request to an existing
destination follows the current output-conflict policy. A committed result whose
file was removed or changed can no longer be replayed, so the request runs again
as new work under that same policy, with its lineage keyed to the new job. The CLI defaults to
`<source-stem>-reviewed.epub`, returns exit code 0 for a ready result and 1 for
validation, conflict, cancellation, or application failure. `--overwrite`
allows replacing an earlier reviewed output, not the source or machine output.

## Verification

`tests/test_review_application.py` covers port orchestration, read-only
validation, real transport tripwires, package changes after preview, fatal
conflicts, v1 refusal, idempotency versus overwrite, cancellation safe points,
redaction, and package-size limits. The real CLI review round trip and output
preservation, replay of a removed or changed result, and cancellation against
production repositories are covered in `tests/test_cli_translate_project.py` and
`tests/test_workflow_characterization.py`; codec and placeholder attack cases
remain in `tests/test_review_exchange.py`.
