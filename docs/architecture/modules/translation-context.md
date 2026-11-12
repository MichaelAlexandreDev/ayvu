# Translation context

Implementation of [#164 / DESKTOP-133](https://github.com/DevStarrk1137/ayvu/issues/164).
#103, #129, #138, #140, #141, #142, #145, #153, #157 and #163 provide the consumed
contracts; #252 and #162 provide the rich runtime and conservative retrieval.
There is no linguistic quality evaluation or AI execution.

`TranslationContextBuilder` is pure application policy. It resolves no provider,
secret, file, network or tool. It imports no frameworks, format parser,
SDK, filesystem or persistence. It does not interpret instructions in text.

## Inputs and authorization

The caller resolves a `ContextSnapshot` with the exact `ResolvedProfileSnapshot`,
the #140 `UnitReference`, current unit and a bounded revision manifest.
Each `ContextEntryRevision` contains project, opaque reference, immutable revision,
content identity verifiable by the owner, kind, purpose, required flag
and structural position. Data category derives exclusively from the closed kind.

`ContextAuthorization` is authority already resolved by the caller. Its binding includes
project, operation, purpose `translation`, allowed kinds/categories, policy,
exact reference to the resolved profile snapshot, document/revision/unit and
UTC issue/expiry interval. Absence, future issuance, expiry, malformed
data or mismatch refuses before the first port, payload construction,
cache and provider. `issued_at <= now < expires_at` applies.
Renewal requires new preparation. Authority is neither inferred nor expanded.

`snapshot_context_sources` composes only explicit selections from owners:

| Kind | Minimum content | Restriction |
| --- | --- | --- |
| `profile` | effective target language | explicit option; style remains inert |
| `current_unit` | current unit's visible text | exact #140 revision; explicit option |
| `terminology` | managed term and translation | exact #157/#163 binding, provider stage and literal occurrence in unit |
| `protected_term` | preserved managed term | same restrictions; managed `required` is preserved |
| `neighbor` | text of an explicitly indicated unit | #140 structural relationship, same document/project |

There is no automatic discovery of neighbors, files or unrelated terms.
Post-cache glossary does not become provider context. Project, work and chapter notes
remain excluded, without a versioned owner/repository in this contract. References
grant no authority to open paths. Owners supply authentic snapshots
and immutable revisions; text changed against expected identity is `context_integrity`.

## Port and v1 limits

There is a single port, `ContextCandidateSource.read(ContextSourceRequest)`. It receives
only validated snapshot, exact requested kind set and `ContextSourceLimits`.
It does not apply policy, ranking or provider selection. Requests above ceilings fail
before calling the port. There is no other candidate repository.

| Execution limit | `bounded_context_v1` ceiling |
| --- | --- |
| selected entries | 64 |
| UTF-8 content per entry | 4,096 bytes |
| UTF-8 content actually represented | 32,768 bytes, including separating LF |
| neighboring units | 8 |

The manifest admits an N+1 metadata sentinel (65), without expanding the content
collection. More than 65 revisions fail. The port request has at most 64 entries,
4,096 bytes per entry and 32,768 returned bytes; it may only lower these
values. The return ceiling does not count separators added later. Selection
rechecks the representation ceiling with separators.

`SnapshotContextCandidateSource` is the production adapter over already resolved
revisions. `StoredContextRevision` stores length before `load(cap)`.
Count, individual size and sum are checked **before any load**.
Excess returns `ContextSourceOversize` without content for the entire batch:
required context fails; advisory context receives `limit` omissions. There is no partial
loading, summary, text cutting or chapter/book fallback. An owner with
lazy storage must respect the cap during loading; the current adapter uses
immutable strings already held by #140/#157. Length and identity are
rechecked in returned content. Duplicates, divergent revision,
kind/project expansion and content swapped under a revision are refused.

## Ordering and capabilities

Closed v1 precedence is profile, current unit, terminology, protected term,
neighbor. Tie breakers: position tuple, opaque reference and revision, using integer
and opaque ASCII string order. Locale and insertion order do not participate.
No decision uses a provider's name, URL or adapter type.

Entries are included whole in this order. Individual/aggregate/count excess,
neighbor limit or absent candidate generates an advisory omission with `limit`,
`neighbor_limit` or `missing`. A required entry in this situation generates a typed
failure. Space is not filled with other candidates. Warnings are deduplicated
in the fixed order `missing`, `limit`, `unsupported`, `neighbor_limit`. Changing ceiling,
precedence, tie breaker, omission or representation requires another policy version.

`ContextCapabilityEvidence` requires a #103 snapshot for the exact configuration and
UTC validity. `unsupported` loads no candidates and produces `unsupported`
omissions; with a required entry, it fails. `unknown`, `configuration_dependent`,
expired/future evidence or unknown limit result in
`context_preflight_required`. The caller executes #103 bounded preflight
and resubmits evidence; the builder never performs this I/O or assumes support.
Contradictory/malformed evidence fails closed.

The effective budget is the minimum of the local ceiling and known limits. #103
currently declares `context_characters`, not bytes. V1 conservatively reduces
byte ceilings to the character ceiling as well and checks both, without assuming
ASCII. An explicitly context-free provider receives normal text/locales,
without `ContextFeature`. This retains LibreTranslate's legacy HTTP body and
forwards `context_unsupported` through the interface's existing warning channel.

## Payload, cache and provenance

`joined_lf_v1` uses a #141 `ContextItem` with role `translation.context.model`, with
whole entries separated by LF. Thus #141's 16-item limit does not
change the 64-entry ceiling. The feature is required if any selected
entry is required. Payload and semantic projection are sensitive and stay separate
from `diagnostic()`, disclosure and provenance metadata.

The ordered canonical projection contains policy domain/version, effective limits,
representation version, references/revisions/content identities, kinds,
required flags and positions **only for what was selected**. It contains no text,
path, display name, secret or UI label. `OpaqueSemanticRevision` carries
this effect to each unit's #153 plan before raw lookup/write. Different content,
revision, order, policy, budget or representation invalidates raw results.
Omitted candidates and downstream style/glossary do not enter this identity.
Without a feature, the raw plan remains the previous one.

`ContextExecution` validates all authorizations before loading the first
candidate, prepares the set atomically and binds preparation to the exact
requests. `DocumentTranslator` checks all units against document
and plan before any cache, including pin resolution. It checks again
before sending chunks. Existing raw/cache/glossary order is preserved.
Raw artifact replay is disabled with context to avoid skipping revalidation.
Cache-only remains local, without querying gate or provider.

#138 records `build_translation_context` after the results artifact becomes
ready, linked to unit and artifact. The optional extension stores only
policy, selected kinds/opaque revisions, omitted kinds/reasons and bytes.
Counts come from bounded collections. It stores neither content nor context
projection/fingerprint. Old records omit the optional field entirely. Repr,
warnings, normalized failures and diagnostics use code/metadata allowlists.

## Data egress and composition

`ContextEgressDisclosure` contains an opaque destination resolved by the caller
(provider/configuration/destination/policy revision), project operation,
provider operation `translate`, purpose, categories, count and bytes of the
actual context representation. It contains no URL or text. Main text bytes
remain under the existing transport contract; disclosure measures
additional context, including separators.

`ContextEgressGate` returns an already resolved #159 decision, bound to the disclosure
hash. `require_context_egress` consumes `ResolvedProviderNetworkDecision`,
requires trusted issuer, same revision, exact binding and valid interval, and
refuses `strict_offline`/`unclassified`. It neither evaluates policy nor grants consent.
Denial, absence, expiry, future issuance or mismatch prevent the provider
call. The gate is revalidated immediately before each chunk call.
#159 HTTP policy remains mandatory in transport; disclosure does not
authorize URL, redirect, fallback or another destination. The caller binds the opaque
destination to the already resolved provider/configuration. Content does not participate in resolution.

`ProjectTranslation.translation_context` injects the authorized set into rich
composition, selected by the model label. Composition prepares/authorizes
before opening repositories or resolving provider; runtime revalidates before
preparing cache/provider. The effective revision enters
`TranslateProjectCommand.context_revision` and the job's `resolve_plan` only
when declared. Without context/label, legacy retains signatures, messages,
fingerprint and checkpoint bytes. There is no new flag/configuration to grant
authorization implicitly. An application consumer resolves snapshots,
authorization, capabilities, destination and gate, constructs `ContextExecution` and passes it
to composition. It also supplies `operation_id` matching authorization,
a valid `model_label` and explicit `cache_path`, according to the rich runtime
contract. A named profile alone does not activate context sending.

Cancellation is checked before loading, between bounded phases/entries, before
payload and before disclosure. Composition also observes bound batch
cancellation during preparation. Failure/cancellation leaves no partially
executable set, context artifact or cache lookup.

## Boundary with future AI functions

Documents, terms and provider responses are untrusted data. They cannot
choose project, source, file, tool, destination, secret, policy or
authorization. Characters, facts, style, relationships or consent are not inferred.
A future AI function will have another contract and explicit authority; it does not replace
this deterministic policy or data egress checks.

## Enforced contracts

- Authorization, ceilings and fail-closed admission:
  [authorization](../../../tests/translation/context/test_translation_context_authorization.py).
- Deterministic snapshots, bounded sources and semantic projection:
  [snapshots](../../../tests/translation/context/test_translation_context_snapshots.py),
  [source](../../../tests/translation/context/test_translation_context_source.py), and
  [projection](../../../tests/translation/context/test_translation_context_projection.py).
- Cache binding, replay restrictions, egress, cancellation and metadata-only provenance:
  [pipeline execution](../../../tests/translation/context/test_context_pipeline_execution.py) and
  [composition binding](../../../tests/composition/test_context_pipeline_binding.py).
