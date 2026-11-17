"""``ModelPort`` over Ollama's ``/api/chat``: one non-streamed call, with the core's JSON schema as ``format``.

The core owns the prompt, batching, the strict result check and the failure families; this module
maps one ``ModelRequest`` to one chat call and the answer back, and nothing more.
"""
from __future__ import annotations

from ...kernel import JobFailure, JobFailureFamily, JobOperationError
from ..llm import DecodingSettings, FinishReason, ModelCallError, ModelRequest, ModelResponse, TransportReason
from .client import OllamaClient, OllamaError

_FINISH = {"stop": FinishReason.STOP, "length": FinishReason.LENGTH}
_TRANSPORT = {"policy": TransportReason.POLICY, "timeout": TransportReason.TIMEOUT,
              "connection": TransportReason.CONNECTION, "request": TransportReason.REQUEST}


def chat_options(decoding: DecodingSettings) -> dict:
    """Every decoding value the core treats as identity is sent; nothing else is."""
    options: dict = {"temperature": float(decoding.temperature), "num_predict": decoding.max_output_tokens}
    if decoding.top_p is not None:
        options["top_p"] = float(decoding.top_p)
    if decoding.seed is not None:
        options["seed"] = decoding.seed
    return options


def _call_error(error: OllamaError) -> Exception:
    if error.reason == "http":
        return ModelCallError(TransportReason.HTTP, status_code=error.status_code,
                              retry_after_seconds=error.retry_after_seconds)
    if error.reason in _TRANSPORT:
        return ModelCallError(_TRANSPORT[error.reason])
    code = "provider_redirect_refused" if error.reason == "redirect" else "malformed_response"
    return JobOperationError(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, code))


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**63 else None


def _response(data: dict) -> ModelResponse:
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    finish = _FINISH.get(data.get("done_reason", "stop"))
    if data.get("done") is not True or not isinstance(content, str) or finish is None:
        raise JobOperationError(JobFailure(JobFailureFamily.PROVIDER_PERMANENT, "malformed_response"))
    return ModelResponse(finish, content, _count(data.get("prompt_eval_count")), _count(data.get("eval_count")))


class OllamaChatModel:
    """One installed model behind one client. ``close`` releases the client's HTTP session."""

    def __init__(self, client: OllamaClient, model_name: str) -> None:
        self._client, self._name = client, model_name

    def __repr__(self) -> str:
        return "OllamaChatModel()"

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = {
            "model": self._name, "stream": False, "format": request.response_schema,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "options": chat_options(request.decoding),
        }
        try:
            data = self._client.chat(body)
        except OllamaError as error:
            raise _call_error(error) from None
        return _response(data)

    def close(self) -> None:
        self._client.close()
