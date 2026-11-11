from ayvu.v1.cache import TranslationCache
from ayvu.interfaces.cli.app import app
from tests.support.cli_support import _seed_cache_entry, runner


def test_cache_inspect_command_lists_entries_by_language_pair(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    _seed_cache_entry(cache_path, "Hello", "Olá", created_at="2024-01-01 10:00:00")
    _seed_cache_entry(cache_path, "World", "Mundo", created_at="2024-01-02 10:00:00")
    _seed_cache_entry(cache_path, "Hola", "Olá", source="es", target="pt", created_at="2024-01-03 10:00:00")

    result = runner.invoke(app, ["cache", "inspect", "--cache", str(cache_path)])

    assert result.exit_code == 0
    assert "Cache summary" in result.output
    assert "en" in result.output
    assert "es" in result.output
    assert "pt" in result.output
    assert "2024-01-01 10:00:00" in result.output
    assert "3 cache entries" in result.output


def test_cache_clean_requires_filter_or_all(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    _seed_cache_entry(cache_path, "Hello", "Olá")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--yes"])

    assert result.exit_code == 1
    assert "escopo explícito" in result.output
    assert "Traceback" not in result.output


def test_cache_clean_dry_run_does_not_delete_entries(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    key = _seed_cache_entry(cache_path, "Hello", "Olá")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--source", "en", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "1 cache entry would be removed" in result.output
    with TranslationCache(cache_path) as cache:
        assert cache.get(key) == "Olá"


def test_cache_clean_deletes_confirmed_entries(tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    removed_key = _seed_cache_entry(cache_path, "Hello", "Olá", target="pt")
    kept_key = _seed_cache_entry(cache_path, "Hello", "Hola", target="es")

    result = runner.invoke(app, ["cache", "clean", "--cache", str(cache_path), "--target", "pt", "--yes"])

    assert result.exit_code == 0
    assert "Deleted 1 cache entry" in result.output
    with TranslationCache(cache_path) as cache:
        assert cache.get(removed_key) is None
        assert cache.get(kept_key) == "Hola"


def test_cache_export_and_import_commands(tmp_path):
    source_cache = tmp_path / "source.sqlite"
    target_cache = tmp_path / "target.sqlite"
    export_path = tmp_path / "cache-export.json"
    key = _seed_cache_entry(source_cache, "Hello", "Olá")

    export_result = runner.invoke(
        app,
        ["cache", "export", str(export_path), "--cache", str(source_cache), "--source", "en"],
    )
    import_result = runner.invoke(
        app,
        ["cache", "import", str(export_path), "--cache", str(target_cache)],
    )

    assert export_result.exit_code == 0
    assert "Cache export saved to" in export_result.output
    assert "Entries exported: 1" in export_result.output
    assert import_result.exit_code == 0
    assert "Cache import" in import_result.output
    assert "Imported" in import_result.output
    with TranslationCache(target_cache) as cache:
        assert cache.get(key) == "Olá"


def test_cache_command_reports_invalid_before_without_traceback(tmp_path):
    cache_path = tmp_path / "translations.sqlite"

    result = runner.invoke(app, ["cache", "inspect", "--cache", str(cache_path), "--before", "not-a-date"])

    assert result.exit_code == 1
    assert "Data de cache inválida." in result.output
    assert "Traceback" not in result.output
