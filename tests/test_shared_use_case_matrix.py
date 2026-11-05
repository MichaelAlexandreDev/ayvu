from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from typer.main import get_command

from ayvu.cli import app


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "architecture" / "shared-use-case-matrix.md"
SNAPSHOT_PATTERN = re.compile(
    r"<!-- AYVU_CLI_SURFACE_SNAPSHOT_START -->\s*"
    r"```json\s*(?P<payload>.*?)\s*```\s*"
    r"<!-- AYVU_CLI_SURFACE_SNAPSHOT_END -->",
    re.DOTALL,
)
LOCAL_LINK_PATTERN = re.compile(r"\[[^]]+\]\((?P<target>[^)]+)\)")
MAPPING_PATTERN = re.compile(r"^AYVU-(?:UC|UI)-\d{3}$")
DECLARED_MAPPING_PATTERN = re.compile(
    r"^\|\s*`(?P<mapping>AYVU-(?:UC|UI)-\d{3})`\s*\|",
    re.MULTILINE,
)
OPTION_KINDS = {
    "adapter-configuration",
    "application-policy",
    "artifact-destination",
    "compatibility-routing",
    "domain-input",
    "presentation-confirmation",
    "presentation-only",
}
PUBLIC_LINK_TARGETS = {
    ROOT / "README.md",
    ROOT / "docs" / "product-scope.md",
    ROOT / "docs" / "translation-workflow-migration-baseline.md",
    ROOT / "src" / "ayvu" / "cache.py",
    ROOT / "src" / "ayvu" / "cache_admin.py",
    ROOT / "src" / "ayvu" / "cache_admin_store.py",
    ROOT / "src" / "ayvu" / "cli.py",
    ROOT / "src" / "ayvu" / "cli_progress.py",
    ROOT / "src" / "ayvu" / "config.py",
    ROOT / "src" / "ayvu" / "content_composition.py",
    ROOT / "src" / "ayvu" / "content_use_cases.py",
    ROOT / "src" / "ayvu" / "epub_io.py",
    ROOT / "src" / "ayvu" / "epub_format.py",
    ROOT / "src" / "ayvu" / "format_service.py",
    ROOT / "src" / "ayvu" / "glossary.py",
    ROOT / "src" / "ayvu" / "html_structure.py",
    ROOT / "src" / "ayvu" / "html_translate.py",
    ROOT / "src" / "ayvu" / "library_service.py",
    ROOT / "src" / "ayvu" / "library_adapters.py",
    ROOT / "src" / "ayvu" / "markdown_writer.py",
    ROOT / "src" / "ayvu" / "preflight.py",
    ROOT / "src" / "ayvu" / "resume.py",
    ROOT / "src" / "ayvu" / "review_export.py",
    ROOT / "src" / "ayvu" / "review_exchange.py",
    ROOT / "src" / "ayvu" / "review_import.py",
    ROOT / "src" / "ayvu" / "review_application.py",
    ROOT / "src" / "ayvu" / "review_composition.py",
    ROOT / "src" / "ayvu" / "translation_memory.py",
    ROOT / "src" / "ayvu" / "translate_project.py",
    ROOT / "src" / "ayvu" / "translator.py",
    ROOT / "src" / "ayvu" / "validation.py",
    ROOT / "tests" / "test_cache.py",
    ROOT / "tests" / "test_cache_admin.py",
    ROOT / "tests" / "test_cache_admin_integration.py",
    ROOT / "tests" / "test_cli.py",
    ROOT / "tests" / "test_cli_progress.py",
    ROOT / "tests" / "test_config.py",
    ROOT / "tests" / "test_content_use_cases.py",
    ROOT / "tests" / "test_epub_io.py",
    ROOT / "tests" / "test_glossary.py",
    ROOT / "tests" / "test_html_translate.py",
    ROOT / "tests" / "test_library.py",
    ROOT / "tests" / "test_resume.py",
    ROOT / "tests" / "test_review_import.py",
    ROOT / "tests" / "test_review_exchange.py",
    ROOT / "tests" / "test_review_application.py",
    ROOT / "tests" / "test_cli_translate_project.py",
    ROOT / "tests" / "test_shared_use_case_matrix.py",
    ROOT / "tests" / "test_translator.py",
    ROOT / "tests" / "test_workflow_characterization.py",
}


def _load_matrix() -> tuple[str, dict[str, Any]]:
    text = MATRIX_PATH.read_text(encoding="utf-8")
    match = SNAPSHOT_PATTERN.search(text)
    assert match is not None, "CLI surface snapshot markers are missing"
    return text, json.loads(match.group("payload"))


def _long_options(command: Any) -> list[str]:
    result: list[str] = []
    for parameter in command.get_params(typer.Context(command)):
        opts = getattr(parameter, "opts", None)
        secondary_opts = getattr(parameter, "secondary_opts", None)
        if opts is None or secondary_opts is None:
            continue
        declared_options = (*opts, *secondary_opts)
        option_tokens = [
            option for option in declared_options if option.startswith("-")
        ]
        if not option_tokens:
            continue
        long_options = [
            option
            for option in option_tokens
            if option.startswith("--")
        ]
        assert long_options, (
            f"public option {command.name}.{parameter.name} has no canonical long form"
        )
        result.extend(long_options)
    return sorted(result)


def _subcommands(command: Any) -> dict[str, Any] | None:
    commands = getattr(command, "commands", None)
    if commands is None:
        return None
    assert isinstance(commands, dict), "command children must be stored by name"
    return commands


