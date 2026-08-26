"""Tests for OpenAICompatibleLLMClient against a real local HTTP server
(stdlib http.server, no external network) — this exercises the actual
urllib request/response path, not a mocked transport, which is what
matters for a client whose entire point is "works with any server
speaking this REST contract."
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import BaseModel

from mongopg_migrate.mapping.llm_client import (
    LLMClientError,
    OpenAICompatibleLLMClient,
    build_llm_client,
)


class _Suggestion(BaseModel):
    field: str
    action: str


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Replays whatever mongopg_migrate.tests configured on the class
    before each request — a stand-in for OpenAI/Ollama/vLLM/etc."""

    status = 200
    body_fn = None  # callable(request_json) -> dict to send back
    last_request = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).last_request = {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)}

        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response_body = type(self).body_fn(type(self).last_request["body"]) if type(self).body_fn else {}
        self.wfile.write(json.dumps(response_body).encode("utf-8"))

    def log_message(self, format, *args):  # silence test output
        pass


@pytest.fixture
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _ScriptedHandler.status = 200
    _ScriptedHandler.body_fn = None
    _ScriptedHandler.last_request = None
    yield server, f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    thread.join()


def _openai_shaped_response(content_obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content_obj)}}]}


def test_successful_round_trip_parses_and_validates_response(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.body_fn = lambda _req: _openai_shaped_response({"field": "name", "action": "map"})

    client = OpenAICompatibleLLMClient(base_url=base_url, model="test-model")
    result = client.suggest(system="sys prompt", user_payload={"a": 1}, output_schema=_Suggestion)

    assert isinstance(result, _Suggestion)
    assert result.field == "name"
    assert result.action == "map"


def test_request_shape_matches_openai_chat_completions_contract(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.body_fn = lambda _req: _openai_shaped_response({"field": "x", "action": "none"})

    client = OpenAICompatibleLLMClient(base_url=base_url, model="my-model", api_key="secret-key")
    client.suggest(system="the system prompt", user_payload={"unresolved_fields": ["name"]}, output_schema=_Suggestion)

    req = _ScriptedHandler.last_request
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer secret-key"
    assert req["body"]["model"] == "my-model"
    assert req["body"]["messages"][0] == {"role": "system", "content": "the system prompt"}
    assert json.loads(req["body"]["messages"][1]["content"]) == {"unresolved_fields": ["name"]}
    assert req["body"]["response_format"]["type"] == "json_schema"


def test_no_api_key_means_no_authorization_header(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.body_fn = lambda _req: _openai_shaped_response({"field": "x", "action": "none"})

    client = OpenAICompatibleLLMClient(base_url=base_url, model="local-model")  # no api_key — e.g. local Ollama
    client.suggest(system="s", user_payload={}, output_schema=_Suggestion)

    assert "Authorization" not in _ScriptedHandler.last_request["headers"]


def test_json_object_mode_is_requested_when_configured(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.body_fn = lambda _req: _openai_shaped_response({"field": "x", "action": "none"})

    client = OpenAICompatibleLLMClient(base_url=base_url, model="m", response_format="json_object")
    client.suggest(system="s", user_payload={}, output_schema=_Suggestion)

    assert _ScriptedHandler.last_request["body"]["response_format"] == {"type": "json_object"}


def test_http_error_status_raises_llm_client_error_with_detail(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.status = 500
    _ScriptedHandler.body_fn = lambda _req: {"error": "internal server error"}

    client = OpenAICompatibleLLMClient(base_url=base_url, model="m")
    with pytest.raises(LLMClientError, match="HTTP 500"):
        client.suggest(system="s", user_payload={}, output_schema=_Suggestion)


def test_malformed_response_shape_raises_llm_client_error(fake_server):
    _server, base_url = fake_server
    _ScriptedHandler.body_fn = lambda _req: {"not": "the expected shape"}

    client = OpenAICompatibleLLMClient(base_url=base_url, model="m")
    with pytest.raises(LLMClientError, match="unexpected response shape"):
        client.suggest(system="s", user_payload={}, output_schema=_Suggestion)


def test_response_content_not_matching_schema_raises_llm_client_error(fake_server):
    _server, base_url = fake_server
    # Valid JSON, but missing the required "action" field.
    _ScriptedHandler.body_fn = lambda _req: _openai_shaped_response({"field": "name"})

    client = OpenAICompatibleLLMClient(base_url=base_url, model="m")
    with pytest.raises(LLMClientError, match="did not match the expected schema"):
        client.suggest(system="s", user_payload={}, output_schema=_Suggestion)


def test_unreachable_server_raises_llm_client_error():
    # Port 1 is reserved/unlikely to be listening — a stand-in for "server down".
    client = OpenAICompatibleLLMClient(base_url="http://127.0.0.1:1", model="m", timeout=2.0)
    with pytest.raises(LLMClientError, match="could not reach"):
        client.suggest(system="s", user_payload={}, output_schema=_Suggestion)


# --- build_llm_client factory ---------------------------------------------------------


def test_build_llm_client_openai_compatible_requires_base_url():
    with pytest.raises(LLMClientError, match="requires --llm-base-url"):
        build_llm_client("openai-compatible", "some-model")


def test_build_llm_client_openai_compatible_constructs_client():
    client = build_llm_client("openai-compatible", "some-model", base_url="http://localhost:11434/v1")
    assert isinstance(client, OpenAICompatibleLLMClient)
    assert client.base_url == "http://localhost:11434/v1"
    assert client.model == "some-model"


def test_build_llm_client_anthropic_constructs_client():
    from mongopg_migrate.mapping.llm_client import AnthropicLLMClient

    client = build_llm_client("anthropic", "claude-opus-5")
    assert isinstance(client, AnthropicLLMClient)


def test_build_llm_client_rejects_unknown_provider():
    with pytest.raises(LLMClientError, match="unknown LLM provider"):
        build_llm_client("some-made-up-provider", "model")
