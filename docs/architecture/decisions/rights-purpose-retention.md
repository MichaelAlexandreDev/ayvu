# Revocation, retention and tombstones

Status: Proposed
Decision-Date: `2026-11-05`
Owners: Ayvu repository maintainer (acceptance and product); Policies (semantics); Knowledge (cache/TM/corpus); Projects + Provenance (identity and dependencies); Infrastructure (future retention and deletion execution).
Issue: [#165 — DESKTOP-134](https://github.com/MichaelAlexandreDev/ayvu/issues/165).

Companion of the [rights and purpose governance decision](rights-purpose-governance.md), under the same `Policy-ID: ayvu-rights-purpose-v1`. It is part of that proposal, not a separate one: the human gate in the [examples and acceptance gate](rights-purpose-examples.md) decides the whole policy.

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
