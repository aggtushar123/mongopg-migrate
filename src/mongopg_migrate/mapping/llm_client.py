"""The pluggable LLM seam (PRD §8: "pluggable... Configurable to use local
models to keep it fully offline-capable").

`mapping/llm_propose.py` never imports a vendor SDK directly — it only
depends on the `LLMClient` protocol below. Two implementations ship here:

- `AnthropicLLMClient` — the Anthropic API's structured-output path.
- `OpenAICompatibleLLMClient` — plain stdlib HTTP against the OpenAI
  chat-completions REST contract, which is what makes this genuinely
  provider-agnostic rather than tied to one vendor: that contract is what
  OpenAI itself, Azure OpenAI, and virtually every self-hosted/local
  runtime (Ollama, vLLM, LM Studio, llama.cpp server, Together, Groq,
  OpenRouter, Fireworks, ...) all implement. Zero added dependency — it's
  `urllib.request`, not a per-vendor SDK — so it works with any backend
  that speaks the contract, including ones that don't exist yet.

`build_llm_client()` is the CLI's `--llm-provider` knob. A third backend
that fits neither shape (a genuinely different protocol, an in-process
model) doesn't need a CLI flag: implement `LLMClient` and pass the instance
directly to `mapping.llm_propose.enrich_mapping_with_llm()` — nothing else
in the LLM-assist path needs to know or care.

Both implementations import their transport dependency lazily (inside
`suggest()`, not at module import time): `anthropic` is only required when
the `anthropic` provider is actually used, and is an optional extra
(`pip install mongopg-migrate[llm]`), not a hard dependency — matching PRD
§8's "off by default". `OpenAICompatibleLLMClient` needs no package at all.
"""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import BaseModel

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
PROVIDERS = ("anthropic", "openai-compatible")


class LLMClientError(Exception):
    """Raised for any failure talking to the LLM — missing package, auth,
    rate limit, network, timeout, API error, or a response that doesn't
    match the requested schema. Callers (mapping/llm_propose.py) catch this
    per-entity and report it as a ProposalIssue rather than aborting the
    whole `propose` run: a transient LLM failure shouldn't discard the
    rule-based mapping already produced."""


class LLMClient(Protocol):
    def suggest(self, *, system: str, user_payload: dict, output_schema: type[BaseModel]) -> BaseModel: ...


class AnthropicLLMClient:
    """LLMClient backed by the Anthropic API's structured-output path
    (`client.messages.parse(..., output_format=<pydantic model>)`).

    Only ever receives `user_payload` built by
    `mapping/llm_propose.build_llm_payload` — field names, types, and
    shapes, never a document or row value (PRD §8 privacy requirement).
    """

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: str | None = None):
        self.model = model
        self._api_key = api_key

    def suggest(self, *, system: str, user_payload: dict, output_schema: type[BaseModel]) -> BaseModel:
        try:
            import anthropic
        except ImportError as e:
            raise LLMClientError(
                "the `anthropic` package is required for --llm-provider anthropic — install with "
                "`pip install mongopg-migrate[llm]`"
            ) from e

        client = anthropic.Anthropic(api_key=self._api_key) if self._api_key else anthropic.Anthropic()
        try:
            response = client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": json.dumps(user_payload)}],
                output_format=output_schema,
            )
        except anthropic.AuthenticationError as e:
            raise LLMClientError(
                "Anthropic authentication failed — set ANTHROPIC_API_KEY, or run `ant auth login`"
            ) from e
        except anthropic.RateLimitError as e:
            raise LLMClientError(f"Anthropic rate limit hit: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMClientError(f"Anthropic API error ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMClientError(f"could not reach the Anthropic API: {e}") from e

        return response.parsed_output


class OpenAICompatibleLLMClient:
    """LLMClient for any server implementing the OpenAI chat-completions
    REST contract — see module docstring for what that covers. Configured
    by `base_url` + `model`; `api_key` is optional since many local
    runtimes (Ollama, LM Studio, a bare llama.cpp server) accept none.

    Structured output: requests JSON via `response_format`. Defaults to
    `"json_schema"` (OpenAI, Azure OpenAI, and most modern local runtimes);
    pass `response_format="json_object"` for a backend that only supports
    plain JSON mode (some older/smaller local servers). Either way, the
    response is parsed and validated against `output_schema` before being
    trusted — the OpenAI-compatible ecosystem is wide enough in practice
    that "the server actually followed the schema" can't be assumed, same
    discipline as AnthropicLLMClient.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        response_format: Literal["json_schema", "json_object"] = "json_schema",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.response_format = response_format
        self.timeout = timeout

    def suggest(self, *, system: str, user_payload: dict, output_schema: type[BaseModel]) -> BaseModel:
        import urllib.error
        import urllib.request

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
        }
        if self.response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "schema": output_schema.model_json_schema(),
                    "strict": True,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMClientError(f"LLM server returned HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMClientError(f"could not reach LLM server at {self.base_url}: {e.reason}") from e
        except TimeoutError as e:
            raise LLMClientError(f"LLM server at {self.base_url} timed out after {self.timeout}s") from e

        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMClientError(f"unexpected response shape from LLM server: {raw!r}") from e

        try:
            return output_schema.model_validate_json(content)
        except Exception as e:
            raise LLMClientError(
                f"LLM response at {self.base_url} did not match the expected schema — "
                f"got: {content[:500]!r}"
            ) from e


def build_llm_client(
    provider: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> LLMClient:
    """Constructs the configured LLMClient by name — backs the CLI's
    `--llm-provider` flag. Calling this library programmatically doesn't
    need it: build any `LLMClient`-shaped object yourself and pass it
    straight to `mapping.llm_propose.enrich_mapping_with_llm()`."""
    if provider == "anthropic":
        return AnthropicLLMClient(model=model, api_key=api_key)
    if provider == "openai-compatible":
        if not base_url:
            raise LLMClientError(
                "--llm-provider openai-compatible requires --llm-base-url "
                "(e.g. https://api.openai.com/v1, or http://localhost:11434/v1 for Ollama)"
            )
        return OpenAICompatibleLLMClient(base_url=base_url, model=model, api_key=api_key)
    raise LLMClientError(f"unknown LLM provider {provider!r} — use one of {PROVIDERS}")
