from dataclasses import replace

import pytest

from ayvu.formats.contracts import AliasVerdict, ExtractionRequest, FidelityAspect, FidelityLevel, FidelityRequirement, FormatOperationError, FormatRequirements, PreviewRequest, ReconstructionRequest, UnitTranslation
from ayvu.formats.local import LocalFormatSource
from ayvu.formats.service import FormatAdapter, FormatService
from tests.support.format_contract_env import PROJECT, CountingSource, FixedGuard, FormatEnv
from tests.support.format_fakes import CARDS_REGISTRATION, NOTES_REGISTRATION, CardsPorts, cards_descriptor, notes_adapter
from ayvu.kernel import JobFailure, JobFailureFamily

from tests.formats.format_adapter_support import BrokenPublisher
from tests.formats.format_adapter_support import cards, complete, documents_of, envdir
from tests.formats.format_adapter_support import materialize
from tests.formats.format_adapter_support import publish_with, registry_with, service_for
from tests.formats.format_adapter_support import with_method


def test_a_preview_can_hold_partial_results_and_a_reconstruction_cannot(envdir, cards):
    documents = documents_of(cards)
    some = complete(documents)[:1]
    previewed, env, publication = materialize(envdir, cards, kind=PreviewRequest, translations=(UnitTranslation(some[0].unit_id, "TRANSLATED"),))
    assert previewed.artifact is not None
    assert b"TRANSLATED" in env.read(publication) and b"second card" in env.read(publication)


def test_a_preview_is_refused_when_experimental_support_was_not_accepted(envdir, cards):
    result, env, publication = materialize(envdir, cards, kind=PreviewRequest, requirements_=FormatRequirements())
    assert result.failure.code == "experimental_not_allowed" and env.publisher.calls == 0


def test_required_fidelity_is_checked_before_reading_or_publishing(envdir, cards):
    source = CountingSource(LocalFormatSource(cards))
    needed = FormatRequirements(fidelity=(FidelityRequirement(FidelityAspect.LINKS, FidelityLevel.BEST_EFFORT),))
    result, env, _ = materialize(envdir, cards, requirements_=needed, source=source)
    assert result.failure.code == "unknown_fidelity" and source.reads == 0 and env.publisher.calls == 0
    needed = FormatRequirements(fidelity=(FidelityRequirement(FidelityAspect.STYLES, FidelityLevel.BEST_EFFORT),))
    result, env, _ = materialize(envdir / "env", cards, requirements_=needed, source=source)
    assert result.failure.code == "insufficient_fidelity" and source.reads == 0


@pytest.mark.parametrize("guard,code", [
    (FixedGuard("yes"), "output_alias_unknown"), (None, None),
], ids=["garbage-verdict", "fine"])
def test_only_an_explicit_distinct_verdict_lets_work_start(envdir, cards, guard, code):
    result, _, _ = materialize(envdir, cards, guard=guard)
    assert (result.failure.code if result.failure else None) == code


def test_a_guard_that_breaks_blocks_the_work(envdir, cards):
    class Broken:
        def verdict(self, source):
            raise RuntimeError("hunter2")

    result, env, _ = materialize(envdir, cards, guard=Broken())
    assert result.failure.code == "output_alias_unknown" and env.publisher.calls == 0 and "hunter2" not in repr(result)


def test_the_guard_runs_before_the_source_is_read(envdir, cards):
    source = CountingSource(LocalFormatSource(cards))
    result, _, _ = materialize(envdir, cards, guard=FixedGuard(AliasVerdict.SAME), source=source)
    assert result.failure.code == "output_is_source" and source.reads == 0


def test_a_request_of_the_wrong_kind_is_refused(cards, envdir):
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    service = service_for()
    assert service.reconstruct("fake_cards", LocalFormatSource(cards), PreviewRequest(documents, ()), env.publication()).failure.code == "invalid_materialization_request"
    assert service.preview("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, ()), env.publication()).failure.code == "invalid_materialization_request"
    assert service.reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), "here").failure.code == "invalid_publication"
    assert service.extract("fake_cards", LocalFormatSource(cards), "all").failure.code == "invalid_extraction_request"


