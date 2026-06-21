import os
import json
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from rove.actions import ACTION_NAMES

logger = logging.getLogger(__name__)

HUMAN_MODES = ("browser_login", "terminal_question")


class LLMUnavailableError(Exception):
    """Raised when the LLM genuinely failed to produce a usable decision — distinct
    from a legitimate LLMDecision(action="CONTINUE"). `retryable` distinguishes a
    transient API/network failure from a deterministic one (bad JSON, empty choices,
    a model that can't produce parseable output) so callers know whether retrying
    again later could ever help."""
    def __init__(self, message: str, *, retryable: bool, cause: Exception | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause


async def _with_retry(fn, *, max_attempts=3, base_delay=0.5):
    """Retry a transient-failure-prone async call (the raw provider SDK call only —
    never wrap parsing/validation, which is deterministic and won't change on retry).
    3 attempts / exponential backoff (0.5s, 1s) mirrors Firecrawl/Crawl4AI's ~3-retry
    convention; total added latency (~1.5s worst case before the final attempt) stays
    small relative to existing page-load timeouts."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"LLM call attempt {attempt+1}/{max_attempts} failed: {e} — retrying in {delay}s")
                await asyncio.sleep(delay)
    raise LLMUnavailableError(f"LLM call failed after {max_attempts} attempts: {last_exc}",
                               retryable=True, cause=last_exc)


@dataclass
class LLMDecision:
    action: str                       # one of actions.ACTION_NAMES
    params: dict = field(default_factory=dict)
    reasoning: str = ""
    needs_human: bool = False
    human_mode: str | None = None     # "browser_login" | "terminal_question" | None
    adjustments: dict = field(default_factory=dict)  # optional crawl-mgmt: {deprioritize:[...], stop:bool}

    @classmethod
    def from_dict(cls, d: dict) -> "LLMDecision":
        """Build from untrusted model output — every field is validated/coerced so a
        malformed or adversarial response can never reach the action executor unchecked."""
        action = d.get("action", "CONTINUE")
        if action not in ACTION_NAMES:
            logger.warning(f"LLM returned unknown action {action!r} — defaulting to CONTINUE")
            action = "CONTINUE"

        params = d.get("params")
        if not isinstance(params, dict):
            params = {}

        human_mode = d.get("human_mode")
        if human_mode not in HUMAN_MODES:
            human_mode = None

        adjustments = d.get("adjustments")
        if not isinstance(adjustments, dict):
            adjustments = {}
        dep = adjustments.get("deprioritize")
        if isinstance(dep, str):
            adjustments["deprioritize"] = [dep]
        elif isinstance(dep, list):
            adjustments["deprioritize"] = [str(x) for x in dep]
        elif dep is not None:
            adjustments["deprioritize"] = []

        return cls(
            action=action,
            params=params,
            reasoning=str(d.get("reasoning", "")),
            needs_human=bool(d.get("needs_human", False)),
            human_mode=human_mode,
            adjustments=adjustments,
        )


class LLMClient(ABC):
    @abstractmethod
    async def decide(self, system: str, context: dict, actions: list) -> LLMDecision:
        """Given a system prompt, page/crawl context, and the available action schema,
        return a single structured decision."""

    @abstractmethod
    async def complete(self, system: str, prompt: str) -> str:
        """Free-form completion. Returns the assistant's text response."""


class FakeLLMClient(LLMClient):
    """Deterministic client for tests — returns scripted decisions in order."""
    def __init__(self, scripted=None, scripted_completions=None):
        self._scripted = list(scripted or [])
        self._scripted_completions = list(scripted_completions or [])

    async def decide(self, system, context, actions):
        return self._scripted.pop(0) if self._scripted else LLMDecision("CONTINUE")

    async def complete(self, system: str, prompt: str) -> str:
        return self._scripted_completions.pop(0) if self._scripted_completions else "{}"


# Without an explicit request timeout, a degraded endpoint can leave the SDK call
# hanging for minutes before it ever raises — observed live against NVIDIA NIM, which
# took ~5 minutes to return a 504 on its own. _with_retry's backoff only kicks in once
# a call actually fails, so a missing client-side timeout defeats the whole retry/
# escalation path by making "attempt 1 of 3" itself take several minutes.
_REQUEST_TIMEOUT_SECONDS = 30.0


class AnthropicClient(LLMClient):
    def __init__(self, model: str, api_key: str):
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS)
        self._model = model

    async def decide(self, system, context, actions):
        # Use a single tool whose input schema mirrors LLMDecision; force tool use.
        tool = {
            "name": "decide",
            "description": "Decide the next crawl action.",
            "input_schema": _decision_json_schema(actions),
        }
        msg = await _with_retry(lambda: self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "decide"},
            messages=[{"role": "user", "content": json.dumps(context)[:12000]}],
        ))
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "decide":
                return LLMDecision.from_dict(block.input)
        raise LLMUnavailableError("no tool_use block returned", retryable=False)

    async def complete(self, system: str, prompt: str) -> str:
        msg = await _with_retry(lambda: self._client.messages.create(
            model=self._model, max_tokens=2048, system=system,
            messages=[{"role": "user", "content": prompt[:12000]}],
        ))
        if not msg.content:
            raise LLMUnavailableError("no content returned", retryable=False)
        return msg.content[0].text


