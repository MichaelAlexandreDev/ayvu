"""Read the mounted Click tree against literal product expectations, not its plan."""
from __future__ import annotations

from dataclasses import replace

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from ayvu.composition.cli import installed_cli_surface, project_cli_surface
from ayvu.composition.editions import get_edition
from ayvu.interfaces.cli import app as cli
from tests.architecture import cli_surface_support
from tests.composition.test_cli_composition import deny_resources


TREES = (
    ("lite", {"translate", "resume", "test-translator", "consent", "consent revoke"}),
    ("free", {"translate", "resume", "test-translator", "inspect", "extract", "apply-review", "consent", "consent revoke"}),
    ("ai", {"translate", "resume", "test-translator", "inspect", "extract", "apply-review", "consent", "consent revoke"}),
    ("cli_free", {"inspect", "test-translator", "languages", "cache", "cache inspect", "cache clean",
                  "cache export", "cache import", "translate", "resume", "extract", "apply-review", "consent",
                  "consent revoke"}),
    ("cli_pro", {"inspect", "test-translator", "languages", "cache", "cache inspect", "cache clean",
                 "cache export", "cache import", "translate", "resume", "extract", "apply-review", "consent",
                 "consent revoke"}),
)


def projected(edition_id):
    edition = get_edition(edition_id)
    return project_cli_surface(edition.features, execution_preset=edition.execution_preset,
                               report_style=edition.report_style)


def tree_commands(command, prefix=()):
    for name, child in getattr(command, "commands", {}).items():
        path = (*prefix, name)
        yield path, child
        yield from tree_commands(child, path)


@pytest.mark.parametrize("edition_id,expected", TREES, ids=[row[0] for row in TREES])
def test_mounted_tree_matches_literal_product_table(edition_id, expected, monkeypatch):
    baseline = cli_surface_support.normalized_cli_contract()
    application = cli.create_app(projected(edition_id))
    command = get_command(application)
    assert {" ".join(path) for path, _ in tree_commands(command)} == expected
    assert application.registered_callback.callback is cli.main
    monkeypatch.setattr(cli_surface_support, "app", application)
    actual = cli_surface_support.normalized_cli_contract()

    def compare(record, original):
        assert record["parameters"] == original["parameters"]
        assert record["help"] == original["help"]
        for name, child in record["commands"].items():
            compare(child, original["commands"][name])

    compare(actual, baseline)


@pytest.mark.parametrize("edition_id,expected", TREES, ids=[row[0] for row in TREES])
def test_help_and_shell_completion_offer_only_mounted_commands(edition_id, expected, monkeypatch):
    monkeypatch.setenv("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
    calls = deny_resources(monkeypatch)
    application = cli.create_app(projected(edition_id))
    command = get_command(application)
    runner = CliRunner()
    help_result = runner.invoke(application, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    for name in {path.split()[0] for _, paths in TREES for path in paths}:
        assert (name in help_result.output) == (name in expected)
    for path, _ in tree_commands(command):
        result = runner.invoke(application, [*path, "--help"])
        assert result.exit_code == 0, result.output
    completion = runner.invoke(application, [], prog_name="ayvu", env={
        "_AYVU_COMPLETE": "complete_bash", "COMP_WORDS": "ayvu ", "COMP_CWORD": "1",
    })
    assert completion.exit_code == 0, completion.output
    assert set(completion.output.splitlines()) == {path.split()[0] for path in expected}
    if "cache" in expected:
        children = runner.invoke(application, [], prog_name="ayvu", env={
            "_AYVU_COMPLETE": "complete_bash", "COMP_WORDS": "ayvu cache ", "COMP_CWORD": "2",
        })
        assert children.exit_code == 0, children.output
        assert set(children.output.splitlines()) == {"inspect", "clean", "export", "import"}
    options = runner.invoke(application, [], prog_name="ayvu", env={
        "_AYVU_COMPLETE": "complete_bash", "COMP_WORDS": "ayvu translate --wor", "COMP_CWORD": "2",
    })
    assert options.exit_code == 0, options.output
    assert options.output.strip() == "--workers"
    shown = runner.invoke(application, ["--show-completion", "bash"], prog_name="ayvu", env={
        "_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1",
    })
    assert shown.exit_code == 0, shown.output
    assert "complete_bash" in shown.output
    assert calls == []


@pytest.mark.parametrize("name", ["inspect", "extract", "apply-review", "languages", "cache"])
@pytest.mark.parametrize("suffix", [[], ["--help"]], ids=["execute", "help"])
def test_absent_command_is_unknown_before_any_service(name, suffix, monkeypatch):
    calls = deny_resources(monkeypatch)
    application = cli.create_app(projected("lite"))
    result = CliRunner().invoke(application, [name, *suffix])
    assert result.exit_code == 2
    assert f"No such command '{name}'" in result.output
    assert calls == []


def test_partial_cache_group_has_no_unselected_help_dispatch_or_completions(monkeypatch):
    calls = deny_resources(monkeypatch)
    application = cli.create_app(replace(installed_cli_surface(), commands=(("cache", "inspect"),)))
    runner = CliRunner()
    result = runner.invoke(application, ["cache", "--help"])
    assert result.exit_code == 0, result.output
    # Group prose is retained; the effective command list and completion must be exact.
    command = get_command(application)
    assert set(command.commands["cache"].commands) == {"inspect"}
    result = runner.invoke(application, ["cache", "clean"])
    assert result.exit_code == 2 and "No such command 'clean'" in result.output
    result = runner.invoke(application, [], prog_name="ayvu", env={
        "_AYVU_COMPLETE": "complete_bash", "COMP_WORDS": "ayvu cache ", "COMP_CWORD": "2",
    })
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "inspect"
    assert calls == []


def test_factories_keep_registration_order_identity_and_independent_groups():
    from ayvu.interfaces.cli.commands import cache, consent, content, resume, review, translate, translator
    surface = installed_cli_surface()
    first = cli.create_app(surface)
    reduced = cli.create_app(projected("lite"))
    second = cli.create_app(replace(surface, commands=tuple(reversed(surface.commands))))
    assert first is not second and first is not cli.app
    assert first.registered_commands is not second.registered_commands
    assert first.registered_groups[0].typer_instance is not second.registered_groups[0].typer_instance
    assert [group.name for group in reduced.registered_groups] == ["consent"]
    callbacks = [content.inspect, translator.test_translator, translator.languages, translate.translate,
                 resume.resume, content.extract, review.apply_review]
    for application in (first, second, cli.app):
        assert [info.callback for info in application.registered_commands] == callbacks
        group = application.registered_groups[0].typer_instance
        assert [info.callback for info in group.registered_commands] == [
            cache.cache_inspect, cache.cache_clean, cache.cache_export, cache.cache_import,
        ]
        assert [info.callback for info in application.registered_groups[1].typer_instance.registered_commands] == [
            consent.revoke]
    first.registered_groups[0].typer_instance.registered_commands.clear()
    assert len(second.registered_groups[0].typer_instance.registered_commands) == 4


def test_completion_installation_is_confined_to_temporary_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
    result = CliRunner().invoke(cli.create_app(projected("lite")), ["--install-completion", "bash"],
        prog_name="ayvu", env={"_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1"})
    assert result.exit_code == 0, result.output
    assert "complete_bash" in (tmp_path / ".bash_completions/ayvu.sh").read_text()
    assert str(tmp_path / ".bash_completions/ayvu.sh") in (tmp_path / ".bashrc").read_text()
