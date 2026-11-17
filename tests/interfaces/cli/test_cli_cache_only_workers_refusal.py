"""#365: without a model label, cache-only with several workers is refused clearly instead of crashing."""
from __future__ import annotations

import pytest

from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, text  # noqa: F401
from tests.support.cli_translate_project_support import _book

REFUSAL = "--cache-only com mais de um worker exige um rótulo de modelo."


def flat(result):
    return " ".join(text(result).split())


@pytest.mark.parametrize("arguments", [
    ("--workers", "2"),
    ("--performance-profile", "high"),
    ("--workers", "4", "--dry-run"),
])
def test_unlabeled_cache_only_with_several_workers_is_refused_before_any_work(tmp_path, monkeypatch, arguments):
    config(tmp_path, label=None)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--cache-only", *arguments)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert REFUSAL in flat(result)
    assert "Use --workers 1 com --cache-only" in flat(result)
    assert transport.requests == []
    assert not (tmp_path / "out.epub").exists()
    assert not (tmp_path / "processing").exists()


def test_unlabeled_cache_only_with_one_worker_is_not_refused(tmp_path, monkeypatch):
    config(tmp_path, label=None)
    result, _ = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--cache-only", "--workers", "1")
    assert isinstance(result.exception, (SystemExit, type(None))), repr(result.exception)
    assert REFUSAL not in flat(result)


def test_labeled_cache_only_with_several_workers_keeps_running_on_the_rich_runtime(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--cache-only", "--workers", "2")
    assert REFUSAL not in flat(result)
    assert isinstance(result.exception, (SystemExit, type(None))), repr(result.exception)
    assert transport.requests == []
