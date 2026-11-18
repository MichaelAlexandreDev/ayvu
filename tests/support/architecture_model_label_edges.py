"""Admission and guided setup share composition's model-label rule (REF-18 step 4)."""
MODEL_LABEL_EDGES = {
    ("ayvu.composition.builders.providers", "ayvu.preferences.model"),
    ("ayvu.composition.builders.providers", "ayvu.translation.errors"),
    ("ayvu.composition.builders.providers", "ayvu.composition.builders.selection"),
    ("ayvu.interfaces.cli.commands.resume", "ayvu.translation.errors"),
    *(("ayvu.composition.builders.selection", f"ayvu.{name}") for name in (
        "preferences.model", "projects.model", "provenance.model", "kernel.errors", "projects.schema", "projects.store", "provenance.store", "translation.errors",
        "composition.builders.rich_provider", "composition.builders.ollama")),
    ("ayvu.composition.cli", "ayvu.composition.builders.selection"),
    ("ayvu.interfaces.cli.settings", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.app", "ayvu.interfaces.cli.translate_options"),
    ("ayvu.interfaces.cli.guided.translation", "ayvu.interfaces.cli.settings"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.providers.errors"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.interfaces.cli.options"),
    ("ayvu.composition.builders.rich_provider", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.run_support", "ayvu.interfaces.cli.settings"),
    ("ayvu.interfaces.cli.translate_options", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.composition.cli"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.guided.translation", "ayvu.interfaces.cli.guided.preferences"),
}
