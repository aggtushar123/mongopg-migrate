"""The pluggable LLM seam (PRD §8: "pluggable... Configurable to use local
models to keep it fully offline-capable").

`mapping/llm_propose.py` never imports a vendor SDK directly — it only
depends on the `LLMClient` protocol below, so a local-model backend (e.g.
an Ollama- or llama.cpp-fronting client) is a drop-in: implement `suggest()`
with the same signature and pass it in instead of `AnthropicLLMClient`.
Nothing else in the LLM-assist path changes.

`AnthropicLLMClient` is the shipped default. It imports the `anthropic`
package lazily (inside `suggest()`, not at module import time) so the
package is only required when `--llm` is actually used — it's an optional
extra (`pip install mongopg-migrate[llm]`), not a hard dependency, matching
PRD §8's "off by default".
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel

DEFAULT_MODEL = "claude-opus-5"


class LLMClientError(Exception):
    """Raised for any failure talking to the LLM — missing package, auth,
    rate limit, network, API error. Callers (mapping/llm_propose.py) catch
    this per-entity and report it as a ProposalIssue rather than aborting
    the whole `propose` run: a transient LLM failure shouldn't discard the
    rule-based mapping already produced."""


class LLMClient(Protocol):
    def suggest(self, *, system: str, user_payload: dict, output_schema: type[BaseModel]) -> BaseModel: ...


class AnthropicLLMClient:
    """Default LLMClient, backed by the Anthropic API's structured-output
    path (`client.messages.parse(..., output_format=<pydantic model>)`).

    Only ever receives `user_payload` built by
    `mapping/llm_propose.build_llm_payload` — field names, types, and
    shapes, never a document or row value (PRD §8 privacy requirement).
    """

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self.model = model
        self._api_key = api_key

    def suggest(self, *, system: str, user_payload: dict, output_schema: type[BaseModel]) -> BaseModel:
        try:
            import anthropic
        except ImportError as e:
            raise LLMClientError(
                "the `anthropic` package is required for --llm — install with "
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
