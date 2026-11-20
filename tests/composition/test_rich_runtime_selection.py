"""Opt-in composition contracts. Storage integration is in tests/interfaces/cli/test_cli_rich_runtime_execution.py."""
from __future__ import annotations
import ast
import pytest
import ayvu.composition.builders.translate_project as composition
from ayvu.composition.builders import rich_provider
from ayvu.provenance.model import ProvenanceValidationError
from tests.support.translation_cache_v2_support import document
from ayvu.kernel import TranslationOptions, OutputPlan, JobFailure, JobFailureFamily
from tests.support.rich_runtime_support import PAIR, runtime
from tests.support.paths import PROJECT_ROOT


@pytest.mark.parametrize("label", [None, "https://private-model", False])
def test_composition_refuses_a_present_invalid_label(label):
    with pytest.raises(ProvenanceValidationError, match="invalid_model_label"):
        rich_provider.selected_model({"libretranslate": label}, "libretranslate")


def _selection_case(tmp_path, monkeypatch, label):
    from types import SimpleNamespace
    from ayvu.translation.project_contracts import TranslateObserver, TranslateProjectResult
    opened, commands, preparations = [], [], []
    project_id = document().project_id
    monkeypatch.setattr(composition, "ProjectJsonRepository", lambda _: SimpleNamespace(
        resolve_database_paths=lambda _: SimpleNamespace(database=tmp_path / "project.sqlite", backups=tmp_path / "backups"),
        resolve_artifact_root=lambda _: tmp_path / "artifacts"))
    def execute(command, **kwargs):
        commands.append(command)
        return TranslateProjectResult(project_id, failure=JobFailure(JobFailureFamily.INPUT, "fixture_gate"))
    def open_case(*args, runtime, **kwargs):
        opened.append(runtime)
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(composition, "open_translate_project", open_case)
    request = composition.ProjectTranslation(tmp_path, project_id, "a" * 64, tmp_path / "book.epub",
        TranslationOptions(language_pair=PAIR), OutputPlan(tmp_path / "out.epub"), False,
        lambda: preparations.append("provider"), model_label=label, cache_path=tmp_path / "cache.sqlite")
    return request, opened, commands, preparations, TranslateObserver()


def test_production_composition_constructs_only_the_rich_runtime(tmp_path, monkeypatch):
    request, opened, commands, preparations, observer = _selection_case(tmp_path, monkeypatch, "local-v1")
    result = composition.translate_in_project(request, observer)
    assert type(opened[0]) is composition.RichTranslationRuntime and commands[0].execution_revision == 2
    assert preparations == [] and result.result.failure.code == "fixture_gate"
    assert commands[0].producer is not None
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


def test_a_project_translation_without_a_label_constructs_no_runtime(tmp_path, monkeypatch):
    """Admission refuses unlabeled work first; only the retired v1 runtime ran without a label (REF-18 step 6)."""
    from ayvu.translation.project_contracts import TranslateCommandError
    request, opened, commands, preparations, observer = _selection_case(tmp_path, monkeypatch, None)
    with pytest.raises(TranslateCommandError, match="model_label_required"):
        composition.translate_in_project(request, observer)
    assert opened == [] and commands == [] and preparations == []
    assert not composition.v2_cache_path(tmp_path / "cache.sqlite").exists()


def test_selection_remains_in_composition():
    root = PROJECT_ROOT / "src" / "ayvu"
    for name in ("translation/project.py", "translation/engine.py", "kernel/values.py"):
        tree = ast.parse((root / name).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not {"cache_repository", "translation_document", "provider_libretranslate", "composition"} & ({(m or "").split(".")[0] for m in imports} | set(imports))
