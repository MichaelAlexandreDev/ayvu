"""Reviewed dependencies of compatibility checkpoints and their CLI composition."""
CHECKPOINT_EDGES = {
    ("ayvu.composition.cli", "ayvu.composition.builders.checkpoint"),
    ("ayvu.composition.builders.checkpoint", "ayvu.recovery.checkpoint"),
    ("ayvu.recovery.checkpoint", "ayvu.translation.errors"),
    ("ayvu.recovery.checkpoint_scan", "ayvu.recovery.checkpoint"),
    ("ayvu.recovery.store", "ayvu.recovery.checkpoint_scan"),
    ("ayvu.recovery.store", "ayvu.recovery.checkpoint"),
}
