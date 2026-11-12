from ayvu.kernel import LanguagePair, TranslationOptions
from tests.support.cli_support import isolated_config


def test_translation_options_exposes_language_pair_values():
    language_pair = LanguagePair(source="en", target="pt")

    options = TranslationOptions(language_pair=language_pair)

    assert options.source == "en"
    assert options.target == "pt"
