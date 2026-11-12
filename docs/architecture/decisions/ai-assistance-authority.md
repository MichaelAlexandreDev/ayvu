# Bounded roles and authority of optional assistance

Status: Proposed
Decision-Date: `2026-11-10`
Owners: Ayvu repository owner and maintainer (human acceptance and product); Application + Policies (selection, authorization, classification and limits); Jobs (deadline, cancellation and outcome); Provenance (lineage); Knowledge (glossary); Profiles/Context (profile and deterministic selection); Formats (reconstruction/validation); Infrastructure (future execution and containment).
Issue: [#166 — DESKTOP-135](https://github.com/DevStarrk1137/ayvu/issues/166).

Contract-ID: `ayvu-assistance-authority-v1`

Vocabulary-Version: `1`

## Proposed decision and scope

Propose exactly four optional roles, with `candidate_only` authority.
Producing a candidate or finding does not change approved state, approve another
result or call an application command. Every change covered by this
matrix requires an explicit human decision, separate from production. Policy alone
does not approve terminology, quality, style or layout. A future exception requires
another `Accepted` contract authorizing a single named transition, with its
actor, scope and evidence; it cannot be inferred from this record.

This record remains `Proposed`. There are no Python ports/DTOs, runtime,
model catalog/download, prompts, SDK, dependencies, Ayvu AI, MCP,
adapter, autonomous agent or mutation use case in this delivery.
No executable assistance contract may begin before final human
acceptance. Accepting this document in the future also does not implement or authorize
an adapter, real content or egress.

## Evidence and dependencies in main

Local inspection without network on `2026-11-10`: HEAD and `refs/heads/main` are
`e1076f442428cca5bd067d125081157d551d7957`. All mandatory artifacts
below are in that `main` tree, before this change. GitHub links provide
traceability supplied by the issue; no online lookup took place. Artifact
presence satisfies the documentary dependency; it does not promote a proposal to acceptance.

| Dependency | Present artifact and authority consumed |
| --- | --- |
| [#129](https://github.com/DevStarrk1137/ayvu/issues/129) | [Threat model](../product-threat-model.md), currently `Accepted`; T03/T04/T09/T11/T12, F04 and G02. The issue requested the `Proposed` artifact; the model's later acceptance does not accept roles or purpose governance. |
| [#126](https://github.com/DevStarrk1137/ayvu/issues/126) | [Events, outcomes and cancellation](../modules/jobs.md): single producer, safe points, settle and terminal result; does not guarantee interruption of a blocking call. |
| [#138](https://github.com/DevStarrk1137/ayvu/issues/138) | [DEC-014/provenance](provenance.md): opaque references, actors as types, parents and revisions; history is not authorization. |
| [#140](https://github.com/DevStarrk1137/ayvu/issues/140) | [Content and units](content-model.md): exact document, revision and unit, without using CSV position as identity. |
| [#159](https://github.com/DevStarrk1137/ayvu/issues/159) | [Network policy](../network-policy.md): `none`/`loopback`, denial before egress, no remote/HTTPS or redirects. Current endpoints do not authorize assistance. |
| [#164](https://github.com/DevStarrk1137/ayvu/issues/164) | [TranslationContextBuilder](../modules/translation-context.md) and [versioned profile](../modules/projects-profiles.md): deterministic selection and exact snapshots; inert legacy style. |
| [#165](https://github.com/DevStarrk1137/ayvu/issues/165) | [Rights/purpose governance](rights-purpose-governance.md), still `Proposed`: `PurposeGrant`, classes, independent gates and its own acceptance; no grant is presumed here. |

Additional evidence: [provider/engine](provider-contracts.md),
[pipeline](translation-pipeline.md), [catalog #157](../modules/knowledge-glossary.md),
[FormatAdapter #127](format-adapter.md) and [EPUB #144](epub-format.md).
This decision is documentary: it is not evidence of assistance execution, and
no executable test of an assistance contract exists or is claimed.

## Vocabulary, names and compatibility

The Contract-ID/Vocabulary-Version pair above jointly versions roles,
categories, fields, states, authority and limits. The following matrix is the only
v1 role vocabulary: exactly four case-sensitive literals,
without `other`, wildcard, aliases, `x-` extension or dynamic discovery. Unknown
roles, versions or categories fail closed; never choose the closest match.
Semantic change requires a new proposed version and explicit acceptance; editorial
correction does not change sets, authority or limits.

| Exclusion from the role contract | Existing or future owner / reason |
| --- | --- |
| Generic assistant, catch-all, role chaining, roles discovered by model/adapter | No authorized owner: refused in this matrix; they prevent exhaustive review. A future implementation implements exactly one accepted role contract, outside domain and application packages, and is wired explicitly by composition. |
| `TranslationAssistant` | Provider/engine and its existing contract own translation. No second translation, language, route or provider change through assistance. |
| AI `ContextBuilder` as deterministic selector | #164's `TranslationContextBuilder` is the sole owner of deterministic selection under the versioned profile/context policy. |
| Model-generated summary | Not a fifth v1 role, nor rejected forever: it may be proposed separately as an untrusted context candidate, with #164's bounded selection and the same human boundary; it never becomes a selector. |
| `ModelRuntime` or generic runtime access | Excluded from this vocabulary; a separately bounded model runtime decision remains deferred to Wave I, not globally rejected. |
| Tools, shell, filesystem, commands, tools/servers and internal AI/MCP bus | Belong to no role; integration/execution and isolation have their own decisions, including #167. There is no execution port in this record. |
| MCP transport or HTTP | Future integration/Infrastructure under Policies and #159, with no transport choice here. MCP does not expand the role contract or create an internal bus. |
| Deterministic QA | Rules planned in `DRAFT-ARCH-049`, separate from advisory assistance findings. Neither type approves the other. |

## Data, trust and selection

Application composition supplies only already authorized immutable snapshots
or opaque handles bounded to the operation. The verified local origin,
structural project/revision/unit binding, chosen role, versioned policy,
limits and application admission decisions are trusted. A serialized
field claiming these facts is not proof: exact local resolution is required.

Documents, EPUB metadata, TranslationUnits, profile/context excerpts, glossary
terms, provider output, imported artifacts and assistance output
are untrusted data, even when contained in an authorized snapshot.
Read authorization does not confer authority on text. Apparent instructions
in these data cannot choose a role, tool, file, provider, destination,
secret, policy or subsequent call. Text is not interpreted as
application instructions. Raw EPUB metadata is sent to no role.

Sensitivity classes come from C1–C5 of the threat model: text/terms and
candidates are confidential C1; references/metadata are sensitive C2/C4;
credentials and C3 authority are forbidden in the payload; C5 are bounded resources,
never capabilities received by the assistant. No data is public by default.

For purpose, use only #165's exact classes: source text/source term
as `source_text`, translation/target term as `translation_text`, aligned pair
as `aligned_pair`, human criterion as `review_annotation` and allowlisted
references as `provenance_metadata`. Also declare the content classes
when an annotation incorporates them. Context/profile text is included only if it can
be classified into these classes without losing scope; raw configuration,
assets, supporting documents, model and unclassified content are denied.
Do not invent a “glossary”, “style” or “assistance” purpose class.

Each handle fixes type, revision, a finite set of units/excerpts and a byte
ceiling; it resolves only in the authorized snapshot, without exploratory reading,
network queries or other projects. #140 IDs remain in the application;
any execution outside that context receives random references per
operation, remapped and validated locally, never stable fingerprints.
A stale, ambiguous, incomplete snapshot or one with unknown lineage denies before
loading. Minimization means selecting before loading: never send a whole book,
complete history, ZIP file, internal paths, assets or unbounded context.
No automatic request/result truncation changes the authorized selection.

## Closed role and authority matrix v1

Common rules C1–C8, N1, J1 and P1 below are part of **every cell** of this matrix;
the references are not optional extensions. T, Q, S and L are review labels,
not additional roles. Categories are closed literals, without a generic payload.

| Role / single responsibility | Request, references and allowed data | Forbidden inputs | Result category / specific fields | Authority, effects and approval | Network, resources, time, cancellation and failures | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `TerminologyCandidateGenerator` (T): propose terminology pairs for catalog review | `terminology_request_v1`; units and visible windows from the #140/#164 snapshot, validated language pair and term subset from the #157 catalog reference/revision; C1/C2/C4; `source_text`, `translation_text`, `aligned_pair`, `provenance_metadata` according to the material actually selected | C1–C8; glossary root/file, full catalog, executable rules, terms from other projects, raw profile | `terminology_candidates_v1`; `source_term`, `target_term`; only a candidate at the #157 glossary/catalog boundary, without editing/approving the glossary | `candidate_only`; no persistent effects; production T0, human decision T2 and separate command T3; #157 revalidates revision and its own limits | N1, class `none` (no egress); T/J1 budget: 32 units, request 64 KiB, result 32 KiB, 64 items, deadline 30 s, local CPU 10 s/RAM 256 MiB; cancellation J1; states E1 and failures F1 | P1; application binds exact catalog/revision and `ai` origin; does not attribute human authorship to the term |
| `QualityFindingAssistant` (Q): flag a possible advisory linguistic issue | `quality_request_v1`; source/translation pairs explicitly aligned by #140 references, #164 windows and selected human criteria; C1/C2/C4; `aligned_pair`, `review_annotation`, `provenance_metadata` (plus text classes if incorporated) | C1–C8; raw corpus/cache/TM, QA rules to execute, approval verdict, unaligned material | `quality_findings_v1`; `finding_kind` in `possible_accuracy`, `possible_omission`, `possible_consistency`, and `description`; no replacement text; distinct from deterministic QA `DRAFT-ARCH-049` | `candidate_only`; no persistent effects; T0/T2/T3 with human decision for any change; does not approve or disable deterministic QA | N1, class `none` (no egress); Q/J1 budget: 32 pairs, request 128 KiB, result 64 KiB, 64 items, deadline 30 s, local CPU 10 s/RAM 256 MiB; cancellation J1; E1/F1 | P1; application records `ai`, criteria/revisions and pair binding; never marks the finding as deterministic |
| `StyleSuggestionAssistant` (S): suggest style wording for an excerpt | `style_request_v1`; selected visible translation, human criteria and classified excerpts from the #164 profile/context snapshot; C1/C2/C4; `translation_text`, `review_annotation`, `provenance_metadata` (also `source_text`/`aligned_pair` if explicitly selected) | C1–C8; full configuration/profile, profile name as instruction, activation of the legacy `style` field, book rewrite | `style_suggestions_v1`; `style_aspect` in `tone`, `clarity`, `fluency`, and plain excerpt `suggested_text`; not a patch or profile revision | `candidate_only`; no persistent effects; T0/T2/T3; human decides on the excerpt. Does not activate inert `style` or change the versioned profile; any activation requires its own contract | N1, class `none` (no egress); S/J1 budget: 16 units, request 64 KiB, result 64 KiB, 16 items, deadline 30 s, local CPU 10 s/RAM 256 MiB; cancellation J1; E1/F1 | P1; application fixes profile/context and human input criterion; suggestion does not create a profile fact |
| `LayoutSuggestionAssistant` (L): describe a possible presentation improvement | `layout_request_v1`; selected visible excerpts and minimal projection of constraints/capabilities already negotiated by FormatAdapter #127/#144, in opaque revision references; C1/C2/C4; `source_text`/`translation_text` according to selection and `provenance_metadata` | C1–C8; raw EPUB/DOM/OPF, images, CSS/scripts, member paths, artifact writing, changes to claims/capabilities | `layout_suggestions_v1`; `layout_aspect` in `spacing`, `line_break`, `readability`, and plain `description`; no markup, executable geometry or patch | `candidate_only`; no persistent effects; T0/T2/T3; human decides, Formats retains negotiation, reconstruction and validation; does not change capabilities or validated artifacts #127/#144 | N1, class `none` (no egress); L/J1 budget: 16 units, request 64 KiB, result 32 KiB, 32 items, deadline 20 s, local CPU 5 s/RAM 256 MiB; cancellation J1; E1/F1 | P1; application fixes descriptor/constraint revision and unit references; description is not proof of fidelity |

## C1–C8: common structural constraints

These are proposed documentary schemas, not executable DTOs or validators.

- **C1 — closed request:** exact fields `contract_id`, `vocabulary_version`,
  `request_id`, `role`, `request_category`, `snapshot_ref`, `project_revision`,
  `unit_refs`, `input_refs`. Languages, criteria and projections specific to the
  row are typed inputs referenced by `input_refs`, not extra keys.
  Composition fixes these fields, their revisions and the category/role correspondence;
  handles do not grant access outside the selection. There is no free prompt field.
- **C2 — types:** new references are random opaque references per operation, not
  Path/URL, names or text hashes; they must resolve in the authorized local binding.
  Revision is a positive integer, without accepting booleans; lists have no duplicates;
  versions/categories are exact. Missing, extra or duplicate fields, wrong types,
  unknown versions and non-finite numbers invalidate the whole set.
  Booleans are not numbers in any field, including confidence.
  `unit_refs`/`input_refs` are nonempty and bounded by the row's budget;
  each unit allows at most four input_refs, including criteria and facts.
- **C3 — no capability in the request:** forbid arbitrary Path, URL, attached
  file, executable command, tool/server, chosen provider/model,
  destination, resolved credential or secret reference, caller-supplied
  authority object, approval identity and policy change.
  Network/purpose/budget bindings remain with the application
  controller, outside the role payload, and are never obtained from document/import.
- **C4 — closed result:** exact fields `contract_id`, `vocabulary_version`,
  `request_id`, `role`, `result_category`, `items`. Each item contains nonempty
  `unit_refs`, only from the request, the row-specific fields and, optionally,
  `evidence_refs`, `confidence`, `rationale`. Evidence references only already
  selected excerpts; it is not a URL, file, new query or call chain.
  The application assigns candidate identity and provenance after validation;
  no identifier claimed by the result becomes an authoritative record.
- **C5 — no approval or command in the result:** forbid `approved=true`,
  human/policy actor, mutation command, network change, PurposeGrant,
  purpose grant, secret reference, executable tool request
  and arbitrary filesystem destination. Extra/unknown fields fail
  closed at any level; no generic extension/metadata field.
- **C6 — inert text:** text fields are plain data, without evaluation,
  command interpolation, link opening, navigation, tool parsing
  or active HTML/Markdown rendering. A sentence containing a path, URL,
  `approved=true` or malicious instruction may remain text, without creating a field
  or effect. In L, markup is not an accepted output format; a description citing it
  remains only text. No result automatically feeds another call.
- **C7 — sizes:** measure the entire serialized request and entire result
  in UTF-8 bytes, including envelope, separators and optional fields, before use.
  Each content string has at most 4 KiB; source_term/target_term also
  respect #157's 1,024/4,096 code points, with the smaller limit applying. A reference
  has up to 128 bytes; rationale up to 1 KiB; evidence_refs up to 8 per item;
  item unit_refs up to the row's limit. Excess invalidates everything, without partial
  result, resending, subdivision or automatic truncation. Zero items is valid
  success without findings, not approval. Confidence, when numeric, is finite between
  0 and 1, or null; absence is not an error.
- **C8 — no power through scoring:** confidence, score, rationale and evidence
  are untrusted, optional and possibly uncalibrated. Score is not a
  v1 field; if added as an extra key, it is invalid. None of these concepts
  authorizes automatic approval, mutation, corpus/dataset eligibility,
  provider/model, network or routing. Rationale is an optional short explanation,
  never a request for, storage or disclosure of hidden reasoning.

## N1: independent content/purpose and network authorization

Application + Policies own classification and admission. Each row
starts without egress (`none`); local execution without transport does not need
network permission, but still requires ordinary content authorization.
Loopback is network and an external process is a recipient: location does not make it trusted.

A future integration needs an immutable binding of operation, project/revision,
data selection/classes, purpose, recipient/configuration and revisions of both
policies. This binding remains with composition, never chosen or expanded
by the role. A policy reference is not a grant or authority object
accepted from an untrusted caller. Revalidate before loading content
and immediately before any effect/egress, within validity/deadline.
Do not resolve credentials for an already denied execution.

| Content/purpose | Network | Proposed admission result |
| --- | --- | --- |
| Refused, unclassified or authorization missing | Any | `denied/content_denied` or `denied/purpose_denied`, according to the first content/purpose gate that refused; zero egress and zero credential resolution. |
| Allowed | `none`/strict-offline and execution requires transport | `denied/network_denied` before content or credential leaves, including loopback; no local/remote fallback. |
| Allowed | Decision missing, expired, divergent or destination not covered | `denied/network_denied`; do not expand policy or infer permission from the grant. |
| Allowed | No transport, or exact decision allows transport | Admissible only if all other limits and accepted contracts exist; never candidate approval. |

Ordinary/governed operational classification comes from the use case reviewed
by the application under #165, not the assistant's declaration. #165 does not
enumerate assistance as its own ordinary operation: until an accepted contract
classifies the exact operation, deny for unclassified purpose. Do not use
`provider_operational` as an alias for assistance or as a grant exemption.
If there is `governed_use`, require effective RightsAssertion and PurposeGrant for
the scope, classes, destination and all exact purposes; #165's Proposed
status prevents this use through its own gate. Quality approval does not grant
purpose or turn a candidate into corpus.

Today #159 authorizes only its translation/discovery endpoints under `loopback`;
it supplies no assistance endpoint. No row in this matrix selects MCP,
HTTP, endpoint or runtime. A future accepted integration contract must bind
transport and recipient under explicit policy; without it, network execution is
denied even if `loopback` is selected. Remote/HTTPS remain outside
the current policy, without silent fallback, environment proxy or redirect.
Network denial and purpose denial remain distinct reasons, even
when both would occur: the fixed order is content/purpose, then network;
one passed gate does not supply the other.

## J1, E1 and F1: budget, optionality and termination

**J1:** T/Q/S/L ceilings are conservative proposals, not capacity measurements.
Application + Policies version/configure equal or lower values before
admission; Jobs owns the total monotonic deadline and #126's token;
Infrastructure owns CPU, RAM and byte measurement/containment. There are no threads,
process control or terminator per role. The row's CPU and RAM limit the
entire local execution attributed to the request, including the future implementation;
the controller also enforces the byte ceiling before allocating the entire result.
Proposed concurrency is one active request per job, one attempt, zero hidden
retries and zero subsequent calls. Queue waiting, preparation, execution and
validation count toward the deadline; do not restart the clock when changing phase.

No promise is made to limit an external server's CPU/RAM: the future execution
owner needs an accepted contract defining and proving equivalent limits at that recipient,
in addition to local limits; without a verifiable guarantee, `unavailable/budget_unavailable`.
Missing or unenforceable ceilings also make execution unavailable.
Future adapter/runtime/isolation must prove bounded stopping and release
under these ceilings before admission. A safe point alone does not interrupt
a blocking call; do not confuse transport timeout with total deadline.

Proposed safe points: before loading, between bounded inputs, before
dispatch, after return and before validation/delivery. Consume #126's token,
without another cancellation source. Deadline exceeded without acknowledged cancellation
is `failed/deadline`; memory/CPU/byte excess is `failed/resource_limit`.
Cancellation acknowledgement commits the operation to `cancelled`, without
delivering a late candidate, even if the call returns success. The race between
request/checkpoint/settle follows #126: an unacknowledged request does not invent
cancellation; earlier settle makes the request `too_late`. One producer emits
request, acknowledgement and a single outcome in canonical order. Do not add
new states to JobOutcome or the persistent job state machine.

**E1:** these are role availability/result states, not new
persisted translation job states. Admission may end without starting a job.
If a future authorized execution starts a separate assistance job,
it uses existing outcomes: success maps to `completed`, failure to `failed`,
acknowledged cancellation to `cancelled`, respecting #126's summary and JobFailure.

| Role state | Observable fact and bounded consequence |
| --- | --- |
| `absent` | No contract/implementation installed or composed. Do not create a no-op, model, SDK, transport or fictitious job. |
| `disabled` | Known role explicitly turned off in authorized composition. Do not load content or invoke implementation. |
| `denied` | Role enabled but content, purpose, network, revision or acceptance gate refused. Typed reason, without execution/egress. |
| `unavailable` | Admission without policy denial, but implementation/capability or verifiable limit missing. Do not treat as disabled/success. |
| `failed` | Execution started without a valid candidate: schema, deadline, resource, integrity or normalized failure. No usable partial candidate. |
| `cancelled` | Token acknowledged at a safe point and outcome settled; discard undelivered buffers/candidates. Do not claim instant interruption, delete the original or undo already completed translation. |
| `successful` | Valid structured result, including empty list, bound to the request. Only an in-memory candidate, still not approved/persisted/applied. |

Evaluation order: absent, disabled, acceptance/structure/authorization gates
(C1–C8, content/purpose, network), execution availability and then J1.
All four absent preserve the deterministic translation flow. An
optional execution uses a separate job and does not change the translation job's cache,
checkpoints, plan, progress or outcome; failure/refusal is presented as
its own result, without being discarded as success or causing fallback.
Exhausting budget does not allow trying another role/provider/model/destination.

**F1:** closed proposed execution reasons are `invalid_result`, `deadline`,
`resource_limit`, `stale_snapshot`, `adapter_failed`, `unexpected_error`;
admission uses `governance_denied`, `invalid_request`, `content_denied`,
`purpose_denied`, `network_denied`, `stale_snapshot`, `capability_unavailable`,
`budget_unavailable`. They are local codes, never messages copied from input.
They are not new codes already implemented in JobFailure. Adapted failures use
the existing integrity/input, policy, capability, resource or internal families
according to cause; cancellation follows its outcome, not a failed/cancelled.
There is no automatic retry or exception leaving partially approved state.

## P1, transitions and adversarial review

Authorship, persistence, the human decision, the required reviews and the adversarial examples A01–A22 are in the [companion record](ai-assistance-authority-transitions.md).

## Rejected alternatives and unanswered questions

| Alternative | Decision / reason |
| --- | --- |
| Universal assistant with tools and free context | Rejected: creates generic authority and internal bus without a reviewable matrix. |
| TranslationAssistant and AI ContextBuilder | Rejected in these roles: duplicate provider/engine and #164 deterministic selection. Future candidate summary remains possible under another contract. |
| Expose ModelRuntime for each role | Excluded from the matrix; separating its bounded decision in Wave I preserves owners and does not reject runtime globally. |
| Role discovery, provider/tool or schema extensible by result | Rejected: content does not grant capability; unknown/extra fail closed. |
| Automatic approval by confidence, generic policy, QA passage or copied consent | Rejected: lacks human act bound to candidate; approval does not grant rights/purpose. |
| Apply suggestions during translation and fall back to remote on failure | Rejected: changes workflow, loses optionality and bypasses gates/budgets. |
| Store prompts, reasoning or whole book for audit | Rejected: expands exposure without proving authority; P1 uses minimal private references and codes. |

Open questions, without provisional authorization:

1. Does the maintainer accept each row, classification, limit and owner of this proposal?
   The gate below must record this decision; delivery authorization is not acceptance.
2. Which accepted contract will classify the exact assistance operation under #165?
   Until then, unknown purpose denies, without creating an “assistance” PurposeGrant.
3. Which lower budgets will be useful on real hardware and how can containment be proved,
   including blocking call and any external recipient? Without proof, unavailable;
   raising the ceiling requires a new version/acceptance, not adapter choice.
4. How will the future interface prove a current human act and anti-replay, and which
   contract will accept candidate persistence/TTL without conflicting with #138?
   Until answered, T1/T3 are not available through this record.
5. Which runtime/integration/isolation will be proposed in Wave I and its issues?
   No transport, SDK, catalog, tool or endpoint is chosen now.
6. Which schema/policy evolution would activate style or apply layout changes
   without changing claims? Profiles/Formats contracts remain the owners;
   suggestion and human decision do not dispense with their future accepted implementation.

## Binary human gate and acceptance checklist

An Ayvu repository owner/maintainer must explicitly accept **each**
role row, data/authority boundary, approval rule and network/resource
owner. The exact action is a **separate reviewed change in this file**
that literally changes `Status: Proposed` to `Status: Accepted` and records
the acceptance date, responsible role and reviewable reference to the human act
that accepted all items below. No acceptance is recorded in this delivery.

Merge, issue closure, publication, approval to publish the proposal,
passing tests or acceptance of #129/#165 do not satisfy this gate. No executable
assistance contract may begin before that acceptance. Acceptance does not grant
PurposeGrant, accept another proposal or authorize an adapter or
mutation by itself. Any pending item keeps the record Proposed, without partial acceptance.

- [ ] Accept each of the four rows and the closed vocabulary/categories/versioning, without generic or unknown role.
- [ ] Accept data limits, snapshots/handles, classification, minimization and C1–C8, including forbidden fields and inert text.
- [ ] Accept T0/T1/T2/T3/D0 separation, human act to store/decide/apply, P1 and no policy approval in this contract.
- [ ] Accept N1, its owners, future classification under #165, independent denials and absence of transport/fallback.
- [ ] Accept each T/Q/S/L budget, bounded configuration, CPU/RAM/byte/deadline owners, #126 cancellation and E1/F1.
- [ ] Accept compatibility with provider/engine, #157, #164, inert style, deterministic QA and FormatAdapter #127/#144; separate runtime in Wave I.
- [ ] Record AR, AC A01–A22, OP, PS and DV review, distinguishing documentary evidence from still nonexistent execution.
- [ ] Resolve or safely defer open questions, preserving denial/unavailability where contract or proof is missing.
- [ ] Record the explicit human act and make the separate status change before starting any executable assistance contract.
