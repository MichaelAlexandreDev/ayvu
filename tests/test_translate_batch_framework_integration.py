"""A non-CLI client binds local books to the production project workflow."""

import json

import pytest

from ayvu.batch_output_policy import OutputIntent
from ayvu.cache import TranslationCache
from ayvu.domain import LanguagePair, OutputPlan, TranslationOptions
from ayvu.epub_io import detect_epub_language
from ayvu.project_service import resolve_project
from ayvu.project_store import ProjectJsonRepository, build_project_request
from ayvu.translate_batch import BatchValidationError, ItemStatus
from ayvu.translate_batch_composition import LocalBatchItem, LocalTranslateBatch, project_item_outcome
from ayvu.translate_project import TranslateObserver
from ayvu.translate_project_composition import LegacyResources, ProjectTranslation, translate_in_project
from test_workflow_characterization import _write_epub


class Translator:
    def __init__(self):
        self.calls = []

    def translate(self, text, source, target):
        self.calls.append((source, target, text))
        return f"PT:{text}"


def test_client_resolves_each_book_lazily_and_executes_production_projects(tmp_path, monkeypatch):
    import ayvu.translate_project_composition as composition

    sources = (_write_epub(tmp_path / "english.epub", language="en"),
               _write_epub(tmp_path / "french.epub", language="fr"))
    original = [path.read_bytes() for path in sources]
    processing, output, reports = tmp_path / "processing", tmp_path / "out", tmp_path / "reports"
    output.mkdir()
    translator = Translator()
    opened, calls, resolved, report_paths, accepted_plans = [], [], [], [], []
    original_open = composition.open_translate_project

    def open_spy(*args, **kwargs):
        use_case = original_open(*args, **kwargs)
        opened.append(use_case)
        original_execute = use_case.execute
        def execute(command, **execution):
            calls.append((command, execution))
            return original_execute(command, **execution)
        use_case.execute = execute
        return use_case

    monkeypatch.setattr(composition, "open_translate_project", open_spy)

    def bind(source):
        def run(accepted, correlation, ordinal, cancellation):
            accepted_plans.append(accepted)
            language = detect_epub_language(source)
            resolved.append(language)
            request = build_project_request(source, source=language, target="pt",
                                            selections={"translator": "fake"})
            project = resolve_project(ProjectJsonRepository(processing), request).project
            translation = ProjectTranslation(
                processing, project.project_id, project.source_sha256, source,
                TranslationOptions(language_pair=LanguagePair(language, "pt")), accepted.output,
                False, lambda: LegacyResources(translator), lambda: TranslationCache(tmp_path / "cache.sqlite"),
                operation_id=correlation, accepted_output=accepted,
            )
            run_result = translate_in_project(translation, TranslateObserver(), cancellation=cancellation)
            assert run_result.report is not None
            accepted.mark_published()
            report_paths.append(accepted.write_auxiliary("report", f"{language}: report\n"))
            return project_item_outcome(run_result.result, report_saved=True)
        return run

    bindings = tuple(LocalBatchItem(OutputIntent(source, OutputPlan(output / f"{source.stem}-pt.epub"),
                                                  reports, f"{source.stem}-report"), bind(source))
                     for source in sources)
    service = LocalTranslateBatch(bindings)
    assert resolved == []
    result = service.use_case.execute(service.command)

    assert result.succeeded and [item.outcome.status for item in result.items] == [ItemStatus.SUCCESS] * 2
    assert resolved == ["en", "fr"]
    assert len(opened) == len(calls) == 2
    for index, (command, execution) in enumerate(calls):
        assert command.accepted_output.output.path == output / f"{sources[index].stem}-pt.epub"
        assert command.accepted_output is accepted_plans[index]
        assert execution["cancellation"] is not None
    outcomes = [item.outcome for item in result.items]
    for field in ("job_ref", "artifact_ref", "report_ref"):
        refs = [getattr(outcome, field) for outcome in outcomes]
        assert all(refs) and len(set(refs)) == 2
    manifests = [json.loads(path.read_text()) for path in processing.glob("projects/*/project.json")]
    assert {manifest["source"] for manifest in manifests} == {"en", "fr"}
    assert len({manifest["project_id"] for manifest in manifests}) == 2
    assert {source for source, _, _ in translator.calls} == {"en", "fr"}
    assert len(report_paths) == 2 and all(path.is_file() for path in report_paths)
    assert all((output / f"{path.stem}-pt.epub").is_file() for path in sources)
    assert [path.read_bytes() for path in sources] == original


@pytest.mark.parametrize("size", [0, 101])
def test_client_rejects_invalid_batch_size_before_any_item(tmp_path, size):
    calls = []
    source = _write_epub(tmp_path / "book.epub")
    item = LocalBatchItem(OutputIntent(source, OutputPlan(tmp_path / "out.epub"), tmp_path / "reports", "report"),
                          lambda *args: calls.append(args))
    with pytest.raises(BatchValidationError, match="invalid_item_count"):
        LocalTranslateBatch((item,) * size)
    assert not calls
