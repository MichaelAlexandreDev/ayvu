"""Real CLI dry runs select only local, read-only rich execution with a label."""
from dataclasses import replace
import json
import re
import sqlite3

import pytest

from ayvu.composition.builders import translate_transient as builder
from ayvu.kernel import LanguagePair, TranslationOptions
from tests.support.transient_translation_support import (
    _book, config, isolate, text, seed, effect_guard, invoke, active_writer, runner, app,
)


def options(**changes):
    return TranslationOptions(LanguagePair("en", "pt"), dry_run=True, **changes)


@pytest.mark.parametrize("existing,warm", [(False, False), (True, False), (True, True)])
def test_real_catalog_and_exact_coverage(tmp_path, monkeypatch, existing, warm):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    if existing:
        seed(tmp_path, source, options(), warm=warm)
    with effect_guard(monkeypatch, tmp_path) as statements:
        result = invoke(tmp_path, source)
    assert result.exit_code == 0, text(result)
    assert re.search(r"Texts from cache\s*│\s*" + ("5" if warm else "0"), text(result))
    assert bool(statements) is existing
    assert "no network or cache warming" in text(result)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_cache_only_quadrants_and_missing_renderer(tmp_path, monkeypatch, warm, full):
    config(tmp_path)
    source = _book(tmp_path / "book.epub", chapter_two="<p>Hello <b>world</b> — café.</p>")
    seed(tmp_path, source, options(), warm=warm)
    missing = tmp_path / "missing.txt"
    flags = ["--cache-only", "--missing-output", str(missing)]
    if full:
        flags.append("--require-full-cache")
    with effect_guard(monkeypatch, tmp_path, presentation=True):
        result = invoke(tmp_path, source, *flags)
    assert result.exit_code == int(full and not warm), text(result)
    assert missing.exists() is not warm
    if not warm:
        content = missing.read_text()
        assert "# Missing texts: 5" in content and "[5]\nHello world — café." in content
        assert "__AYVU_" not in content and "<b>" not in content and content.endswith("\n")


