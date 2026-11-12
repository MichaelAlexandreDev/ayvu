# Rights assertions, purpose grants and effective policy

Status: Proposed
Decision-Date: `2026-11-05`
Owners: Ayvu repository maintainer (acceptance and product); Policies (semantics); Knowledge (cache/TM/corpus); Projects + Provenance (identity and dependencies); Infrastructure (future retention and deletion execution).
Issue: [#165 — DESKTOP-134](https://github.com/MichaelAlexandreDev/ayvu/issues/165).

Companion of the [rights and purpose governance decision](rights-purpose-governance.md), under the same `Policy-ID: ayvu-rights-purpose-v1`. It is part of that proposal, not a separate one: the human gate in the [examples and acceptance gate](rights-purpose-examples.md) decides the whole policy.

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
