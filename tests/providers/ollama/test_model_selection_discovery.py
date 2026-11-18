"""Settings discovery exposes validated names and bounded failures, never raw server metadata (#377)."""
from types import SimpleNamespace

import pytest

from ayvu.providers import ProviderSetupError
from ayvu.providers.ollama.client import InstalledModel, OllamaError
from ayvu.providers.ollama.discovery import discover_model_names


def test_names_are_deduplicated_and_sorted():
    models = [InstalledModel('z:1b', 'a' * 64), InstalledModel('a:1b', 'b' * 64), InstalledModel('z:1b', 'a' * 64)]
    assert discover_model_names(SimpleNamespace(list_models=lambda: models)) == ('a:1b', 'z:1b')


@pytest.mark.parametrize('name', ['bad model', '[red]name', 'https://user:password@host', 'name\nsecret'])
def test_unsafe_server_names_are_not_offered_to_settings(name):
    client = SimpleNamespace(list_models=lambda: [InstalledModel(name, 'a' * 64)])
    with pytest.raises(ProviderSetupError, match='^malformed_models$'):
        discover_model_names(client)


def test_discovery_error_contains_only_the_existing_failure_code():
    def unavailable():
        raise OllamaError('connection')
    with pytest.raises(ProviderSetupError, match='^provider_unavailable$'):
        discover_model_names(SimpleNamespace(list_models=unavailable))
