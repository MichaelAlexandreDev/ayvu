"""Reviewed dependency inventory for offline transient execution (REF-18 G3)."""
TRANSIENT_EDGES = {
    ("ayvu.composition.cli", "ayvu.composition.builders.translate_transient"),
    ("ayvu.interfaces.cli.run_support", "ayvu.interfaces.cli.progress"),
    *(("ayvu.composition.builders.translate_transient", f"ayvu.{name}") for name in (
        "formats.local", "formats.epub.reader", "formats.epub.html_structure", "knowledge.cache.scope", "knowledge.cache.identity_values",
        "knowledge.cache.read_only", "knowledge.cache.schema", "knowledge.glossary.rules",
        "projects.model", "projects.service", "projects.store", "provenance.model", "providers.libretranslate.adapter",
        "translation.engine", "translation.local_cache_plan", "translation.errors",
        "translation.transient", "translation.transient_units", "translation.document_contracts", "translation.report",
    )),
    *(("ayvu.translation.transient", f"ayvu.{name}") for name in (
        "formats.book_facts", "formats.contracts", "formats.service", "translation.engine", "translation.errors",
        "translation.transient_units")),
    *(("ayvu.translation.transient_units", f"ayvu.{name}") for name in (
        "content.model", "content.protection", "knowledge.cache.identity_values",
        "knowledge.cache.entries", "knowledge.cache.cache_only", "translation.document_contracts", "translation.engine",
        "translation.local_cache_plan", "translation.pipeline")),
}
