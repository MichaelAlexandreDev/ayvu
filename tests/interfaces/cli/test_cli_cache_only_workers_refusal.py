"""#365 and REF-18: every unlabeled cache-only run is refused without creating resources."""
from __future__ import annotations

import pytest

from tests.support.cli_rich_runtime_support import Transport, config, invoke, isolate, text  # noqa: F401
from tests.support.cli_translate_project_support import _book

from tests.support.model_label_support import REFUSAL, assert_refused, snapshot


def flat(result):
    return " ".join(text(result).split())


@pytest.mark.parametrize("arguments", [
    ("--workers", "2"),
    ("--performance-profile", "high"),
    ("--workers", "4", "--dry-run"),
])
def test_unlabeled_cache_only_with_several_workers_is_refused_before_any_work(tmp_path, monkeypatch, arguments):
    config(tmp_path, label=None)
    source = _book(tmp_path / "book.epub")
    before = snapshot(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, source, "--cache-only", *arguments)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert REFUSAL in flat(result)
    assert_refused(result)
    assert snapshot(tmp_path) == before
    assert transport.requests == []
    assert not (tmp_path / "out.epub").exists()
    assert not (tmp_path / "processing").exists()


def test_unlabeled_cache_only_with_one_worker_is_refused(tmp_path, monkeypatch):
    config(tmp_path, label=None)
    source = _book(tmp_path / "book.epub")
    before = snapshot(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, source, "--cache-only", "--workers", "1")
    assert_refused(result)
    assert snapshot(tmp_path) == before and transport.requests == []


def test_labeled_cache_only_with_several_workers_keeps_running_on_the_rich_runtime(tmp_path, monkeypatch):
    config(tmp_path)
    result, transport = invoke(tmp_path, monkeypatch, _book(tmp_path / "book.epub"), "--cache-only", "--workers", "2")
    assert REFUSAL not in flat(result)
    assert isinstance(result.exception, (SystemExit, type(None))), repr(result.exception)
    assert transport.requests == []
