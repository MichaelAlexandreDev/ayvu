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
| [#129](https://github.com/MichaelAlexandreDev/ayvu/issues/129), [threat model](product-threat-model.md) | Accepted on 2026-11-05; T03 (destinations), T05 (cache/corpus), T09 (AI/MCP), T11 (retention/revocation), F06 and G04. That acceptance does not accept this policy. |
| [#134](https://github.com/MichaelAlexandreDev/ayvu/issues/134), [manifest](../modules/projects.md) | `project_id`, revision and exact source/configuration identity. Path, title and isolated hash are neither scope nor authority. `policy_ref` is not a grant. |
| [#138](https://github.com/MichaelAlexandreDev/ayvu/issues/138), [DEC-014/provenance](provenance.md) | Opaque references, revisions and lineage; actors are types, without people's names. An opaque subject does not prove identity or rights; copied history does not authorize. |
| [Module boundaries](modular-monolith-boundaries.md), [artifacts](job-artifacts.md), [official product scope](../../product-scope.md) | Policies decides; cache, TM and corpus are independent. An existing artifact does not prove permission. Content for corpus/training is not automatic contribution. |
| [Provider contracts](provider-contracts.md), [discovery](../modules/providers.md) | Configuration/discovery are neither network nor purpose authorization. |

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

## The rest of the policy

The policy continues in three companion records under the same Policy-ID:

- [Rights assertions, purpose grants and effective policy](rights-purpose-grants.md): RightsAssertion, PurposeGrant, operations and gates, the algorithm and the complete effective policy tables.
- [Revocation, retention and tombstones](rights-purpose-retention.md): bounded obligations, revocation, retention and minimization.
- [Examples and acceptance gate](rights-purpose-examples.md): adversarial examples and oracles, rejected alternatives, open questions and the binary human gate.