def test_no_project_never_reads_even_a_corrupt_cache(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    (tmp_path / "cache.sqlite.v2.sqlite").write_bytes(b"corrupt")
    with effect_guard(monkeypatch, tmp_path) as statements:
        result = invoke(tmp_path, source)
    assert result.exit_code == 0, text(result)
    assert statements == []


@pytest.mark.parametrize("state", ["absent", "readonly", "corrupt", "busy"])
def test_cache_storage_states_have_no_effects(tmp_path, monkeypatch, state):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    _, cache = seed(tmp_path, source, options())
    if state == "absent":
        cache.unlink()
    elif state == "readonly":
        cache.chmod(0o400)
        cache.with_name(cache.name + ".lock").chmod(0o400)
    elif state == "corrupt":
        cache.write_bytes(b"invalid SQLite")
    if state == "busy":
        with active_writer(cache), effect_guard(monkeypatch, tmp_path):
            result = invoke(tmp_path, source)
    else:
        with effect_guard(monkeypatch, tmp_path):
            result = invoke(tmp_path, source)
    assert result.exit_code == int(state in {"corrupt", "busy"}), text(result)
    if state == "busy":
        assert "cache is busy" in text(result) and "cache_recovery_required" not in text(result)
    if state == "corrupt":
        assert "cache_corrupt" in text(result)


@pytest.mark.parametrize("flags,workers", [(["--workers", "2"], 2), (["--performance-profile", "high"], 4)])
def test_worker_count_is_retained_and_sequential_analysis_is_explicit(tmp_path, monkeypatch, flags, workers):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    counts = []
    original = builder.translate
    def translate(book, hooks, label, **kwargs):
        counts.append(book.options.workers)
        return original(book, hooks, label, **kwargs)
    monkeypatch.setattr(builder, "translate", translate)
    with effect_guard(monkeypatch, tmp_path):
        result = invoke(tmp_path, source, *flags)
    assert result.exit_code == 0, text(result)
    assert counts == [workers]
    assert f"{workers} workers configured; sequential local analysis" in text(result)
    assert "parallel" not in text(result)


def test_batch_keeps_automatic_markdown_and_missing_reports(tmp_path, monkeypatch):
    config(tmp_path)
    first, second = (_book(tmp_path / name) for name in ("first.epub", "second.epub"))
    with effect_guard(monkeypatch, tmp_path, presentation=True):
        result = runner.invoke(app, ["--mode", "developer", "translate", str(first), str(second),
            "--source", "en", "--target", "pt", "--dry-run", "--cache-only", "--workers", "2",
            "--cache", str(tmp_path / "cache.sqlite"), "--output-dir", str(tmp_path / "output")])
    assert result.exit_code == 0, text(result)
    reports = list((tmp_path / "books" / "Relatorios").glob("*.md"))
    missing = list((tmp_path / "books" / "Relatorios").glob("*.txt"))
    assert len(reports) == len(missing) == 2
    assert all("2 workers configured; sequential local analysis" in path.read_text() for path in reports)


def test_current_glossary_is_applied_to_raw_hits_without_cache_rewrite(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    glossary = tmp_path / "new-glossary.json"
    glossary.write_text(json.dumps({"reader": "READER-NEW"}))
    # The existing project authorizes this intention; its raw entries predate the
    # non-native glossary, which deliberately does not enter raw semantic keys.
    seed(tmp_path, source, options(), glossary=glossary)
    reports = []
    original = builder.translate
    def translate(*args, **kwargs):
        report = original(*args, **kwargs)
        reports.append(report)
        return report
    monkeypatch.setattr(builder, "translate", translate)
    with effect_guard(monkeypatch, tmp_path):
        result = invoke(tmp_path, source, "--glossary", str(glossary))
    assert result.exit_code == 0, text(result)
    assert reports[0].texts_from_cache == 5
    assert reports[0].glossary_usage.total_applied > 0


@pytest.mark.parametrize("state,code", [("unreadable", "invalid_manifest"),
    ("ambiguous", "ambiguous_project"), ("changed", "source_changed")])
def test_catalog_refusals_are_explicit_and_leave_no_effects(tmp_path, monkeypatch, state, code):
    from uuid import uuid4
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, _ = seed(tmp_path, source, options())
    root = tmp_path / "processing" / "projects"
    manifest = root / project.project_id / "project.json"
    if state == "unreadable":
        manifest.write_text("invalid json")
    elif state == "ambiguous":
        data = json.loads(manifest.read_text())
        data["project_id"] = str(uuid4())
        duplicate = root / data["project_id"]
        duplicate.mkdir()
        (duplicate / "project.json").write_text(json.dumps(data))
    else:
        _book(source, chapter_two="<p>Changed source.</p>")
    with effect_guard(monkeypatch, tmp_path):
        result = invoke(tmp_path, source, "--cache-only")
    assert result.exit_code == 1 and code in text(result), text(result)


@pytest.mark.parametrize("flags", [["--url", "http://user:secret@localhost:5000"], ["--retries", "-1"],
    ["--url", "https://localhost:5000"], ["--retry-backoff", "-1"], ["--glossary", "missing.json"]])
def test_local_configuration_refusals_never_prepare_transport(tmp_path, monkeypatch, flags):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    with effect_guard(monkeypatch, tmp_path):
        result = invoke(tmp_path, source, *flags)
    assert result.exit_code == 1, text(result)
    assert "secret" not in text(result)


def test_automatic_missing_text_output_uses_the_current_renderer(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    with effect_guard(monkeypatch, tmp_path, presentation=True):
        result = invoke(tmp_path, source, "--cache-only")
    assert result.exit_code == 0, text(result)
    missing = list((tmp_path / "books" / "Relatorios").glob("book-pt-missing*.txt"))
    assert len(missing) == 1 and "# Missing texts: 5" in missing[0].read_text()


@pytest.mark.parametrize("state,code", [("overflow", "cache_candidate_limit_exceeded"),
    ("invalid_tokens", "cache_entry_corrupt"), ("ambiguous", None)])
def test_canonical_failures_and_ambiguity_are_not_silently_ignored(tmp_path, monkeypatch, state, code):
    from ayvu.knowledge.cache.repository import SQLiteExactCache
    from ayvu.providers.libretranslate.adapter import LIBRETRANSLATE_PRODUCER
    from tests.support.transient_translation_support import exact_entry
    from tests.support.translation_cache_v2_support import make_plan
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, cache = seed(tmp_path, source, options(), warm=False)
    writer = SQLiteExactCache(cache)
    producer = replace(LIBRETRANSLATE_PRODUCER, model="declared:local-model-2026")
    for index in range(257 if state == "overflow" else 2 if state == "ambiguous" else 1):
        plan = make_plan(producer=producer, capabilities="capfp:v1:" + f"{index:064x}")
        writer.put_if_absent(exact_entry(project, plan, "T", "__AYVU_TAG_99__" if state == "invalid_tokens" else "CACHED-TITLE"))
    with effect_guard(monkeypatch, tmp_path, presentation=True):
        result = invoke(tmp_path, source, "--cache-only")
    assert result.exit_code == int(code is not None), text(result)
    if code:
        assert code in text(result) and not list((tmp_path / "books").rglob("*.txt"))
    else:
        missing = next((tmp_path / "books").rglob("*.txt"))
        assert "# Missing texts: 5" in missing.read_text()


def test_warm_cache_does_not_replay_or_publish_existing_raw_artifacts(tmp_path, monkeypatch):
    config(tmp_path)
    source = _book(tmp_path / "book.epub")
    project, _ = seed(tmp_path, source, options())
    directory = tmp_path / "processing" / "projects" / project.project_id / "artifacts"
    directory.mkdir()
    (directory / "raw-results.json").write_text('{"canary":"existing raw evidence"}')
    with effect_guard(monkeypatch, tmp_path):
        result = invoke(tmp_path, source, "--cache-only", "--require-full-cache")
    assert result.exit_code == 0, text(result)
