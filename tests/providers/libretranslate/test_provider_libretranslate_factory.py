from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


from ayvu.providers.ports import ProviderRoute
from ayvu.providers.libretranslate.adapter import LibreTranslateProvider, LibreTranslateProviderFactory
from ayvu.providers.service import execute
from ayvu.providers.libretranslate.client import LibreTranslateTranslator, RequestRateLimiter, Translator
from tests.support.provider_fakes import FakeSession, request

from tests.providers.libretranslate.provider_libretranslate_support import BRIDGE
from tests.providers.libretranslate.provider_libretranslate_support import bare_factory, libre_registry, ok
from tests.providers.libretranslate.provider_libretranslate_support import translator


def test_sequential_use_has_one_translator_and_one_session():
    sessions = []
    target = libre_registry(LibreTranslateProviderFactory(bare_factory(sessions)))
    with target.open('libre_local') as opened:
        assert execute(opened, request()).text == 'Ola'
    assert len(sessions) == 1 and sessions[0].closed == 1


def test_each_parallel_task_gets_its_own_session_and_shares_the_rate_limiter():
    limiter = RequestRateLimiter(requests_per_second=None)
    sessions = []
    created = []

    def make():
        base = bare_factory(sessions, rate_limiter=limiter)()
        created.append(base)
        return base

    target = libre_registry(LibreTranslateProviderFactory(make))
    barrier = Barrier(4)

    def task(_):
        with target.open('libre_local') as opened:
            barrier.wait(timeout=5)
            return execute(opened, request()).text

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(task, range(4))) == ['Ola'] * 4
    assert len({id(session) for session in sessions}) == 4
    assert {id(base.rate_limiter) for base in created} == {id(limiter)}
    assert [session.closed for session in sessions] == [1, 1, 1, 1] and all(len(session.posts) == 1 for session in sessions)


def test_factory_applies_the_configured_route():
    sessions = []

    def make():
        base = LibreTranslateTranslator(retries=0)
        base.session = FakeSession([ok('Hello'), ok('Ola')])
        sessions.append(base.session)
        return base

    target = libre_registry(LibreTranslateProviderFactory(make, route=BRIDGE))
    with target.open('libre_local') as opened:
        result = execute(opened, request('Salut', 'fr', 'pt'))
    assert result.route == ProviderRoute('fr', 'pt', 'en') and len(sessions[0].posts) == 2


def test_a_translator_without_a_session_needs_no_cleanup():
    class Plain(Translator):
        def translate(self, text, source, target):
            return text.upper()

    target = libre_registry(LibreTranslateProviderFactory(Plain))
    with target.open('libre_local') as opened:
        assert execute(opened, request()).text == 'HELLO'


def test_explicit_cleanup_runs_on_close():
    calls = []
    LibreTranslateProvider(translator(), cleanup=lambda: calls.append(1)).close()
    LibreTranslateProvider(translator()).close()
    assert calls == [1]
