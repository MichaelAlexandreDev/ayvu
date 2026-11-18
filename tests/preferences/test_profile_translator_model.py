"""Named profiles reuse each provider's existing model validation and round-trip fields (#377)."""
import pytest

from ayvu.preferences import AyvuConfig, ConfigError


@pytest.mark.parametrize('translator,model', [('libretranslate', 'local-model'), ('ollama', 'qwen2.5:7b')])
def test_profile_selection_round_trips(translator, model):
    profile = {'translator': translator, 'model': model}
    configuration = AyvuConfig.from_dict({'profiles': {'chosen': profile}})
    assert configuration.profiles['chosen'].translator == translator
    assert configuration.profiles['chosen'].model == model
    assert configuration.to_dict()['profiles']['chosen'] == profile


@pytest.mark.parametrize('translator,model', [('libretranslate', 'UPPER'), ('ollama', 'bad model'),
                                              ('unknown', 'model'), ([], 'model')])
def test_invalid_profile_selection_is_refused(translator, model):
    with pytest.raises(ConfigError):
        AyvuConfig.from_dict({'profiles': {'chosen': {'translator': translator, 'model': model}}})
