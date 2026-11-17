"""Composition admission holds even for callers outside the command parser."""
import pytest

from ayvu.composition import cli as root
from ayvu.translation import PreflightError
from ayvu.kernel import LanguagePair, OutputPlan, TranslationOptions
from tests.support.model_label_support import REFUSAL, snapshot


@pytest.mark.parametrize("entry", [root._translate_in_project, root._translate_without_project])
def test_composition_refuses_before_preparation_or_storage(tmp_path, entry):
    prepared = []
    source = tmp_path / "book.epub"
    options = TranslationOptions(LanguagePair("en", "pt"))
    book = root._BookRequest(source, OutputPlan.for_translation(source, tmp_path / "out.epub", options.language_pair),
        False, options, tmp_path / "cache.sqlite", lambda: prepared.append(True))
    hooks = root._BookHooks(*(lambda *args: None for _ in range(4)))
    before = snapshot(tmp_path)
    with pytest.raises(PreflightError, match=REFUSAL):
        entry(book, hooks)
    assert prepared == [] and snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["ollama", " Ollama "])
def test_a_translator_with_a_discovered_identity_is_admitted_without_a_label(name):
    """Ollama's identity is the digest its preflight discovers; that preflight refuses a missing model."""
    assert root.require_translation_model({}, name) is None
    assert root.require_translation_model({"libretranslate": "local-v1"}, name) is None


def test_libretranslate_without_a_label_is_still_refused():
    with pytest.raises(PreflightError, match=REFUSAL):
        root.require_translation_model({"ollama": "qwen2.5:7b"}, "libretranslate")


def test_an_unknown_translator_still_gets_its_own_error():
    with pytest.raises(PreflightError) as raised:
        root.require_translation_model({"ollama": "qwen2.5:7b"}, "unknown")
    assert raised.value.summary == "Não foi possível preparar o tradutor."
    assert raised.value.detail.startswith("Unsupported translator: unknown.")
