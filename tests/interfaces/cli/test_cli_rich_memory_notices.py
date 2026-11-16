"""With a model label, the memory flags say how they behave on the rich runtime; without one, nothing new is printed."""
from __future__ import annotations
import pytest
from tests.support.cli_translate_project_support import _book
from tests.support.cli_rich_runtime_support import config, invoke, isolate, text  # noqa: F401

V1_CACHE_NOTICE = "o cache v1 não é usado como memória"
THRESHOLD_WARNING = "--tm-apply-threshold só vale com --tm-conservative-auto-apply"


def flat(result):
    return " ".join(text(result).split())


def test_labeled_memory_says_the_v1_cache_is_not_memory(tmp_path, monkeypatch):
    config(tmp_path)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--translation-memory")
    assert result.exit_code == 0, text(result)
    assert V1_CACHE_NOTICE in flat(result)
    assert THRESHOLD_WARNING not in flat(result)


def test_an_explicit_threshold_without_conservative_apply_is_warned_and_the_run_continues(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"),
                               "--translation-memory", "--tm-apply-threshold", "0.9")
    assert result.exit_code == 0, text(result)
    assert THRESHOLD_WARNING in flat(result)
    assert "a memória aproximada fica só como sugestão" in flat(result)
    assert any(method == "POST" for method, _, _ in transport.requests)


def test_an_explicit_default_threshold_is_still_warned(tmp_path, monkeypatch):
    config(tmp_path)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"),
                       "--translation-memory", "--tm-apply-threshold", "0.95")
    assert result.exit_code == 0, text(result)
    assert THRESHOLD_WARNING in flat(result)


def test_the_threshold_with_conservative_apply_is_not_warned(tmp_path, monkeypatch):
    config(tmp_path)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"),
                       "--translation-memory", "--tm-conservative-auto-apply", "--tm-apply-threshold", "0.98")
    assert result.exit_code == 0, text(result)
    assert V1_CACHE_NOTICE in flat(result)
    assert THRESHOLD_WARNING not in flat(result)


@pytest.mark.parametrize("arguments", [
    ("--translation-memory",),
    ("--translation-memory", "--tm-apply-threshold", "0.9"),
])
def test_unlabeled_runs_print_no_memory_notice(tmp_path, monkeypatch, arguments):
    config(tmp_path, label=None)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), *arguments)
    assert result.exit_code == 0, text(result)
    assert V1_CACHE_NOTICE not in flat(result)
    assert THRESHOLD_WARNING not in flat(result)


def test_labeled_runs_without_memory_print_no_memory_notice(tmp_path, monkeypatch):
    config(tmp_path)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--tm-apply-threshold", "0.9")
    assert result.exit_code == 0, text(result)
    assert V1_CACHE_NOTICE not in flat(result)
    assert THRESHOLD_WARNING not in flat(result)


def test_a_refused_parallel_memory_run_prints_no_memory_notice(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"),
                               "--translation-memory", "--tm-apply-threshold", "0.9", "--workers", "2")
    assert result.exit_code == 1
    assert "--workers é incompatível com --translation-memory" in flat(result)
    assert V1_CACHE_NOTICE not in flat(result) and THRESHOLD_WARNING not in flat(result)
    assert transport.requests == []
