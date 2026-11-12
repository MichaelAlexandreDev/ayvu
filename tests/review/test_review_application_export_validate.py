from __future__ import annotations

import socket
import urllib.request
from dataclasses import replace

import pytest
import requests.sessions

from ayvu.artifacts.model import ArtifactState
from ayvu.review.application import ExportReviewPackage, ReviewExportCommand, ReviewOutcome, ReviewPackageExport, ReviewValidationCommand, ReviewUseCasePorts, ValidateReviewPackage
from ayvu.translation.engine import EngineResult

from tests.review.review_application_support import DESTINATION
from tests.review.review_application_support import Operations, PACKAGE, PackagePort
from tests.review.review_application_support import UNIT_ID, _destination_fingerprint
from tests.review.review_application_support import _identity, _ports, _translation


def test_export_orchestrates_v2_publish_and_content_free_terminal_result():
    ports, packages, operations = _ports()
    exported = ExportReviewPackage(ports).execute(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))

    assert exported.outcome is ReviewOutcome.READY
    assert exported.artifact is not None and exported.artifact.state is ArtifactState.READY
    assert packages.exports == operations.publish_calls == 1
    assert operations.complete_calls[0][1] == (UNIT_ID,)
    assert exported.events == operations.events
    assert "PRIVATE_REVIEW_TEXT_CANARY" not in repr(exported)
    assert "PRIVATE_OUTPUT_PATH_CANARY" not in repr(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))


@pytest.mark.parametrize("identity_change", ["project", "machine_artifact"])
def test_export_rejects_identity_that_does_not_match_the_translation(identity_change):
    class MismatchedPackagePort(PackagePort):
        def export(self, translation, destination):
            self.exports += 1
            identity = _identity()
            if identity_change == "project":
                identity = replace(identity, project_id="00000000-0000-4000-8000-000000000160")
            else:
                identity = replace(identity, machine_artifact_id="00000000-0000-4000-8000-000000000161")
            return ReviewPackageExport(identity, PACKAGE, (UNIT_ID,))

    packages, operations = MismatchedPackagePort(), Operations()
    result = ExportReviewPackage(ReviewUseCasePorts(packages, operations)).execute(
        ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)))

    assert result.outcome is ReviewOutcome.FAILED and result.code == "translation_identity_mismatch"
    assert operations.begin_calls == [] and operations.publish_calls == 0


def test_export_refuses_translation_without_stable_unit_evidence():
    ports, packages, operations = _ports()
    translation = replace(_translation(), engine=EngineResult(documents=(), translations=()))

    result = ExportReviewPackage(ports).execute(
        ReviewExportCommand(translation, DESTINATION, _destination_fingerprint(DESTINATION)))

    assert result.code == "translation_identity_unavailable"
    assert operations.begin_calls == [] and packages.exports == 0


def test_validate_is_side_effect_free_and_has_instrumented_network_tripwires(tmp_path, monkeypatch):
    ports, packages, operations = _ports()
    sentinel = tmp_path / "unchanged.bin"
    sentinel.write_bytes(b"snapshot")
    before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
    transports = []

    def blocked(*args, **kwargs):
        transports.append((args, kwargs))
        raise AssertionError("network transport must not be used")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)

    result = ValidateReviewPackage(ports).execute(ReviewValidationCommand(PACKAGE))

    assert result.valid and result.validation_evidence_digest
    assert (sentinel.read_bytes(), sentinel.stat().st_mtime_ns) == before
    assert packages.validations == 1
    assert not operations.begin_calls
    assert operations.publish_calls == operations.reconstruct_calls == 0
    assert not transports


@pytest.mark.parametrize("safe_point", ["review_before_work", "review_before_validation"])
def test_validation_cancellation_stops_before_more_package_work(safe_point):
    ports, packages, operations = _ports()

    class Cancellation:
        def checkpoint(self, requested):
            return object() if requested == safe_point else None

    result = ValidateReviewPackage(ports).execute(
        ReviewValidationCommand(PACKAGE, cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    assert packages.validations == 0
    if safe_point == "review_before_work":
        assert packages.inspections == 0
    else:
        assert packages.inspections == 1
    assert not operations.begin_calls and operations.reconstruct_calls == 0


def test_export_cancellation_after_publish_never_returns_ready():
    ports, _packages, operations = _ports()

    class Cancellation:
        publications = 0

        def checkpoint(self, requested):
            if requested != "review_before_publication":
                return None
            self.publications += 1
            return object() if self.publications == 2 else None

    result = ExportReviewPackage(ports).execute(
        replace(ReviewExportCommand(_translation(), DESTINATION, _destination_fingerprint(DESTINATION)),
                cancellation=Cancellation()))

    assert result.outcome is ReviewOutcome.CANCELLED
    assert result.artifact is None and operations.package_published
    assert operations.complete_calls == [] and operations.fail_calls == ["cancelled"]