def test_documents_from_another_adapter_are_refused(envdir, tmp_path):
    notes_path = tmp_path / "x.notes"
    notes_path.write_bytes(b"a note\n")
    notes = FormatService(registry_with((NOTES_REGISTRATION, notes_adapter()))).extract("fake_notes", LocalFormatSource(notes_path), ExtractionRequest(PROJECT)).documents
    env = FormatEnv(envdir / "env")
    result = service_for().reconstruct("fake_cards", LocalFormatSource(notes_path), ReconstructionRequest(notes, complete(notes)), env.publication())
    assert result.failure.code == "incompatible_adapter" and env.publisher.calls == 0


def test_documents_with_another_locator_are_refused(envdir, cards):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(locators=(("fake_card", 4),)), inspector=base, extractor=base, previewer=base, reconstructor=base, validator=base)
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    service = FormatService(registry_with((replace(CARDS_REGISTRATION, locators=(("fake_card", 4),)), adapter)))
    result = service.reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), env.publication())
    assert result.failure.code == "incompatible_locator" and env.publisher.calls == 0


@pytest.mark.parametrize("name,method", [
    ("reconstruct", lambda self, context, request: iter(["text, not bytes"])),
    ("reconstruct", lambda self, context, request: b"bytes iterate as integers"),
    ("reconstruct", lambda self, context, request: 7),
], ids=["str-chunk", "bytes-object", "not-iterable"])
def test_output_of_the_wrong_type_never_becomes_an_artifact(envdir, cards, name, method):
    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method(name, method), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure.code in ("malformed_output", "adapter_failed") and result.artifact is None and not env.is_ready(publication)


def test_an_adapter_failing_midway_leaves_no_artifact_and_keeps_its_reason(envdir, cards):
    failure = JobFailure(JobFailureFamily.INPUT, "corrupt_container")

    def reconstruct(self, context, request):
        yield b"partial"
        raise FormatOperationError(failure)

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method("reconstruct", reconstruct), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure is failure and not env.is_ready(publication)


def test_an_adapter_crashing_midway_is_reduced_to_a_code(envdir, cards):
    def reconstruct(self, context, request):
        yield b"partial"
        raise RuntimeError("Call me Ishmael.")

    base = CardsPorts()
    adapter = FormatAdapter(cards_descriptor(), inspector=base, extractor=base, previewer=base, reconstructor=with_method("reconstruct", reconstruct), validator=base)
    result, env, publication = materialize(envdir, cards, adapter)
    assert result.failure.code == "adapter_failed" and "Ishmael" not in repr(result) and not env.is_ready(publication)


def test_publication_failures_are_typed(envdir, cards):
    from ayvu.artifacts.model import ArtifactIntegrityError, ArtifactStoreError

    assert publish_with(envdir, cards, BrokenPublisher(error=ArtifactStoreError("disk_full"))).failure.code == "disk_full"
    failed = publish_with(envdir / "env", cards, BrokenPublisher(error=ArtifactIntegrityError("digest_mismatch")))
    assert (failed.failure.family, failed.failure.code) == (JobFailureFamily.INTEGRITY, "digest_mismatch")
    message = publish_with(envdir / "env" / "env", cards, BrokenPublisher(error=RuntimeError("hunter2")))
    assert message.failure.code == "publication_failed" and "hunter2" not in repr(message)


def test_an_artifact_that_is_not_ready_is_not_success(envdir, cards):
    assert publish_with(envdir, cards, BrokenPublisher(returned="ready")).failure.code == "publication_failed"


def test_the_artifact_lease_is_still_enforced(envdir, cards):
    env = FormatEnv(envdir / "env")
    documents = documents_of(cards)
    publication = replace(env.publication(), owner_token="0" * 64)
    result = service_for().reconstruct("fake_cards", LocalFormatSource(cards), ReconstructionRequest(documents, complete(documents)), publication)
    assert result.artifact is None and not env.is_ready(publication)
    assert result.failure.family is JobFailureFamily.RESOURCE
