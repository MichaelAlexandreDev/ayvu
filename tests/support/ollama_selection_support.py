"""In-process Ollama HTTP answers for selection tests; no socket or real model."""
import json

from tests.support.cli_rich_runtime_support import Transport
from tests.support.ollama_fakes import DIGEST_A, MODEL, structured


class OllamaTransport(Transport):
    def __init__(self, *, unavailable=False, failing=False):
        super().__init__()
        self.unavailable, self.failing = unavailable, failing
        self.models = [(MODEL, DIGEST_A), ('llama3.2:3b', 'b' * 64)]

    def get(self, url, **kwargs):
        self.requests.append(('GET', url, None))
        assert url.endswith('/api/tags'), url
        return self._response({'models': [{'name': name, 'digest': digest} for name, digest in self.models]},
                              503 if self.unavailable else 200)

    def post(self, url, **kwargs):
        body = kwargs['json']
        self.requests.append(('POST', url, body))
        assert url.endswith('/api/chat'), url
        if self.failing and 'Goodbye reader.' in body['messages'][1]['content']:
            return self._response({'error': 'unavailable'}, 503)
        return self._response(structured(body))
