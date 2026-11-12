# Format contracts

The accepted scope and limits are recorded in the [decision](../decisions/format-adapter.md).

The contract below preserves the implementation stage recorded in #127;
references to later integrations describe that stage, not their current delivery status.

## One port per operation

`FormatAdapter` is a descriptor plus up to six ports: inspector, structured extractor, visible-content extractor,
previewer, reconstructor and validator. A read-only format has no reconstructor. Building an adapter fails with `descriptor_mismatch` when
a claim has no port, a port has no claim, or a port has the wrong shape.

So no consumer needs `hasattr` or a condition on a format's name. A test scans the contract for both.

## What a descriptor claims

A descriptor must state every operation and every fidelity aspect, because absence must not read as support.
A new member is therefore a code change that every adapter has to answer for.

| Operation support | Evidence | Port | Can run |
| --- | --- | --- | --- |
| `supported` | Required | Required | Yes |
| `experimental` | Optional | Required | Only if the caller accepts experimental |
| `unsupported` | None | None | No |
| `unknown` | None | None | No |

| Fidelity level | Evidence | Meets a requirement of `preserved` | Meets `best_effort` |
| --- | --- | --- | --- |
| `preserved` | Required | Yes | Yes |
| `best_effort` | Required | No | Yes |
| `lost` | Required | No | No |
| `not_applicable` | None | Yes, because there is nothing to lose | Yes |
| `unknown` | None | No | No |

A descriptor lists every locator schema it uses, since one format can place content in more than one way, such as
a body and a package file. An inventory lists each document's logical key and title, which is what an application
needs to resolve a selection before extraction.

The aspects are structure, inline markup, styles, images, links, navigation, metadata and internal names, and
they describe what preview and reconstruction write. A format that writes nothing cannot claim to preserve
anything. `maturity` is informative, and negotiation never reads it.

**Evidence is a reference, not a proof.** It names where a claim is checked, such as `contract:format_adapter_v1`.

## Negotiation

`negotiate_format` is pure. **Unlike [provider negotiation](../decisions/provider-capabilities.md), it has no "ask later":**
a provider can be probed at runtime, but fidelity cannot, so `unknown` is refused.

## Detection

Detection looks only at declared signatures, the extension and the media type. No adapter code runs, and at most
4 KiB is read.

| Evidence | Result |
| --- | --- |
| One signature matches | `match` |
| Two signatures match | `ambiguous` |
| A signature matches, and the extension belongs to a format with no signature | `ambiguous`, unless the extension is also the signed format's |
| No signature matches, and the extension belongs to a format with a signature | `no_match`, with `signature_conflict`, naming that format as a candidate |
| Only an extension, of one format with no signature | `match` |
| Only an extension, of two formats with no signature | `ambiguous` |
| Nothing | `no_match`, with `no_candidate` |

A format with a signature can never match by its extension alone. Nothing is guessed, and `ambiguous` hands the
decision back to the caller.

## Order of checks

Checks run cheapest first, and everything above "The adapter runs" happens with no adapter code running.

| Inspect, extract, visible-content export, validate | Preview, reconstruct |
| --- | --- |
| Adapter is known | Adapter is known |
| Negotiation | Negotiation, including required fidelity |
| Cancellation | Documents belong to this adapter and locator |
| Source size limit | Every result names a unit, and for reconstruction every unit has one |
| Source fingerprint | Destination is not the source |
| The adapter runs | Cancellation, size limit and fingerprint |
| Result matches the descriptor | Source still matches the documents |
| Source has not changed | The adapter runs, and its bytes go to the artifact store |
| | Source has not changed, before publication |

A result that contradicts the descriptor is `descriptor_mismatch`: wrong type, identity, locator, project,
fingerprint, an unselected document, or an undeclared format version.

## The original is never written

There are two layers.

1. **By construction.** An adapter receives a read-only stream and returns bytes. `FormatPublication` holds no
   path, and `ArtifactService` writes to its own staging area.
2. **By check.** `OutputGuard` answers `distinct`, `same` or `unknown`, and only `distinct` lets work start.
   `LocalOutputGuard` compares device and inode, so it recognizes the same path, `a/../b`, symlinks, chains of
   symlinks, linked directories and hard links.

**Limits of the check.** It is a moment in time, and a destination replaced afterwards is not noticed. A bind
mount can show a different device for the same file. `ExportVisibleContent` uses `LocalOutputGuard` while planning
and checks it again before publication; this does not provide a filesystem-wide lock against an adversarial race.

**A changed source** is caught by comparing fingerprints before work and again before publication, so reconstruction
reads the whole file at least twice. A change between the second fingerprint and the publication is not caught,
but the output was built before it, from what was checked.

## Cancellation

A claim declares its safe points per operation, and the suite cancels at each one and fails if one is never
reached. `format_safe_points` lists what a `CancellationSource` must declare. An adapter calls `context.checkpoint`,
which raises. An adapter that catches this still returns nothing, because the context remembers.

A cancelled EPUB artifact stays unpublished because the record is abandoned by `ArtifactService`. A cancelled
Markdown publication removes its staged file before returning the per-document failure; already published files
remain completed outcomes.

## Contract suite

`tests/support/format_contract.py` is the suite #144 must pass. `tests/support/format_fakes.py` holds two invented formats, one
read-only and one that round-trips, and they pass the same tests with different descriptors. Which checks apply
comes from the descriptor alone. Claiming less than an adapter can do is refused by the mismatch check, and
claiming more fails the suite.

## Privacy

`repr` holds no text, title, selection or path. Adapter exceptions are reduced to `adapter_failed` without reading
their message, and `FormatOperationError` is how an adapter reports a sanitized reason.

## Enforced contracts

- [test_format_adapter_contract](../../../tests/formats/test_format_adapter_contract.py).
- [test_format_contract_architecture](../../../tests/formats/test_format_contract_architecture.py).
- [test_format_contract_values](../../../tests/formats/test_format_contract_values.py).
- [test_format_detection](../../../tests/formats/test_format_detection.py).
- [test_format_local](../../../tests/formats/test_format_local.py).
- [test_format_registry](../../../tests/formats/test_format_registry.py).
- [test_format_service_extraction](../../../tests/formats/test_format_service_extraction.py).
- [test_format_service_materialization](../../../tests/formats/test_format_service_materialization.py).