def _discover_command(command: Any, universal_options: set[str]) -> dict[str, Any]:
    subcommands: dict[str, Any] = {}
    command_children = _subcommands(command)
    if command_children is not None:
        subcommands = {
            name: _discover_command(child, universal_options)
            for name, child in sorted(command_children.items())
        }
    return {
        "options": sorted(set(_long_options(command)) - universal_options),
        "subcommands": subcommands,
    }


def _discover_cli_surface(universal_options: set[str]) -> dict[str, Any]:
    root = get_command(app)
    root_commands = _subcommands(root)
    assert root_commands is not None, "root Typer command must expose subcommands"
    return {
        "root_options": sorted(set(_long_options(root)) - universal_options),
        "commands": {
            name: _discover_command(command, universal_options)
            for name, command in sorted(root_commands.items())
        },
    }


def _snapshot_surface(snapshot: dict[str, Any]) -> dict[str, Any]:
    def normalize(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "options": sorted(record["options"]),
            "subcommands": {
                name: normalize(child)
                for name, child in sorted(record.get("subcommands", {}).items())
            },
        }

    return {
        "root_options": sorted(snapshot["root_options"]),
        "commands": {
            name: normalize(record)
            for name, record in sorted(snapshot["commands"].items())
        },
    }


def _iter_command_records(snapshot: dict[str, Any]):
    def walk(path: str, record: dict[str, Any]):
        yield path, record
        for name, child in sorted(record.get("subcommands", {}).items()):
            yield from walk(f"{path} {name}", child)

    for name, record in sorted(snapshot["commands"].items()):
        yield from walk(name, record)


def _section(text: str, heading: str, next_heading: str) -> str:
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def _declared_mappings(text: str) -> list[str]:
    definition_tables = "\n".join(
        (
            _section(text, "## Use cases", "## Proposed target ownership"),
            _section(text, "## Interface-only behavior", "## Option boundary"),
        )
    )
    return DECLARED_MAPPING_PATTERN.findall(definition_tables)


def _guided_mappings(text: str) -> list[str]:
    guided_table = _section(
        text,
        "## Guided workflow coverage",
        "## Extraction constraints and known gaps",
    )
    mappings: list[str] = []
    for line in guided_table.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Guided choice" or set(cells[0]) <= {"-", " "}:
            continue
        assert len(cells) == 2, f"invalid guided workflow row: {line}"
        match = re.fullmatch(r"`(?P<mapping>AYVU-(?:UC|UI)-\d{3})`", cells[1])
        assert match is not None, f"guided workflow has no valid mapping: {line}"
        mappings.append(match.group("mapping"))
    assert mappings, "guided workflow table has no mapped rows"
    return mappings


def test_committed_matrix_matches_effective_typer_surface() -> None:
    _, snapshot = _load_matrix()
    universal_options = set(snapshot["universal_options"])

    assert _snapshot_surface(snapshot) == _discover_cli_surface(universal_options)


def test_universal_options_exist_at_every_command_level() -> None:
    _, snapshot = _load_matrix()
    expected = set(snapshot["universal_options"])
    root = get_command(app)
    pending = [root]

    while pending:
        command = pending.pop()
        assert expected <= set(_long_options(command))
        command_children = _subcommands(command)
        if command_children is not None:
            pending.extend(command_children.values())


def test_every_command_and_option_has_a_stable_mapping() -> None:
    text, snapshot = _load_matrix()
    declared_mappings = _declared_mappings(text)
    declared_mapping_set = set(declared_mappings)

    assert len(declared_mappings) == len(declared_mapping_set), (
        "use-case and interface IDs must have exactly one definition"
    )
    assert set(_guided_mappings(text)) <= declared_mapping_set

    for option_group in ("universal_options", "root_options"):
        for option, contract in snapshot[option_group].items():
            assert contract["kind"] in OPTION_KINDS, (
                f"{option} has an invalid classification"
            )
            mappings = contract.get("mappings", [])
            assert mappings, f"{option} has no use-case or interface mapping"
            assert all(MAPPING_PATTERN.fullmatch(mapping) for mapping in mappings)
            assert set(mappings) <= declared_mapping_set

    for command_path, record in _iter_command_records(snapshot):
        mappings = record.get("use_cases", [])
        assert mappings, f"{command_path} has no use-case or interface mapping"
        assert all(MAPPING_PATTERN.fullmatch(mapping) for mapping in mappings)
        assert set(record["options"].values()) <= OPTION_KINDS
        assert set(mappings) <= declared_mapping_set
        for mapping, exclusions in record.get("option_exclusions", {}).items():
            assert mapping in mappings
            assert set(exclusions) <= set(record["options"])


def test_matrix_local_links_resolve_inside_repository() -> None:
    text, _ = _load_matrix()

    for match in LOCAL_LINK_PATTERN.finditer(text):
        raw_target = match.group("target")
        if raw_target.startswith(("https://", "http://")):
            continue
        assert "://" not in raw_target, f"unsupported link scheme: {raw_target}"
        assert not raw_target.startswith("#"), (
            f"internal link fragments are not validated: {raw_target}"
        )
        assert "#" not in raw_target, f"local link fragments are not validated: {raw_target}"
        resolved = (MATRIX_PATH.parent / raw_target).resolve()
        assert resolved.is_relative_to(ROOT), f"link escapes repository: {raw_target}"
        assert resolved in PUBLIC_LINK_TARGETS, f"local link is not allowlisted: {raw_target}"
        assert resolved.exists(), f"broken local link: {raw_target}"