class OpenAIClient(LLMClient):
    def __init__(self, model: str, api_key: str, base_url: str | None = None, supports_json_schema: bool = True):
        import openai
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=_REQUEST_TIMEOUT_SECONDS)
        self._model = model
        self._supports_json_schema = supports_json_schema

    async def decide(self, system, context, actions):
        # Structured output via a JSON schema — some OpenAI-compatible backends (vLLM,
        # which NVIDIA NIM runs on) reject the bare `json_object` mode with a 400,
        # demanding an explicit schema instead. Other OpenAI-compatible backends (many
        # Ollama/local-model and OpenRouter-proxied models) don't implement json_schema
        # at all and need the more widely-supported json_object mode instead.
        if self._supports_json_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "decision", "schema": _decision_json_schema(actions)},
            }
        else:
            response_format = {"type": "json_object"}
        resp = await _with_retry(lambda: self._client.chat.completions.create(
            model=self._model,
            response_format=response_format,
            messages=[
                {"role": "system", "content": system + "\nRespond ONLY with JSON matching the decision schema."},
                {"role": "user", "content": json.dumps(context)[:12000]},
            ],
        ))
        if not resp.choices:
            raise LLMUnavailableError("provider returned no choices for a decision request", retryable=False)
        raw = resp.choices[0].message.content
        if not raw or not raw.strip():
            # Some "thinking"-style models (e.g. google/diffusiongemma-26b-a4b-it via
            # NVIDIA NIM) return empty/whitespace content instead of the structured JSON
            # — falling back to "{}" here would silently parse into a legitimate-looking
            # CONTINUE decision, hiding the failure instead of escalating it.
            raise LLMUnavailableError("provider returned empty content", retryable=False)
        try:
            return LLMDecision.from_dict(json.loads(_strip_markdown_fence(raw)))
        except Exception as e:
            raise LLMUnavailableError(f"decision parse failed: {e}", retryable=False, cause=e)

    async def complete(self, system: str, prompt: str) -> str:
        resp = await _with_retry(lambda: self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt[:12000]},
            ],
        ))
        if not resp.choices:
            raise LLMUnavailableError("provider returned no choices for a completion request", retryable=False)
        return resp.choices[0].message.content or ""


def _strip_markdown_fence(raw: str) -> str:
    """Some models (e.g. google/diffusiongemma-26b-a4b-it via NVIDIA NIM) wrap JSON in
    a ```json ... ``` fence even under response_format=json_schema, which a strict
    json.loads rejects outright. Strip a single leading/trailing fence if present;
    leave anything else untouched so a genuine parse failure still surfaces as one."""
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if s.startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _decision_json_schema(actions: list) -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [a["name"] for a in actions]},
            "params": {"type": "object"},
            "reasoning": {"type": "string"},
            "needs_human": {"type": "boolean"},
            "human_mode": {"type": "string", "enum": ["browser_login", "terminal_question"]},
            "adjustments": {"type": "object"},
        },
        "required": ["action", "reasoning"],
    }


def make_llm_client(provider: str, model: str, api_key: str = "", base_url: str | None = None) -> LLMClient | None:
    """Factory. provider='none' (or missing key) returns None → master runs heuristic-only."""
    provider = (provider or "none").lower()
    if provider == "none":
        return None
    key = api_key or os.environ.get(
        {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "local": "LOCAL_LLM_API_KEY",
         "nvidia": "NVIDIA_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(provider, ""),
        "",
    )
    try:
        if provider == "anthropic":
            return AnthropicClient(model or "claude-sonnet-4-6", key)
        if provider == "openai":
            return OpenAIClient(model or "gpt-4o", key)
        if provider == "local":
            # OpenAI-compatible local endpoint (Ollama, LM Studio, vLLM). Most local
            # model servers (Ollama in particular) don't implement response_format=
            # json_schema, so fall back to the more widely-supported json_object mode.
            return OpenAIClient(
                model,
                key or "not-needed",
                base_url or os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1"),
                supports_json_schema=False,
            )
        if provider == "nvidia":
            # NVIDIA's hosted NIM catalog — also OpenAI-compatible. Any chat-capable
            # model in the catalog works via --master-model, e.g. "minimaxai/minimax-m3"
            # or "nvidia/nemotron-3-ultra-550b-a55b".
            return OpenAIClient(
                model or "nvidia/nemotron-3-ultra-550b-a55b",
                key,
                base_url or os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            )
        if provider == "openrouter":
            # OpenRouter — also OpenAI-compatible, proxies to many backing providers
            # (Anthropic, OpenAI, Google, etc). Pick any model slug via --master-model,
            # e.g. "anthropic/claude-sonnet-4.5" or "google/gemini-2.5-flash". Whether
            # json_schema is supported depends on which backing model is selected, so
            # use the more widely-supported json_object mode rather than assuming.
            return OpenAIClient(
                model or "anthropic/claude-sonnet-4.5",
                key,
                base_url or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                supports_json_schema=False,
            )
    except Exception as e:
        logger.error(f"Could not init LLM provider {provider}: {e} — falling back to heuristic-only")
        return None
    logger.warning(f"Unknown provider {provider!r} — heuristic-only")
    return None
