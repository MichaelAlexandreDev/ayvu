"""A fake Ollama server on a real loopback socket: ``GET /api/tags`` and ``POST /api/chat``, nothing else.

It answers the core's structured schema the way a model would, reading the rendered JSON data, so tests
drive the real client, chat port, LLM core and network policy. No real Ollama and no internet is used.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
MODEL = "qwen2.5:7b"


def default_translation(text: str) -> str:
    return f"PT[{text}]"


def chat_answer(content: str, *, done_reason: str = "stop") -> dict:
    return {"model": MODEL, "message": {"role": "assistant", "content": content}, "done": True,
            "done_reason": done_reason, "prompt_eval_count": 12, "eval_count": 7}


def structured(body: dict, translate=default_translation) -> dict:
    """The answer a well-behaved model gives: every segment, by id, through ``translate``."""
    segments = json.loads(body["messages"][1]["content"])["segments"]
    return chat_answer(json.dumps({"translations": [{"id": item["id"], "text": translate(item["text"])}
                                                    for item in segments]}))


class FakeOllama:
    """``script`` holds one step per chat call: a callable taking the request body and returning
    ``(status, payload, headers)``; ``payload`` is a dict (sent as JSON) or bytes. With no step left,
    the server answers through ``translate``."""

    def __init__(self, models=((MODEL, DIGEST_A),), *, translate=default_translation) -> None:
        self.models = list(models)
        self.translate = translate
        self.script: list = []
        self.requests: list[tuple[str, str]] = []
        self.chat_bodies: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeOllama:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat_count(self) -> int:
        return len(self.chat_bodies)

    def _tags(self) -> tuple[int, dict, dict]:
        return 200, {"models": [{"name": name, "model": name, "digest": digest, "size": 1}
                                for name, digest in self.models]}, {}

    def _chat(self, body: dict) -> tuple[int, object, dict]:
        with self._lock:
            step = self.script.pop(0) if self.script else None
        if step is None:
            return 200, structured(body, self.translate), {}
        return step(body)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _reply(self, status: int, payload: object, headers: dict) -> None:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path))
                self._reply(*fake._tags()) if self.path == "/api/tags" else self._reply(404, {"error": "no"}, {})

            def do_POST(self):
                fake.requests.append(("POST", self.path))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/chat":
                    self._reply(404, {"error": "no"}, {})
                    return
                fake.chat_bodies.append(body)
                self._reply(*fake._chat(body))

        return Handler


def slow(seconds: float):
    """A step that answers only after ``seconds``, to exceed a client timeout."""
    def step(body):
        time.sleep(seconds)
        return 200, structured(body), {}
    return step


def reply(status: int, payload: object = None, headers: dict | None = None):
    return lambda body: (status, {"error": "x"} if payload is None else payload, headers or {})


@pytest.fixture
def fake_ollama():
    server = FakeOllama().start()
    yield server
    server.close()
