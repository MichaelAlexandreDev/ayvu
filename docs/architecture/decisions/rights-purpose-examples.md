# Rights governance examples and acceptance gate

Status: Proposed
Decision-Date: `2026-11-05`
Owners: Ayvu repository maintainer (acceptance and product); Policies (semantics); Knowledge (cache/TM/corpus); Projects + Provenance (identity and dependencies); Infrastructure (future retention and deletion execution).
Issue: [#165 — DESKTOP-134](https://github.com/MichaelAlexandreDev/ayvu/issues/165).

Companion of the [rights and purpose governance decision](rights-purpose-governance.md), under the same `Policy-ID: ayvu-rights-purpose-v1`. It is part of that proposal, not a separate one: the human gate in the [examples and acceptance gate](rights-purpose-examples.md) decides the whole policy.

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
