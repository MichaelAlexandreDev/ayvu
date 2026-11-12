# Rights, purpose, retention and revocation governance

Status: Proposed
Decision-Date: `2026-11-05`
Owners: Ayvu repository maintainer (acceptance and product); Policies (semantics); Knowledge (cache/TM/corpus); Projects + Provenance (identity and dependencies); Infrastructure (future retention and deletion execution).
Issue: [#165 — DESKTOP-134](https://github.com/MichaelAlexandreDev/ayvu/issues/165).

Policy-ID: `ayvu-rights-purpose-v1`

Vocabulary-Version: `1`

## Recommendation and limits of this decision

Adopting the contracts and tables below, with denial by default, is recommended.
These are product recommendations for human review, not legal analysis,
ownership validation or a license to redistribute protected content.
Possessing a work does not prove rights. Ayvu only records a local assertion
and a bounded human choice; it cannot grant third-party rights.

This record remains **Proposed**. No rights/purpose registry
may begin while it is Proposed. There is no registry, database schema,
migration, CLI, UI, exporter, uploader, deletion worker, training pipeline
or unlearning mechanism in this delivery. The tables describe the contract of
a future implementation, conditional on acceptance and execution issues.
They do not change current CLI commands, messages, retention or permissions.

`RightsAssertion` records that an identified local actor asserted a rights
basis for a scope. It does not validate the assertion or authorize a purpose.
`PurposeGrant` is the **sole canonical authorization record** for governed
purposes, including explicit refusal. It requires a separate effective
rights assertion. Neither alone represents universal permission.

“Consent” and the product term “consent” describe only the explicit human
act that may create a `PurposeGrant`. They are not a third domain object,
serialized type, alias of `PurposeGrant`, rights basis, state or independent
source of authority. Fields such as `consent`, `consent_id` and
`consent=true` are unknown in the canonical contract and invalidate its input;
they do not automatically convert into a grant.

## Evidence and present dependencies

Local verification without network: the three mandatory artifacts are in `main`
`bca1ccf1b035dbab8542475368265e58b4e9853c`. The analyzed checkout is
`aaca5668781996c9fa53497d48343cac6a537c7d`, which contains that base. Presence in
`main` is the prerequisite, not issue closure or acceptance of another proposal.

| Dependency / evidence | What this record consumes and does not presume |
| --- | --- |
| [#129](https://github.com/MichaelAlexandreDev/ayvu/issues/129), [threat model](../product-threat-model.md) | Accepted on 2026-11-05; T03 (destinations), T05 (cache/corpus), T09 (AI/MCP), T11 (retention/revocation), F06 and G04. That acceptance does not accept this policy. |
| [#134](https://github.com/MichaelAlexandreDev/ayvu/issues/134), [manifest](../modules/projects.md) | `project_id`, revision and exact source/configuration identity. Path, title and isolated hash are neither scope nor authority. `policy_ref` is not a grant. |
| [#138](https://github.com/MichaelAlexandreDev/ayvu/issues/138), [DEC-014/provenance](provenance.md) | Opaque references, revisions and lineage; actors are types, without people's names. An opaque subject does not prove identity or rights; copied history does not authorize. |
| [Module boundaries](../modular-monolith-boundaries.md), [artifacts](job-artifacts.md), [official product scope](../../product-scope.md) | Policies decides; cache, TM and corpus are independent. An existing artifact does not prove permission. Content for corpus/training is not automatic contribution. |
| [Provider contracts](../provider-contracts.md), [discovery](../provider-discovery.md) | Configuration/discovery are neither network nor purpose authorization. |

GitHub references are traceability evidence; they were not consulted
online in this review. No external standard or legal document is assumed
by this proposal. Future network rules from
[#159](https://github.com/MichaelAlexandreDev/ayvu/issues/159), approved memory from
[#161](https://github.com/MichaelAlexandreDev/ayvu/issues/161) and the model's G03/G04 gates
remain necessary. Current provenance does not offer descendant queries
or individual deletion, and the CLI does not record all lineage. Without a complete
inventory, future governed use must deny; this proposal does not fill those
gaps with a supposed deletion guarantee.

## Closed conventions and versioning

All identifiers in the tables are case-sensitive literals and
jointly versioned by Policy-ID and Vocabulary-Version. There is no `other`, `all`,
wildcard, free purpose, `x-` extension, inferred or accepted unknown value.
Explanatory words and scenario IDs are not policy values.
Lists are nonempty finite sets, without duplicates, in lexical order,
except lists whose zero minimum is explicitly stated below;
an empty list outside those exceptions, wrong type, extra key, duplicate key and missing field are
invalid. `null` exists only where the table explicitly allows it.

New local references (`record_id`, `actor_ref`, `preview_ref`, `support_ref`,
`destination_ref`, `work_ref`, `segment_ref`, `snapshot_ref`, `obligation_ref`)
are canonical lowercase UUID4s, randomly generated by the local application.
They are not text, URI, path, content hash, username or document.
The existing `project_id` is the manifest's canonical UUID, without retroactively requiring
UUID4 for valid manifests. An opaque reference is not evidence by itself: it must
resolve uniquely in the authorized local context, without network resolution.
Example IDs below are review symbols, not serializable inputs.

Times are canonical UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; valid calendar,
microsecond precision, without rounding. Durations are positive integer
seconds; one day is exactly 86,400 seconds. Booleans are not integers.
There is an authoritative clock injected by the application, outside the payload/model.
A clock unavailable or going backward relative to the last local decision denies
new effects until local reconciliation. Import time is not authority.

| Version value / change | Mandatory transition and effect |
| --- | --- |
| `ayvu-rights-purpose-v1`, vocabulary `1` | Only pair understood by this record; still blocked by Proposed. |
| Any different, missing or partially recognized pair | `deny_version`; do not map by similarity, automatically migrate or accept the known part. |
| Editorial correction without semantic change | Same pair; ordinary review verifies that no set, deadline, consequence or precedence changed. |
| Add/remove value, change scope, deadline, authority, consequence or rule | New Policy-ID and new Vocabulary-Version; new Proposed record and new human acceptance. |
| Activate new accepted version | All previous assertions and grants become immediately incompatible. There is no migration in v1, or replacement across versions. Projects with records from another version are blocked; any migration requires another accepted governance decision with explicit rules, never an import or new grant that ignores the past. |

Governance record status has the closed set `Proposed`, `Accepted`,
`Superseded`. Only `Proposed -> Accepted` through the final gate; only
`Accepted -> Superseded` upon recording an accepted replacement. Other movements
are invalid. A new version starts in Proposed. An unknown version of a
restriction cannot be discarded by a new reader: a project with records
from another version results in `deny_version`. The Policy-ID/Vocabulary-Version pair
is handled in P06, not as an unknown vocabulary value in P01.

## Purpose, data and destination vocabularies

| Exact purpose | Meaning and consequence to show in the preview |
| --- | --- |
| `corpus_build` | Select/copy content into a local governed corpus or dataset. Creates an independent copy; does not allow export, training or sharing. |
| `dataset_export` | Produce a dataset package for an exact destination. Creates a copy that may escape local control; does not authorize the recipient to train or publish. |
| `model_training` | Use data to adjust parameters, including fine-tuning. The model may retain information; revocation does not instantly undo learning. |
| `model_evaluation` | Use data as evaluation inputs, references or benchmarks. Results/logs also need inventory and deadline; does not allow training. |
| `research` | Analyze content in a research activity bounded by snapshot/preview. Does not allow export, training, model evaluation or publication by inference. |
| `contribution` | Deliver data to a private recipient for an identified collection or initiative. Not a waiver of rights or unrestricted license. |
| `redistribution` | Make a copy of data or model artifact available to the approved recipient, including public publication. Recalling copies may be impossible; the local assertion does not license third-party work. |

One purpose does not imply another: evaluation during training requires both; export
for contribution requires `dataset_export` and `contribution`; publishing the package also
requires `redistribution`; building corpus for training requires `corpus_build` and
`model_training`. The request declares the operation's complete set, and
no payload field chooses classification. A still unclassified use
denies. There is no “translation” purpose in this list: the operational flow is in the
operations table, without being a corpus grant.

| Exact data class | Unit and limit |
| --- | --- |
| `source_text` | Visible source text; no assets/DRM, hidden metadata or code. |
| `translation_text` | Isolated translated text, still potentially protected and identifiable. |
| `aligned_pair` | Aligned source/translation pair, treated as a distinct unit; not inferred from the two isolated grants. |
| `review_annotation` | Human review annotation, without automatically incorporating book text. If it incorporates it, also declare the corresponding content class. |
| `provenance_metadata` | Only allowlisted lineage references and codes, without personal identity or path. Metadata remains sensitive. |
| `model_artifact` | Derived model artifact, with complete ancestry; not presumed anonymous or free of memorized content. |

A split pair or transformed data is a new request with its exact classes;
there is no authorization through conversion, claimed anonymization or file extension.
A pair's snapshot identifies both components separately to check
rights. Unlisted data, including assets, generic prompts, credentials
and supporting documents, is not exportable under this policy.

| Exact destination class | Binding and limit |
| --- | --- |
| `local_managed` | Storage/runner managed by Ayvu in this local context; not an arbitrary external file or process that forwards data. |
| `private_recipient` | A specific nonpublic recipient previously registered locally; even on loopback, if outside Ayvu control. |
| `public_release` | A specific previously registered public channel; future access and copies are uncontrollable. |

Each grant has exactly one class and a `destination_ref` resolving
to an immutable **local** catalog descriptor reviewed by a human. Catalog
and its runtime are future; until they exist, resolution fails and use denies.
The descriptor binds recipient, storage/runner and configuration version;
it does not receive URL, class or recipient from a document, imported manifest, AI/MCP
or request as a source of authority. Changing recipient/configuration or
redirecting requires a new reference and new preview. Same class with a different
reference is a different destination. Network enforcement checks its own
endpoints, redirects, proxies and offline mode; the grant does not expand them.

| Purpose | Valid data classes | Valid destination classes |
| --- | --- | --- |
| `corpus_build` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed` |
| `dataset_export` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed`, `private_recipient` |
| `model_training` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `local_managed`, `private_recipient` |
| `model_evaluation` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `local_managed`, `private_recipient` |
| `research` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `local_managed`, `private_recipient` |
| `contribution` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata` | `private_recipient` |
| `redistribution` | `source_text`, `translation_text`, `aligned_pair`, `review_annotation`, `provenance_metadata`, `model_artifact` | `private_recipient`, `public_release` |

The table is exhaustive: the purpose × data × destination Cartesian product of
the record/request must be a subset of the valid cells. Any
other whole combination is invalid, without using only the valid part.
An operation with local and external effects splits into requests with a single
destination; all must authorize before each effect, without tacit partial approval.

## Finite scopes and identity

All scopes include `project_id`, `manifest_revision` (exact positive
integer) and `snapshot_ref`. The snapshot is immutable, locally reviewed, with 1 to
4,096 atomic subjects; it contains only references, versions and relations, never
text. No scope covers future subjects or revisions. Another project,
clone/import or new manifest revision does not inherit authority. Even a
revision that only changes a location requires a new explicit scope review;
this is the conservative option until a more specific contract exists.

| `scope_kind` | Mandatory additional selection | Exact coverage |
| --- | --- | --- |
| `project` | `work_ref=null`, `segment_ref=null`, `subject_ref=null` | Finite set from that project/revision's snapshot. |
| `work` | `work_ref`; `segment_ref=null`, `subject_ref=null` | Snapshot subjects bound to that work/local versioning. Does not cover another translation by similarity. |
| `segment` | `work_ref`, `segment_ref`; `subject_ref=null` | Snapshot subjects from that segment/revision. Review positional index is not identity. |
| `subject` | `work_ref`, `subject_ref`; `segment_ref` according to the immutable mapping (null only for a subject without segment) | One exact snapshot subject, possibly representing an artifact with recorded ancestry. |

For this contract, `subject_ref` uses the closed namespace
`ayvu-rights-subject`, integer version `1`, random UUID4 value. It is a proposed local
mapping for DEC-014's opaque `SubjectRef`, not a new retroactive meaning
for its free namespaces. The mapping references work, segment
when present, identity version and lineage; collision, multiple resolution,
truncated lineage, unknown identity or absence of this mapping deny.
`work_ref` and `segment_ref` identify immutable bindings in that snapshot, without
inventing a universal work ID absent from #134. The fingerprint remains in the
existing manifest; its hash is not copied into a policy record/tombstone.

Broad/specific relation is actual inclusion of resolved sets, not name
order or field count. Two scopes may overlap without containing one
another. All intersections are calculated before the decision. A request
with 4,097 subjects is invalid and must be split, preserving complete
checking and obligations for each part. No truncation allows use.

## Actors, origin and authority

| `actor_kind` / origin | May assert rights? | May create/reactivate/expand grant? | Other permitted transitions |
| --- | --- | --- | --- |
| `local_human` | Yes, through explicit operation and preview | Create new, including explicit replacement; never reactivate terminal ID | Refuse, withdraw/dispute assertion and revoke according to tables. |
| `policy_engine` | No | No | Invalidate use, calculate expiry and revoke for risk/change according to reasons; never grant. |
| `project_import` | No | No | Only nonauthoritative candidate data. |
| `document` | No | No | Only nonauthoritative content. |
| `ai_mcp` | No | No | Only nonauthoritative suggestion/output. |
| `provider` | No | No | Only nonauthoritative metadata/response. |
| `copied_history` | No | No | Only nonauthoritative history. |

The `actor_ref` of `local_human` is a random pseudonym for this context/project,
resolved by the authenticated local session, not a name, email, global account or
imported value. The application binds an explicit operation to a present human
session; receiving `actor_kind=local_human` is not enough. Provenance actors
`human`/`system` do not convert into these powers. The engine is not the human's legal
representative. Absence of a verifiable local actor denies.

The mandatory human preview is immutable and binds Policy-ID, vocabulary,
scope/snapshot, declared bases, purposes, classes, destination/recipient,
use/retention deadline, replaced restrictions and all consequences and
limitations of this record. The human confirms each dimension without preselection
of new purposes. A change between preview and effect invalidates the operation.
An accepted preview for rights is not acceptance for purpose: they are distinct
operations, references and evidence, even if the future UI groups them.
Only the local application service may issue IDs after confirmation;
serialized fields, preview replay, copied history and AI/MCP output cannot
create, reactivate, expand or impersonate a local record. Without future
local integrity/origin protection, deny, without trusting a UUID.

## RightsAssertion: evidence, fields and lifecycle

| Closed `basis` | What the actor asserts | `support_ref` |
| --- | --- | --- |
| `creator_asserted` | Created the selected material and asserts holding the relevant rights. Does not resolve incorporated third-party rights. | null or opaque reference |
| `permission_asserted` | Received permission/license applicable to the selected material. Ayvu does not verify terms or document. | Mandatory opaque reference |
| `public_domain_asserted` | Asserts that the selected material is public domain in the relevant context. Not legal certification. | null or opaque reference |

No basis is deduced from purchase, cache, TM approval, import, product
terms or submission to provider. Legal exception is not represented in v1; do not
invent `other` or turn uncertainty into public domain. A future basis
requires a new version and appropriate human/legal review.

Exact `RightsAssertion` fields: `record_id`, `policy_id`,
`vocabulary_version`, `scope`, `data_classes`, `basis`, `actor_kind`, `actor_ref`,
`asserted_at`, `expires_at`, `expiry_kind`, `support_ref`, `preview_ref`,
`state`, `replaces`, `terminal_at`, `terminal_reason`. `scope` follows the table;
`data_classes` has 1 to 6 values and does not authorize purpose. `actor_kind` must
be `local_human`. `replaces` has 0 to 64 distinct local IDs. The same limit
applies to the grant; beyond that, explicit staged reconciliation maintaining denial.

`expiry_kind` accepts only `fixed_utc`. The rule is
`asserted_at < expires_at <= asserted_at + 31.536.000 s` (365 days).
`terminal_at` and `terminal_reason` are null in `asserted`; in terminal states
they are mandatory, with time not before the assertion. An existing ID's basis,
scope, classes, actor, evidence or validity are not changed.

| Storable state | Permitted transition | Effect |
| --- | --- | --- |
| `asserted` | Local creation; may move to `withdrawn` or `disputed` | Candidate, never grant. Effective only within deadline and version. |
| `withdrawn` | From `asserted`, human, reason `human_withdrawal` | Terminal, blocks scope/classes and revokes dependent grants. |
| `disputed` | From `asserted`, human or engine, reason `rights_disputed` or `identity_ambiguous` | Terminal, blocks; dispute is not resolved by choosing another basis. |

Additional calculated effective states, never written as input state:
`expired` when `now >= expires_at`; `version_incompatible` when the version
pair differs. They also block. No renewal or reactivation of terminal
ID. A new assertion with new preview and new ID replaces only named IDs
with **identical** binding (scope/snapshot and classes), without erasing withdrawal/
dispute evidence. The preview requires the human to acknowledge each restriction.
Replacement and creation are atomic; failure maintains the block. No basis
has precedence over another. More than one effective assertion applicable to the same
subject/class is `deny_ambiguous`, except idempotent repetition of the same ID and
same content. Different content with the same ID is corruption (`deny_invalid`).

Each request subject/class requires exactly one valid assertion covering
it; `aligned_pair` also requires coverage of the `source_text` and
`translation_text` components; `model_artifact` requires coverage of the artifact
and all ancestors with content, according to their classes. Unknown,
truncated or rights-lacking ancestor denies. Assertions may come from distinct
local actors, but none replaces the grant's human actor.

## PurposeGrant: evidence, fields and lifecycle

Exact `PurposeGrant` fields: `record_id`, `policy_id`,
`vocabulary_version`, `scope`, `purposes`, `data_classes`, `destination_class`,
`destination_ref`, `rights_assertion_refs`, `actor_kind`, `actor_ref`,
`created_at`, `granted_at`, `expires_at`, `expiry_kind`, `retention_class`, `preview_ref`,
`state`, `replaces`, `terminal_at`, `terminal_reason`.

`purposes` contains 1 to 7 exact values; `data_classes` 1 to 6.
`rights_assertion_refs` contains 1 to 64 local assertion IDs for an
`active`; it does not resolve imported assertions. In `draft` or `denied`, it may be empty,
since denial does not require proof of rights. `actor_kind` is always `local_human`
on creation; revocation actor/time remain in separate terminal evidence.
A grant never contains legal documents or source text.

`created_at` is the local creation instant and never changes. For directly
created active/denied, created_at=granted_at=confirmed_at of the preview.
In draft, granted_at is null and expires_at is the proposed deadline, limited to
created_at+7,776,000 s; the draft is discarded after 86,400 s even if that deadline
is later. On draft confirmation, granted_at receives authoritative time;
a draft already due for discard or past its deadline cannot be activated. In active/denied,
`expiry_kind=fixed_utc` and
`granted_at < expires_at <= granted_at + 7.776.000 s` (90 days).
For `active`, `expires_at` does not exceed the earliest expiry of its assertions.
`retention_class=governed_copy_until_expiry` is the only value for grants.
Refusal also has a valid deadline for the record, but **does not lose its block**
when that deadline passes. `terminal_at/reason` are null in `draft` and `active`,
mandatory in `denied` and `revoked`; refusal uses `human_refusal` and
terminal_at=granted_at, the refusal confirmation instant, including when leaving
draft. Every purpose/data/destination combination follows the exhaustive table.

| Storable state | Permitted transitions | Consequence in plain language |
| --- | --- | --- |
| `draft` | Create locally; `draft -> active` or `draft -> denied`, only human with new current preview | Draft does not authorize, does not block another record merely by existing and cannot be imported as authority. |
| `active` | Create directly with confirmation or leave draft; `active -> revoked` | Allows only the listed use while all gates pass; never universal use. |
| `denied` | Creation or departure from draft by explicit refusal | Do not use this selection for this set of purposes/classes/destination; refusal prevails even against more specific grants. |
| `revoked` | Only from active, human or engine for permitted reasons | Stop new uses now; dependencies are blocked and receive withdrawal/deletion obligation. External copies may remain. |

Additional calculated effective states: `expired` and `version_incompatible`,
with the same time/version rule as assertions; they are not valid inputs.
Expiry means ceasing uses and issuing obligation, not renewal. `active` cannot
return to draft; `denied`/`revoked` do not return to active; no ID is
reactivated by import, TM approval, passage of time or new version.
All unlisted movements are invalid and return `deny_invalid`.
Active/denied fields are immutable except state/terminal_at/terminal_reason
in the permitted transition. During draft -> active/denied only state,
granted_at, preview_ref, rights_assertion_refs and applicable terminal fields may change,
atomically on confirmation. Selection, destination and proposed deadline do not change;
changing them requires another draft/ID. An incomplete draft may receive rights
references only at that confirmation, which reassesses all conditions.

A new choice requires new ID/preview. `replaces` removes from effective selection
only explicitly named restrictions with **entirely identical** binding:
Policy-ID, scope/snapshot, purpose set, classes and destination/ref.
It does not change history, existing obligations or dependent records.
Replacement across versions is invalid in v1; there is no automatic equivalence
or implicit cross-version reconciliation operation. Two successors
of the same record are ambiguity and block. Incomplete replacement remains
denied. A specific grant does not cancel broad refusal; to change that choice,
the human must review the entire broad refusal and then create the exact
desired restrictions. No implicit exception or choice of the newest record.

## Operations and independent gates

The ordinary rows below still require normal project/file
authorization, human selection and content safety. No implicit legal
validation. They are classification to preserve the current product, not new permission
to automate export or share data. Discovery, acceptance of terms
and an allowed URL do not grant purpose.

| Closed operation/classification | Purpose gate | Other gates and limit |
| --- | --- | --- |
| `project_local` — create/import/inspect project, read/extract visible text, generate translated EPUB or Markdown/review for the project's own use | Does not require PurposeGrant | Ordinary authorization and operator's local destination. Exporting a dataset does not fit this row by changing extension or name. |
| `cache_local` — create/query/retain/clean operational cache, import/export its interchange for local personal use | Does not require PurposeGrant | Separate cache; using interchange for research/corpus/delivery requires governed purposes. Current retention does not change. |
| `tm_local` — create/reuse personal TM or approve TM in the project | Does not require PurposeGrant from this policy | Quality approval is neither rights basis nor corpus. #161 still owns the approved memory lifecycle; no cross-project reuse is inferred. |
| `provider_operational` — send selected visible text to the translator to produce the requested result | Does not require PurposeGrant for that operational processing | Separate ordinary and network authorization, with explicit recipient/limits. Provider terms/use do not authorize its training, evaluation, research or resale. If those uses cannot be separated, the future gate denies that submission. |
| `governed_use` — any of the seven purposes, even entirely local | Requires independently effective RightsAssertion and PurposeGrant | Project, identity, inventory/lineage, purpose and, if there is egress, network must pass. Export/distribution/contribution never masquerade as cache or ordinary output. |

Unknown operation is `deny_unknown`. Classification comes from a reviewed local
use case, not book text, file extension, tool name
or purpose declared by an untrusted requester.

| Ordinary authorization/identity | Purpose (when required) | Network (when there is egress) | Result |
| --- | --- | --- | --- |
| No | Any | Any | `deny_project` |
| Yes | No | Any | Deny for the purpose reason |
| Yes | Yes | No | `deny_network` |
| Yes | Yes or exempted by ordinary operation | Yes or no egress | Only then allow the bounded effect |

No “local” socket is exempt from the network gate. `local_managed` only exempts
network if no transport occurs. Network permission never supplies purpose;
a PurposeGrant never expands destination, offline mode or provider authorization.

## Algorithm and complete effective policy tables

The closed result is `allow` or `deny` with exactly one of the codes below.
P00–P18 order fixes the reason when there are multiple errors; **all** conditions
must pass. Evaluation does not continue after error to seek a more
permissive grant. Conflict/scenario tables assume previous gates are
satisfied, except when explicitly indicated. Since this record is still
Proposed, any real governed attempt for v1 stops at P00.

The application classifies the operation before this evaluation. P00 and P05–P16 apply
only to `governed_use`. Ordinary operations traverse only P01
(operation identifier), P02 (own structure), P03 (ordinary authorization/identity),
P04, P17 and P18; they do not receive grant/assertion fields or become
blocked by Proposed. This distinction does not change the delivered CLI.

A normalized request Q contains operation, project/revision, subjects/classes,
complete purpose set, exact destination and clock instant.
“Applicable” means nonempty intersection of subjects **and** purposes
**and** classes, with the same destination/class. Assertions have no
purpose/destination dimension. A set impossible to resolve is not an empty set.
Malformed authoritative records that do not allow disjointness to be proved block
all governed uses of the project until reconciliation. Imported data remains
outside the authoritative set, unable to create arbitrary blocks in the registry.

| Order | Evaluated condition | Result / code |
| --- | --- | --- |
| P00 | Record not Accepted, acceptance incomplete or active version without its own acceptance | `deny / deny_governance` |
| P01 | Unknown operation, purpose, class, destination, scope_kind, actor, basis, state, retention, expiry_kind or reason | `deny / deny_unknown` |
| P02 | Invalid structure, invalid combination, clock missing/going backward, ID with different contents, invalid replacement or local origin without integrity | `deny / deny_invalid` |
| P03 | Project without ordinary authorization, identity/snapshot missing, ambiguous, stale, truncated or outside binding; terminal inventory pending/incomplete | `deny / deny_project` |
| P04 | Correctly classified ordinary operation | Exempt P05–P16; still apply P17 and P18. Do not create grant/assertion. |
| P05 | Attempt to use import, document, AI/MCP, provider, forged actor or copied history as authority, or preview missing/unbound | `deny / deny_authority` |
| P06 | Incompatible/missing version pair in required record or unreconciled restriction | `deny / deny_version` |
| P07 | Applicable assertion withdrawn/disputed, expired or branched replacement; two effective assertions per subject/class | `deny / deny_rights` for withdrawal/dispute/expiry; `deny / deny_ambiguous` for multiplicity/branching |
| P08 | Any subject/class/component/ancestor of Q lacks the required single effective assertion | `deny / deny_rights`; does not yet examine grant refs. |
| P09 | Applicable `denied` or `revoked` PurposeGrant not explicitly replaced | `deny / deny_explicit` or `deny / deny_revoked`, respectively; if both, `deny_explicit` |
| P10 | Applicable active PurposeGrant has already expired, even if another valid active exists | `deny / deny_expired`; do not return to old/broad grant |
| P11 | More than one active candidate covering all of Q, or conflicting successors | `deny / deny_ambiguous`; idempotent duplicate of same ID/content counts once |
| P12 | No existing authoritative non-draft grant for the project | `deny / deny_absent` |
| P13 | No single remaining active covers all subjects/revisions of Q | `deny / deny_scope` |
| P14 | None of those grants covers all purposes of Q | `deny / deny_purpose` |
| P15 | None of those grants covers all classes of Q | `deny / deny_data` |
| P16a | None of those grants covers Q's exact destination/class/ref, or descriptor/recipient does not resolve | `deny / deny_destination` |
| P16b | The sole complete candidate does not reference all P08 assertions, or references an ineffective assertion | `deny / deny_rights` |
| P17 | Egress without independent network authorization for current destination/configuration | `deny / deny_network` |
| P18 | All gates passed; exactly one grant covers all of Q when required | `allow / allow_bounded` |

P13–P16a sequentially filter the same candidate set, without combining
permissions from different records. A “remaining” active also requires
`granted_at <= now < expires_at`, valid preview and origin;
a future date is P02. Draft is never candidate or restriction; if only drafts exist,
P12 denies. An assertion with a future date is also P02. A grant not applicable due to another
purpose/data/destination does not block a valid grant of Q; it simply does not
authorize Q. Terminal/expired records block only their intersection, without
revoking other choices by inference. Pending version change is the conservative
exception explicitly stated in P06.

For P11, “covers all of Q” is only structural coverage of all subjects,
purposes, classes and destination; it does not depend on P16b's refs test. If there
is more than one, P11 denies before choosing any. P08 examines only Q's
rights, even without grant. If neither rights nor grant exist, P08 is the first
reason; V02 and cases intended to test grant absence assume valid A.
Refusal remains valid without rights, but P08 precedes P09 in the returned code:
without A and with denied, result is deny_rights; with valid A, deny_explicit.
Both situations deny, without erasing refusal.

| Records intersecting in Q | Deterministic decision |
| --- | --- |
| Broad allow + specific deny | `deny_explicit` in the intersection; outside it evaluate all gates. |
| Broad deny + specific allow | `deny_explicit`; specificity does not override refusal. |
| Broad allow + specific revocation, or inverse | `deny_revoked`; no broad/specific fallback. |
| Valid allow + applicable expired allow, broad/specific or partially overlapping | `deny_expired`; a new grant does not clear prior expiry without explicit replacement. |
| Two allows covering Q, broad/specific, same instant or different instants | `deny_ambiguous`; never choose newest, first or most-permissive. |
| Two only partial allows whose union would cover Q | Deny at the first failing P13–P16 filter; do not stitch scope/purpose/data/destination. |
| One allow covers Q and another only part, both valid | Allow if all other gates pass; the partial neither expands nor decides Q. |
| Deny/revoked/expired in disjoint scope, nonintersecting purpose/data or another destination | Neither authorizes nor vetoes Q; check Q's own grant. |
| Same ID, same content repeated | One idempotent occurrence; does not create authority if imported. |
| Same ID with different contents, or ambiguous origin | `deny_invalid`; never break ties by timestamp. |
| New grant with complete replaces, identical binding and local confirmation; no other restriction | Predecessors cease deciding new uses; history and obligations remain. Reevaluate all of P00–P18. |
| New grant without replaces or with different binding | Does not clear restriction; declared invalid replacement is P02. |

For multi-subject requests, a deny on one denies all of Q; the
caller may propose another request without that subject, never silently
omit it. Assertion and grant are checked during planning, before
each read/copy/egress and before publishing each artifact. The decision is
not an unlimited token: revocation between stages prevents the next effect. An
irreversible effect already started is recorded as such and follows the limitations below.

## Revocation and bounded obligations

`terminal_reason`/`revocation_reason` use exactly the values below.
A rights withdrawal reason cannot be used as initial grant refusal
without recording the correct transition. Revocation actor is verified in the
local operation; its serialized field is not enough.

| Reason | Permitted actor / origin | Effect and consequence before confirmation |
| --- | --- | --- |
| `human_refusal` | local_human; creation of denied | Blocks new overlapping choices; not proof that previous uses were unlawful. |
| `human_withdrawal` | local_human; assertion withdrawn or grant revoked | Stop using now; managed derived copies must be withdrawn; third parties may already have them. |
| `rights_disputed` | local_human or policy_engine; assertion disputed and dependent grants revoked | Uncertain rights block, without legal judgment; do not resolve with a broader assertion. |
| `identity_ambiguous` | local_human or policy_engine; assertion disputed and dependent grants revoked | Untrusted scope/lineage blocks until new identity review. |
| `destination_changed` | local_human or policy_engine; grant revoked | Do not send to changed recipient; repeat preview for new destination, without fallback. |
| `policy_changed` | policy_engine or local_human; grant revoked | Old acceptance is not valid in the new version; reconfirm limits/restrictions too, without automatic conversion. |
| `security_incident` | local_human or policy_engine; grant revoked | Stop future effects and isolate managed derivatives; do not promise to undo prior leak. |
| `expiry_reached` | policy_engine; effective expired state of assertion/grant | Deadline ended, new uses denied and obligations issued. Do not write as revoked just to hide expiry. |
| `explicit_replacement` | local_human; replaced event of assertion/grant | New decision with identical binding removes predecessor from selection, without reactivating ID or erasing obligations. |

Revocation confirmed at T is effective for every effect with clock `now >= T`;
expiry also at the exact instant (`now == expires_at`). It does not depend on worker,
restart, next synchronization or physical deletion. The operation records durable
evidence/obligation before confirming success; recording failure denies
use and does not pretend revocation was completed. Withdrawal/dispute/expiry of an
assertion immediately invalidates all grants depending on it. New
rights/grants do not resurrect already withdrawn datasets/artifacts.

For each terminal event, the future executor inventories dependents and issues
one idempotent obligation per event+artifact+type and, when present,
known recipient. Initial refusal without dependents generates zero obligations;
do not invent artifacts or delete unrelated copies. This decision creates no worker. Mandatory
limits: block selection/training/egress of derivatives as of T; inventory
dependents/lineage within 86,400 s; remove from selections and delete managed
copies by T+604,800 s (7 days); send request to known recipients within
86,400 s; record response or its absence by T+2,592,000 s (30 days).
Incomplete inventory is never success: keep project/derivatives blocked
and report `unfulfilled` obligation. Offline or powered-off system does not get
a new deadline: on return, block before any effect and report delay.

| Closed `obligation_kind` | Affected object and honest completion |
| --- | --- |
| `inventory_check` | A terminal event without complete inventory: block project, complete enumeration by T+86,400 s or record inventory_incomplete. Does not require inventing artifact_ref. |
| `managed_delete` | Dataset, package, evaluation, research result or derived managed copy: block and delete within 7 days. Failure remains explicit. |
| `recipient_withdrawal` | Known recipient: request cessation/deletion; recipient confirmation does not prove physical erasure. Without control/response, record limit. |
| `model_restrict` | Managed derived model: block new use/export now and eliminate managed copy within 7 days; decide retraining in another issue, without promising unlearning. |

| Closed `obligation_state` | Permitted transition / meaning |
| --- | --- |
| `pending` | Created at actual issuance instant >=T; only `pending -> fulfilled` or `pending -> unfulfilled`. due_at/delete_at remain anchored to original T, including retries. |
| `fulfilled` | Terminal: verified managed action or bounded request/response recorded; does not imply external erasure. |
| `unfulfilled` | Terminal: deadline missed, incomplete inventory or impossible action; incident remains visible. New attempt uses new ID linked to previous one, without erasing failure. |

The closed `obligation_result` field is `pending`, `managed_deleted`,
`recipient_confirmed`, `recipient_unverifiable`, `model_removed`,
`deadline_missed`, `inventory_incomplete`, `managed_unavailable`, `inventory_complete`. `pending` only with pending;
fulfilled only with `managed_deleted` for managed_delete, `model_removed` for
model_restrict, `recipient_confirmed` for recipient_withdrawal or
`inventory_complete` for inventory_check;
unfulfilled only with `deadline_missed` for any type, `inventory_incomplete`
for inventory_check,
`managed_unavailable` for managed_delete/model_restrict, or
`recipient_unverifiable` for recipient_withdrawal. Other combinations are invalid.
There is no “resolved” state hiding an unknown dependency.
Closure uses actual closed_at and may be late; after the deadline never fulfilled.
When a powered-off application returns, record unfulfilled/deadline_missed without
changing due_at/T. If enumeration revealed a gap within the deadline, use
inventory_incomplete. There is one inventory_check per event whose inventory cannot
be proved complete at T; if proved to have no dependents, do not
create an artificial obligation and mark inventory complete.

Operational cache, personal/approved TM and original are not automatic targets
of this deletion: a grant does not change their retention. Removing a governed derivative
referencing a TM entry does not delete the TM entry. Disputed rights
may require separate action in those systems under their own policy; this
proposal does not authorize deleting source/original EPUB or changing visible text.

Limits to show **before** any future confirmation: local deletion
is not secure erase; backups, unmanaged exports, recipients and already
distributed copies may persist; there is no guarantee of retroactive deletion at
third parties, public recall or instant unlearning. Already delivered model artifacts
cannot be remotely deleted by this policy. The obligation
is to cease new controlled uses, withdraw managed derivatives and request withdrawal
where there is a known recipient; do not promise control over what is not controlled.

## Retention, minimization and tombstones

Proposed deadlines are product ceilings for human acceptance, without claiming legal
obligation. No infinite deadline or “retain as needed” is allowed.
There is no implicit legal hold: a future legal requirement needs a separate
decision, without inserting private documents here or automatically expanding TTL.

| Closed `retention_class` | Object and start of deadline | Expiry/exact rule |
| --- | --- | --- |
| `policy_source` | RightsAssertion/PurposeGrant and allowlisted preview | Keep while valid candidate/draft; draft expires 86,400 s after creation and is discarded. At terminality, expiry or replacement T, delete body/preview by T+2,592,000 s (30 days), preserving tombstone and evidence/obligations only within their respective deadlines. |
| `support_link` | Opaque assertion support reference | Used only while candidate assertion; then only for withdrawal review, delete by T+2,592,000 s. Never store document, path or credential; do not control external document retention. |
| `revocation_evidence` | Terminal event, allowlisted refs and obligations | Delete detailed body by T+7,776,000 s (90 days), even with unfulfilled obligation; reduce final result to tombstone. Do not extend through attempt/restart. |
| `audit_tombstone` | Minimal terminality/decision metadata, starting at T | Delete by T+31,536,000 s (365 days), including pseudonymous actor and dependency refs. Refusal no longer has a record, but absence still denies; do not resurrect ID or import history. |
| `governed_copy_until_expiry` | Governed copy/dataset/result/model | Maximum deadline equals earliest expires_at of ancestral grants and assertions; cease use at that instant, delete managed copies by +604,800 s. Renewal does not extend existing copy without new review and new derivative identity. |

`terminality T` is the first of: withdrawal, dispute, revocation, expiry,
replacement or version change. T is never recalculated from last access.
For denied, T is refusal confirmation (granted_at), not earlier creation of
a draft; do not issue new terminality or
restart TTL when expires_at passes. Expiry, incompatibility and replacement
keep the original stored state, but generate event/tombstone and effective
block; body terminal_at/reason remain null when the table requires it.
Calculated reasons are, respectively, expiry_reached, policy_changed and
explicit_replacement. Created events belong to the source (policy_source) and are
deleted with it by T+30 days; they do not have a 90-day TTL starting before T exists.
Deleting an entire policy first requires revoking active grants and issuing
obligations. Do not purge blocks earlier to obtain allow. After
deleting tombstone, no grant preceding T is reusable: new use requires new
preview/assertion/grant and new identity. If concurrent authorization is
affected by a restriction to be purged, invalidate it before purge, do not allow
fallback. The future implementation must prove this, including backup
restoration; inability to prove integrity after restore denies project.

The terminal body is reduced to tombstone within 30 days, **preserving the exact
restriction** in the allowlist below until its 365-day TTL. Do not delete the finite
subject set needed for intersection in P07/P09/P10. To correct
identity that does not allow snapshot equivalence to be resolved, v1 denies the project:
it does not allow replacement with a different scope or invent migration. The product
must refer that case to a separate identity/policy decision.

No permanent log of each read or denial is kept. During an
operation, a decision reason may exist in memory with TTL until the end of that
operation, at most 86,400 s. Persistent audit only for creation,
refusal, withdrawal, dispute, revocation, expiry, replacement and obligation
fulfillment, within the ceilings above. Display labels, real operator identity,
recipient address and preview details do not enter tombstone.

Exact event evidence allowlist: `event_id` (UUID4), `record_id`,
`policy_id`, `vocabulary_version`, `event_kind`, `effective_at`, `actor_kind`,
`actor_ref` (null only for engine), `reason` (null only on creation),
`obligation_refs` (zero IDs, except exactly one in obligation_closed), `retention_class`,
`delete_at`. `created` uses retention_class=policy_source; delete_at is null
while active source has no T, atomically set at T to T+30 days.
For draft, it is created_at+86,400 s; activating draft updates to null until T.
Others use revocation_evidence, with delete_at=T+7,776,000 s.
Obligations point to terminal event_id; it does not contain a copy of the
dependent list. The complete query by event feeds the minimal counters;
partial query keeps inventory_state=incomplete, never counts as success.
Closed `event_kind`: `created`, `denied`, `withdrawn`, `disputed`, `revoked`,
`expired`, `version_invalidated`, `replaced`, `obligation_closed`. Transitions correspond only to
lifecycle tables; expired is a calculated fact. `obligation_closed` has
exactly one obligation_ref and reason equal to the parent event's terminal reason;
state/result are read from the linked obligation, not implicit event
fields. The parent event is the obligation's already existing event_id, never the
obligation_closed itself; cycles and nonexistent reference are P02.
Each other event uses the corresponding reason from the table.
version_invalidated uses policy_changed, without changing the assertion's stored state;
grants may also have revoked/policy_changed. The first **terminal** event fixes T.
Do not write free data in `reason`.

Exact obligation allowlist: `obligation_ref`, `event_id`, `artifact_ref`
(UUID4 from local mapping; null only for inventory_check), `destination_ref`
(UUID4 mandatory only for recipient_withdrawal; null for others),
`obligation_kind`, `obligation_state`, `obligation_result`, `created_at`,
`due_at`, `closed_at` (null only in pending), `previous_ref` (null or UUID4),
`sent_at`, `response_at`, `receipt_ref`, `delete_at`. The three new fields are
null for managed_delete/model_restrict/inventory_check. For recipient_withdrawal, sent_at is
null until sending or the authoritative request time; response_at is null until
response, otherwise >= sent_at. Response and closure are recorded atomically,
with response_at <= closed_at; there is no response_at in pending. receipt_ref is a random
UUID4 of local confirmation of this very obligation, without additional support
object or external resolution (never response/text/address). It is issued by the
local adapter upon observing confirmation and deleted with the obligation at T+90 days;
mandatory for recipient_confirmed
with non-null sent_at/response_at, and optional for recipient_unverifiable.
Fulfillment requires sending by T+86,400 s; delay is deadline_missed, even with
later confirmation. inventory_check uses due_at=T+86,400 s;
managed_delete/model_restrict use due_at=T+604,800 s;
recipient_withdrawal uses due_at=T+2,592,000 s, with sending by T+86,400 s.
Its body follows revocation_evidence (90 days), and minimal result follows tombstone.

Exact tombstone allowlist: `record_id`, `record_kind`, `policy_id`,
`vocabulary_version`, `project_id`, `event_kind`, `terminal_reason`,
`effective_at`, `actor_kind`, `actor_ref` (null for engine), `preview_ref`,
`restriction`, `block_kind`, `inventory_state`, `obligation_summary`, `retention_class=audit_tombstone`, `delete_at`.
`record_kind` only `rights_assertion` or `purpose_grant`.
`block_kind` only `rights`, `explicit`, `revoked`, `expired`,
`version_incompatible`, `none`: withdrawn/disputed rights use rights;
denied grant uses explicit; revoked grant uses revoked; expiry uses expired;
version change uses version_incompatible; replaced uses none. Explicit
replacement also atomically updates predecessor block_kind to none and
restriction to null, without changing T/delete_at or original terminal fact/reason.
That none persists after deleting both bodies, without resurrecting refusal.
`restriction`
is null only for none; in other cases it has exactly `scope`, `subject_refs`,
`purposes`, `data_classes`, `destination_class`, `destination_ref`, with the
same immutable binding values and the list of 1 to 4,096 resolved subjects
from the snapshot. For rights_assertion, purposes is empty and both destination fields
are null; for purpose_grant, they are mandatory according to the original grant. This is
the minimal information to keep calculating intersection/replacement without
storing full preview, support evidence or content. The subject list
and necessary scope relations are subject to the same tombstone TTL, not the
source content TTL. No other permanent block index exists.
Closed `inventory_state` is `pending`, `complete`, `incomplete`, derived from
inventory_check; only complete releases P03. With proven absence of dependents, it is
complete with zero obligations. Pending/incomplete block the entire project,
even if block_kind=none; replacing grant does not resolve unknown inventory.
Assertion expiry in tombstone (record_kind=rights_assertion, block_kind=expired)
is P07 deny_rights; grant expiry is P10 deny_expired, not interchangeable.
`obligation_summary` is a tuple of the three nonnegative integer counters
`pending_count`, `fulfilled_count`, `unfulfilled_count`, each <= 2**63-1;
without obligation, all zero. Creation of tombstone only after terminality, with
corresponding terminal event_kind; it does not retain the full grant body.
delete_at is always effective_at+31,536,000 s; effective_at uses the first T,
not the last obligation. No optional field outside the nullabilities above.

P03 requires **positive proof of current complete inventory in each governed
operation**, not absence of tombstone. After the 90-day evidence TTL,
resolving inventory_state=incomplete requires full local reconstruction; without it,
it remains denied. Before purging tombstone at 365 days with inventory still
pending/incomplete, invalidate all project grants and discard the governed
authorization context (without deleting the ordinary project or original).
The next access is bootstrap without authority: reconstructed complete inventory,
new assertions, new grants and new previews are mandatory. If reconstruction
is impossible, P03 keeps denying for lack of the positive precondition, without keeping
an eternal marker. The future implementation must prove this bootstrap also
after restore, not infer complete from disappearing records.

Tombstones **do not authorize** or reconstruct grants. They retain only the binding
needed to deny, not text, title, content hashes, address/URL, personal
data or support. P07/P09/P10 read reduced restrictions the same way
as their predecessors. The concurrent block is invalidated before final
purge to prevent fallback to old grants.
Minimal record of a closed obligation preserves only counters;
obligation references disappear in 90 days. The restriction binding may
retain destination_ref and subjects for up to 365 days exclusively to deny;
they remain sensitive metadata, without name/address or claimed anonymization.

Privacy prohibitions apply to policy, persisted preview, events,
errors, repr, logs and tombstones: no source/translated text, book prompt,
supporting legal document, credential, absolute private path, name,
email, title or unnecessary personal identifier. Do not derive reference
from excerpt, title, email or secret by hash; digest is not anonymization.
The visual preview may show the human a selection they may already read, but its
persisted record contains only the allowlist, never a screenshot/text.
Exact persisted preview allowlist: `preview_ref`, `record_kind`, `binding`,
`actor_ref`, `issued_at`, `confirmed_at`, `consumed_at`, `confirmation_ref`,
`policy_id`, `vocabulary_version`, `expires_at`, `delete_at`. The binding of
rights_assertion has exactly scope, data_classes, basis, support_ref,
actor_kind, actor_ref, expiry_kind, expires_at and replaces. The binding of
purpose_grant has exactly scope, purposes, data_classes, destination_class,
destination_ref, rights_assertion_refs, actor_kind, actor_ref, expiry_kind,
expires_at, retention_class and replaces. These are the identically named fields of this contract,
all immutable in preview. Field names in this sentence are literal.
IDs and asserted_at/granted_at are associated by the service only on confirmation,
which records the unique preview_ref in the record; one preview_ref cannot create two
records. asserted_at/granted_at must coincide with confirmed_at.
confirmation_ref is UUID4 evidence of the local human session, without real
identity; it is not accepted from imported payload. confirmed_at/consumed_at/confirmation_ref
are null before confirmation; explicit confirmation of **all** dimensions
and consequences fills all three atomically and consumes the preview a single
time, without replay. issued_at <= confirmed_at=consumed_at < expires_at,
expires_at=issued_at+900 s (15 minutes); preview expiry does not extend the
grant deadline. delete_at is null for consumed preview linked to a record still without T;
it is atomically fixed at T to T+30 days. Draft preview follows created_at+1 day.
On issuance, delete_at=expires_at+86,400 s; on consumption, atomically apply the
linked record's rule above. Unconsumed expired preview is deleted
by expires_at+86,400 s. Consequences are those of this version, without free field,
recipient capture, text or document. Local proof of the act still requires the
anti-replay implementation/validation indicated in the open questions; until it
exists, P05 denies.

## Adversarial examples and review/test oracles

This matrix is the exhaustive vocabulary review index. A known value
does not mean allow: it only eliminates P01; other conditions remain necessary.

| Vocabulary / closed set | Validation and decision point |
| --- | --- |
| Policy-ID, Vocabulary-Version, governance status | P00/P06; no implicit translation across versions. |
| Purposes (7), data classes (6), destination classes (3) | P01/P02 for each valid/invalid cell of the 7×6×3 product; P14/P15/P16a for exact match. |
| scope_kind (4), subject namespace/version | P01/P02/P03; inclusion/intersection in P07–P13; missing identity is not empty scope. |
| actor_kind/origin (7), rights bases (3) | P01/P02/P05/P07/P08; only local_human creates, no basis has precedence. |
| Stored assertion (3) and grant (4) states | P02 for transitions; P07/P09; draft does not authorize. |
| Effective expired/version_incompatible states (2) | P06/P07/P10, derived exclusively from version/clock, never input. |
| expiry_kind (1), retention_class (5) | P01/P02; deadlines/source/TTL in retention table; grant retention_class only governed_copy_until_expiry. |
| Terminal reasons (9), event_kind (9) | P01/P02; combinations and actors from reason, lifecycle and evidence tables. |
| obligation_kind (4), obligation_state (3), obligation_result (9) | P01/P02; deadlines, combinations and terminal state in obligations table. |
| record_kind (2), block_kind (6), inventory_state (3), tombstone counters/refs | P01/P02; inventory P03; rights/expired of assertion P07, explicit/revoked P09, expired of grant P10, version_incompatible P06, none never authorizes. |
| Operations (5), results (2), decision codes (18) | Classify before P00; P00–P18 and gates table; any missing mandatory field denies. |

The only stored state change edges are below. Creation and
calculated effective state are not reactivation edges. Any unlisted state
pair, including self-transition, is invalid in P02; idempotent retry
of the same event is not a new transition.

| Lifecycle | Exact permitted edges |
| --- | --- |
| governance | `Proposed -> Accepted`, `Accepted -> Superseded` |
| RightsAssertion | `asserted -> withdrawn`, `asserted -> disputed` |
| PurposeGrant | `draft -> active`, `draft -> denied`, `active -> revoked` |
| obligation | `pending -> fulfilled`, `pending -> unfulfilled` |

Symbols: Q is an exact valid request; A a local assertion; G a grant;
B/S broad/specific scopes with proved inclusion; T is the injected clock.
There is no work text, real person, address, secret or path in examples.
Governance tests are documentary; no executable behavior is claimed
of registry, persistence or network that do not yet exist.
Except where the case tests inventory, scenarios assume current complete
inventory, including after obligations are issued; thus P03 does not mask the
purpose gate indicated in the oracle.

| Case | Input/attack or limit | Mandatory oracle |
| --- | --- | --- |
| V01 | Policy still Proposed, even with this issue's approval | P00 `deny_governance`. |
| V02 | Valid A, no G | P12 `deny_absent`; rights are not purpose. |
| V03 | Syntactically valid G, A missing | P08 `deny_rights`; UI “consent” does not supply A. |
| V04 | Unknown value in each P01 vocabulary (version/status are V01/V18 cases); extra consent key is a separate case | Value: P01 deny_unknown; extra key: P02 deny_invalid, without coercion. |
| V05 | G expires at T, clock at T−1 microsecond / T | Other gates valid: allow_bounded before, P10 deny_expired at T. |
| V06 | G revoked at T, another valid broad G | P09 deny_revoked at T; no fallback. |
| V07 | G B active; G S denied / G B denied; G S active | Both P09 deny_explicit in the intersection. |
| V08 | Two active G covering Q, broad/specific; reverse order or timestamps | P11 deny_ambiguous in both orders. |
| V09 | A expired/withdrawn/disputed, G active | P07 deny_rights, stop use and issue dependent obligation. |
| V10 | Q has invalid binding; separately, valid Q with current A, G in Q's project but distinct revision/segment, without another candidate | First case P03 deny_project; second P13 deny_scope. Clone without its own G and with current A is P12 deny_absent. |
| V11 | G only model_training, Q model_evaluation; or Q requires both | P14 deny_purpose; evaluation is not training. |
| V12 | G translation_text, Q aligned_pair; component rights valid | P15 deny_data; pair does not arise by composition. |
| V13 | Same destination-class, different destination_ref; or redistribution to public_release instead of private_recipient | P16a deny_destination; separately, purpose forbidden for public destination is P02 deny_invalid. |
| V14 | Valid A: grant allowed, network blocked; separately, network allowed and grant absent | P17 deny_network / P12 deny_absent. No gate supplies another. |
| V15 | Manifest import with actor_kind local_human and copy of preview/history | P05 deny_authority; zero creation/reactivation/expansion. |
| V16 | Document instructs grant/upload; AI/MCP returns approved/grant or provider claims rights | P05 deny_authority; only data, zero local authority. |
| V17 | Retained cache, personal/approved TM, purchase, accepted terms or provider use, without A or G | No A/G generated; corpus_build first denies at P08 deny_rights. With separate valid A, P12 deny_absent. |
| V18 | Current governance accepted; required record has old Policy-ID or partially known vocabulary | P06 deny_version; v1 does not migrate. Unaccepted governing version would be P00, separate V01 case. |
| V19 | Separate cases: new G without replaces for refusal; declared broad/specific replaces; two active successors without another restriction | Respectively P09 deny_explicit; P02 deny_invalid; P11 deny_ambiguous. |
| V20 | Two grants cover parts of Q; joining would give permission | P13–P16 at first empty filter, never union to authorize. |
| V21 | Truncated/unknown model lineage or pair without source rights | P03/P08; metadata does not resolve rights. |
| V22 | Isolated cases: snapshot does not resolve; preview changed; clock goes backward; same ID changes content | Respectively P03 deny_project; P05 deny_authority; P02 deny_invalid; P02 deny_invalid. |
| V23 | Revoke between planning and egress/publication | Recheck prevents next effect at T; idempotent obligation; already sent effect has explicit limit. |
| V24 | Request sent within 1 day, recipient does not confirm by 30 days; model already distributed | unfulfilled/recipient_unverifiable; block managed copies, without promising external erasure/unlearning. |
| V25 | Purge tombstone or restore old backup to resurrect G | Without current integrity/review, deny_invalid; old G never returns through tombstone absence. |
| V26 | Reactivate denied/revoked, renew expired ID or change same ID's binding; separately, create new record without preview | P02 deny_invalid in first group; P05 deny_authority in second. Only new valid human operation with new ID/replacement may authorize. |
| V27 | Draft, empty set, mandatory support null, grant expires after A | Draft does not authorize; others P02; only existing draft results in P12. |
| V28 | Source deadline 30 days, support 30, evidence 90, tombstone 365; restart application | No restart extends T/delete_at; delete at ceiling, without content in residues. |
| V29 | Corpus grant while cache/TM expires, or revoke corpus with TM retained | Independent retentions; no grant changes cache/TM TTL or deletes original. |
| V30 | Publication/merge/issue closure acceptance without separate reviewed change | Remains Proposed; P00 denies, registry implementation forbidden. |

## Rejected alternatives and open questions

| Alternative | Reason for rejection |
| --- | --- |
| Single “consent” object for rights, quality and purpose | Confuses unvalidated legal assertion with human authorization and opens TM/corpus escalation. |
| Opt-out, terms, purchase, cache/TM, import or provider as grant | Absence of explicit decision is not permission; sources have no local authority. |
| Payload-extensible vocabulary, free purpose or wildcard destination | Prevents exhaustive review; accepts authority change through data. |
| Newest/first/specific/most permissive wins | May bypass broad refusal, withdrawal or expiry; order and timestamp are not intent. |
| Merge partial grants to complete an operation | Creates a decision no human preview presented in full. |
| Eternal retention of audit, text or support to “prove rights” | Excess data, personal linkage and false legal promise. |
| Revocation waits for worker or promises to delete every model/copy | Window of improper use and impossible technical guarantee for third parties. |
| Start registry while proposal is reviewed | Crystallizes unaccepted semantics/human decision; binary gate requires waiting. |

Open questions, without provisional permission: (1) do the three bases cover intended
cases in which jurisdictions and how to obtain independent legal assessment;
(2) do the 365/90/30/7/1-day ceilings and absence of legal hold meet product and
local obligations; (3) which recipients and contractual guarantees may exist;
(4) which local authentication and anti-replay/integrity protection will prove the human
act without keeping unnecessary identity; (5) which stable work/segment/subject identity
implementation and descendant inventory will be accepted;
(6) which purposes/classes will actually be offered in the UI and how to show
their consequences without inducement; (7) whether public distribution of models/data
will even be offered; (8) how to reconcile purge and backup restoration
with current append-only provenance; (9) how to operate withdrawal at a recipient without
API, offline device or previously delivered model. Until answer/acceptance,
the governed path remains closed, without automatic legal choice.

## Binary human gate and acceptance checklist

An Ayvu repository owner/maintainer must accept **each** closed vocabulary,
precedence table, retention/tombstone rule, revocation limitation and
consequence in plain language. The exact action is a **separate,
reviewed change** that literally changes `Status: Proposed` to `Status: Accepted`
**in this record** and adds UTC date, responsible role and opaque/reviewable
reference to human acceptance of all items below. The review must
record acceptance explicitly; generic approval is not enough. This document
does not record such acceptance or mark items as approved.

Merging this Proposed, authorization to publish the issue, issue closure,
passing tests or acceptance of the threat model **do not satisfy** the gate.
Do not start rights/purpose registry implementation while Proposed.
Accepting the policy also does not authorize real data, grant PurposeGrant or
dispense with G03/G04 gates and implementation issues.

- [ ] Accept exactly the vocabularies/versioning, bases, actors, scopes and mappings, without wildcard, free field or consent alias.
- [ ] Accept separation of evidence, authority and RightsAssertion/PurposeGrant lifecycles, including invalid transitions and local human origin.
- [ ] Accept each P00–P18 row and each broad/specific/allow/deny/expiry conflict, without tie-breaking by order, timestamp or union.
- [ ] Accept the purpose × data × destination table and classification of all ordinary/governed operations, with independent network gate.
- [ ] Accept exact deadlines and allowlists for source, support, evidence, obligation and tombstone, purge/restore without resurrection and no retention of unnecessary content/identity.
- [ ] Accept each revocation reason/class, obligation/deadline/result and honest limits on third parties, backups, public copies and models.
- [ ] Accept all consequences and limitations the human preview must show before confirmation, including each purpose and terminal state.
- [ ] Resolve or record safe deferral of each open question, keeping cases without implementable contract denied.
- [ ] Record exhaustive governance review of values/transitions, adversarial V01–V30 review and privacy/link review; distinguish documentary review from executable test.
- [ ] Make the separate status change with explicit human evidence; only then plan registry in its own issues, without activating data operations through this acceptance.
