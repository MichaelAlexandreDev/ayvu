import pytest

from ayvu.knowledge.glossary.rules import (
    GLOSSARY_RULE_FORBIDDEN,
    GLOSSARY_RULE_PRESERVE,
    GLOSSARY_RULE_TRANSLATE,
    Glossary,
    GlossaryEntry,
    GlossaryError,
    GlossaryUsage,
    apply_glossary,
    apply_glossary_with_usage,
    glossary_to_dict,
    load_glossary,
    save_glossary,
)


def test_apply_glossary_replaces_terms():
    glossary = Glossary({"Game Loop": "loop de jogo", "Object Pool": "pool de objetos"})
    assert apply_glossary("A Game Loop can use an Object Pool.", glossary) == (
        "A loop de jogo can use an pool de objetos."
    )


def test_apply_glossary_preserves_all_caps():
    assert apply_glossary("OBSERVER", {"Observer": "Observer"}) == "OBSERVER"


def test_apply_glossary_prefers_longer_terms():
    glossary = Glossary({"Pattern": "padrão", "Design Pattern": "padrão de projeto"})
    assert apply_glossary("Design Pattern", glossary) == "padrão de projeto"


def test_load_glossary_returns_glossary_object(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Observer": "Observer"}', encoding="utf-8")

    glossary = load_glossary(glossary_path)

    assert isinstance(glossary, Glossary)
    assert glossary.apply("OBSERVER") == "OBSERVER"


def test_load_glossary_supports_advanced_rules(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        """
        {
          "Game Loop": {"rule": "translate", "translation": "loop de jogo"},
          "Observer": {"rule": "preserve"},
          "Foo": {"rule": "forbidden"}
        }
        """,
        encoding="utf-8",
    )

    glossary = load_glossary(glossary_path)

    assert glossary.entries == (
        GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo"),
        GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE),
        GlossaryEntry("Foo", GLOSSARY_RULE_FORBIDDEN),
    )
    assert glossary.apply("The Game Loop keeps observer.") == "The loop de jogo keeps Observer."
    assert glossary.forbidden_terms_in("Avoid foo in the output.") == ["Foo"]


def test_load_glossary_supports_required_terms(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        """
        {
          "Game Loop": {
            "rule": "translate",
            "translation": "loop de jogo",
            "required": true
          },
          "Observer": {
            "rule": "preserve",
            "required": true
          }
        }
        """,
        encoding="utf-8",
    )

    glossary = load_glossary(glossary_path)

    assert glossary.entries == (
        GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo", required=True),
        GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE, required=True),
    )


def test_save_glossary_writes_valid_advanced_json(tmp_path):
    glossary_path = tmp_path / "glossaries" / "technical.json"
    glossary = Glossary(
        [
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo"),
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE),
            GlossaryEntry("AntiPattern", GLOSSARY_RULE_FORBIDDEN),
        ]
    )

    saved_path = save_glossary(glossary_path, glossary)

    assert saved_path == glossary_path
    assert glossary_to_dict(glossary) == {
        "Game Loop": {"rule": "translate", "translation": "loop de jogo"},
        "Observer": {"rule": "preserve"},
        "AntiPattern": {"rule": "forbidden"},
    }
    assert load_glossary(glossary_path).entries == glossary.entries


def test_apply_glossary_with_usage_counts_terms_and_scans_output():
    glossary = Glossary(
        [
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo", required=True),
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE, required=True),
            GlossaryEntry("AntiPattern", GLOSSARY_RULE_FORBIDDEN),
        ]
    )

    application = apply_glossary_with_usage(
        "The game loop keeps observer and mentions AntiPattern.",
        glossary,
    )

    assert application.text == "The loop de jogo keeps Observer and mentions AntiPattern."
    assert application.usage.applied_terms == {"Game Loop": 1, "Observer": 1}
    assert application.usage.required_terms_present == {"Game Loop": 1, "Observer": 1}
    assert application.usage.forbidden_terms_found == {"AntiPattern": 1}


def test_glossary_usage_reports_missing_required_terms():
    glossary = Glossary(
        [
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo", required=True),
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE, required=True),
        ]
    )
    usage = GlossaryUsage()
    usage.merge(glossary.scan_output("A loop de jogo is present."))

    usage.finalize_required_terms(glossary)

    assert usage.required_terms_missing == ["Observer"]


def test_apply_glossary_preserve_rule_keeps_configured_term():
    glossary = Glossary(
        [
            GlossaryEntry("Observer", GLOSSARY_RULE_PRESERVE),
            GlossaryEntry("Game Loop", GLOSSARY_RULE_TRANSLATE, "loop de jogo"),
        ]
    )

    assert apply_glossary("observer uses the Game Loop.", glossary) == "Observer uses the loop de jogo."


def test_apply_glossary_accepts_advanced_mapping_directly():
    glossary = {
        "Observer": {"rule": "preserve"},
        "Game Loop": {"rule": "translate", "translation": "loop de jogo"},
    }

    assert apply_glossary("observer uses the Game Loop.", glossary) == "Observer uses the loop de jogo."


def test_load_glossary_rejects_unknown_rule(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Observer": {"rule": "unknown"}}', encoding="utf-8")

    with pytest.raises(GlossaryError, match="unsupported rule 'unknown'"):
        load_glossary(glossary_path)


def test_load_glossary_rejects_translate_rule_without_translation(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Observer": {"rule": "translate"}}', encoding="utf-8")

    with pytest.raises(GlossaryError, match="must include a string translation"):
        load_glossary(glossary_path)


def test_load_glossary_rejects_unsupported_fields(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"Observer": {"rule": "preserve", "translation": "Observador"}}', encoding="utf-8")

    with pytest.raises(GlossaryError, match="unsupported fields: translation"):
        load_glossary(glossary_path)


def test_load_glossary_rejects_non_boolean_required(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        '{"Observer": {"rule": "preserve", "required": "yes"}}',
        encoding="utf-8",
    )

    with pytest.raises(GlossaryError, match="field 'required' must be a boolean"):
        load_glossary(glossary_path)


def test_load_glossary_rejects_required_for_forbidden_terms(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        '{"AntiPattern": {"rule": "forbidden", "required": true}}',
        encoding="utf-8",
    )

    with pytest.raises(GlossaryError, match="unsupported fields: required"):
        load_glossary(glossary_path)


def test_load_glossary_rejects_empty_terms(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"": "empty"}', encoding="utf-8")

    with pytest.raises(GlossaryError, match="must not be empty"):
        load_glossary(glossary_path)


def test_load_glossary_missing_file_has_clear_error(tmp_path):
    with pytest.raises(GlossaryError, match="Glossary file not found"):
        load_glossary(tmp_path / "missing.json")
