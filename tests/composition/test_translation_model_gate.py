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
