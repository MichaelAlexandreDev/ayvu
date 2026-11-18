"""Composition owns provider-specific model precedence and keeps inputs immutable (#377)."""
import pytest

from ayvu.composition.cli import select_translator
from ayvu.preferences import AyvuConfig, TranslationProfile
from ayvu.translation import PreflightError


@pytest.mark.parametrize('flags,expected', [
    ({}, ('ollama', 'profile:7b', 'http://localhost:11434')),
    ({'model': 'flag:3b'}, ('ollama', 'flag:3b', 'http://localhost:11434')),
    ({'translator': 'libretranslate'}, ('libretranslate', 'config-label', 'http://localhost:5000')),
])
def test_flags_override_a_profile_without_borrowing_another_providers_model(flags, expected):
    config = AyvuConfig(translator_models={'libretranslate': 'config-label', 'ollama': 'config:1b'})
    profile = TranslationProfile(translator='ollama', model='profile:7b')
    selection = select_translator(config, profile=profile, **flags)
    assert (selection.translator, selection.model, selection.url) == expected
    assert config.translator_models == {'libretranslate': 'config-label', 'ollama': 'config:1b'}


@pytest.mark.parametrize('translator', ['unknown', 'openai', 'anthropic'])
def test_disabled_providers_keep_the_specific_early_refusal(translator):
    with pytest.raises(PreflightError) as raised:
        select_translator(AyvuConfig(), translator=translator, model='label')
    assert f'Unsupported translator: {translator}' in raised.value.detail
