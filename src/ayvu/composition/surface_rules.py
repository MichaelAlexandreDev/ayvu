"""Pure surface projection, independent of presenters and resource factories.

Command paths double as stable presentation binding keys. Menu pairs retain
existing keys, including gaps. Writers describe operations, not input formats.
"""
from __future__ import annotations

from dataclasses import dataclass

from .feature_catalog import Feature, validate_features, validate_presentation


@dataclass(frozen=True)
class CommandRule:
    path: tuple[str, ...]
    requires: Feature


@dataclass(frozen=True)
class MenuRule:
    key: str
    action: str
    requires: Feature


@dataclass(frozen=True)
class BindingRule:
    id: str
    kind: str
    requires: frozenset[Feature] = frozenset()


COMMAND_RULES = (
    CommandRule(("inspect",), Feature.INSPECT_EPUB),
    CommandRule(("test-translator",), Feature.TEST_TRANSLATOR),
    CommandRule(("languages",), Feature.LIST_LANGUAGES),
    CommandRule(("cache", "inspect"), Feature.CACHE_ADMINISTRATION),
    CommandRule(("cache", "clean"), Feature.CACHE_ADMINISTRATION),
    CommandRule(("cache", "export"), Feature.CACHE_ADMINISTRATION),
    CommandRule(("cache", "import"), Feature.CACHE_ADMINISTRATION),
    CommandRule(("translate",), Feature.TRANSLATE_EPUB),
    CommandRule(("resume",), Feature.RESUME_TRANSLATION),
    CommandRule(("extract",), Feature.EXTRACT_MARKDOWN),
    CommandRule(("apply-review",), Feature.REVIEW_APPLY),
    CommandRule(("consent", "revoke"), Feature.TRANSLATE_EPUB),
)
MENU_RULES = (
    MenuRule("1", "translate", Feature.TRANSLATE_EPUB),
    MenuRule("2", "preview", Feature.PREVIEW),
    MenuRule("3", "library", Feature.LIBRARY),
    MenuRule("4", "settings", Feature.SETTINGS),
    MenuRule("5", "glossaries", Feature.GLOSSARY_CATALOG),
    MenuRule("6", "help", Feature.GUIDED_MENU),
    MenuRule("0", "exit", Feature.GUIDED_MENU),
)
# Presentation binding names, audited separately from the feature rules above.
MENU_BINDINGS = (
    ("translate", "interfaces.cli.guided.translation._run_guided_translation"),
    ("preview", "interfaces.cli.guided.translation._run_guided_preview"),
    ("library", "interfaces.cli.guided.library._run_guided_library"),
    ("settings", "interfaces.cli.guided.preferences._run_guided_settings"),
    ("glossaries", "interfaces.cli.guided.glossaries._run_guided_glossaries"),
    ("help", "interfaces.cli.guided.menu._handle_guided_main_choice"),
    ("exit", "interfaces.cli.guided.menu._handle_guided_main_choice"),
)
BINDING_RULES = (
    BindingRule("libretranslate", "provider"),
    BindingRule("epub", "input_format"),
    BindingRule("markdown", "extraction_writer", frozenset((Feature.EXTRACT_MARKDOWN,))),
    BindingRule("markdown", "report_writer", frozenset((Feature.FULL_REPORT,))),
)


@dataclass(frozen=True)
class SurfaceSelection:
    commands: tuple[tuple[str, ...], ...]
    menu_actions: tuple[tuple[str, str], ...]
    providers: tuple[str, ...]
    input_formats: tuple[str, ...]
    extraction_writers: tuple[str, ...]
    report_writers: tuple[str, ...]
    assistance_roles: tuple[str, ...]
    preview: bool
    execution_preset: str | None
    report_style: str


def validate_rules(*, commands=COMMAND_RULES, menu=MENU_RULES, bindings=BINDING_RULES):
    """Reject ambiguous registrations and rules with no catalog owner."""
    known = frozenset(Feature)
    paths = tuple(rule.path for rule in commands)
    if len(frozenset(paths)) != len(paths):
        raise ValueError("Duplicate command path")
    if any(not path or any(not part for part in path) for path in paths):
        raise ValueError("Empty command path")
    if any(path[:length] in paths for path in paths for length in range(1, len(path))):
        raise ValueError("Command cannot also be a group")
    for rules, attribute in ((menu, "key"), (menu, "action")):
        if len(frozenset(getattr(rule, attribute) for rule in rules)) != len(rules):
            raise ValueError("Duplicate menu action or key")
    if any(rule.requires not in known for rule in (*commands, *menu)):
        raise ValueError("Orphan surface rule")
    actions = frozenset(action for action, _ in MENU_BINDINGS)
    if any(not rule.key or rule.action not in actions for rule in menu):
        raise ValueError("Orphan menu action")
    kinds = frozenset(("provider", "input_format", "extraction_writer", "report_writer"))
    if any(rule.kind not in kinds or rule.requires - known for rule in bindings):
        raise ValueError("Unknown binding kind or dependency")
    if len(frozenset((rule.kind, rule.id) for rule in bindings)) != len(bindings):
        raise ValueError("Duplicate binding")


def project_surface(features: frozenset[str], *, execution_preset=None, report_style=None) -> SurfaceSelection:
    """Project effective capabilities only, including synthetic selections for tests.

    Presets and report style are declarations here. Parameter/preset enforcement
    belongs to the later interface integration, not this pure plan.
    """
    validate_features(features)
    style = report_style if report_style is not None else ("full" if Feature.FULL_REPORT in features else "summary")
    validate_presentation(features, execution_preset, style)
    validate_rules(commands=COMMAND_RULES, menu=MENU_RULES, bindings=BINDING_RULES)

    def bindings(kind):
        return tuple(rule.id for rule in BINDING_RULES if rule.kind == kind and rule.requires <= features)

    return SurfaceSelection(
        tuple(rule.path for rule in COMMAND_RULES if rule.requires in features),
        tuple((rule.key, rule.action) for rule in MENU_RULES
              if Feature.GUIDED_MENU in features and rule.requires in features),
        bindings("provider"), bindings("input_format"), bindings("extraction_writer"),
        bindings("report_writer"), (), Feature.PREVIEW in features, execution_preset, style,
    )
