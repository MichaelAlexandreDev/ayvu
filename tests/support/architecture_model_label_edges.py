"""Admission and guided setup share composition's model-label rule (REF-18 step 4)."""
MODEL_LABEL_EDGES = {
    ("ayvu.composition.builders.rich_provider", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.run_support", "ayvu.interfaces.cli.settings"),
    ("ayvu.interfaces.cli.translate_options", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.composition.cli"),
    ("ayvu.interfaces.cli.guided.preferences", "ayvu.translation.errors"),
    ("ayvu.interfaces.cli.guided.translation", "ayvu.interfaces.cli.guided.preferences"),
    # REF-18 step 5: the old-data notice counts the v1 cache read-only.
    ("ayvu.composition.cli", "ayvu.knowledge.cache.legacy_census"),
    ("ayvu.interfaces.cli.batch", "ayvu.interfaces.cli.run_messages"),
}
