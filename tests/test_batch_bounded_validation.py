"""Batch argument rejection happens before translation or publication boundaries."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import ayvu.cli as cli
import ayvu.cli_composition as composition


@pytest.fixture
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    calls = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected boundary: {name}")
        return fail

    for name in ("_load_existing_config_or_default", "_run_translation", "_translate_in_project", "translate_without_project",
                 "run_translation_preflight", "TranslationCache", "_save_markdown_report",
                 "_save_missing_texts"):
        owner = composition if name in {"translate_without_project", "TranslationCache"} else cli
        monkeypatch.setattr(owner, name, forbidden(name))
    return tmp_path, calls


def _snapshot(root: Path):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def test_batch_over_100_rejected_before_execution_and_mutation(isolated_cli):
    root, calls = isolated_cli
    source = root / "book.epub"
    source.write_bytes(b"source sentinel")
    before = _snapshot(root)

    result = CliRunner().invoke(cli.app, ["translate", *([str(source)] * 101)])

    assert result.exit_code == 1
    assert "100" in result.output
    assert calls == []
    assert _snapshot(root) == before


def test_batch_explicit_missing_output_rejected_before_execution_and_mutation(isolated_cli):
    root, calls = isolated_cli
    sources = [root / "first.epub", root / "second.epub"]
    for source in sources:
        source.write_bytes(b"source sentinel")
    before = _snapshot(root)

    result = CliRunner().invoke(cli.app, ["translate", *(str(source) for source in sources),
                                          "--cache-only", "--missing-output", str(root / "missing.txt")])

    assert result.exit_code == 1
    assert "--missing-output" in result.output
    assert calls == []
    assert _snapshot(root) == before
