import pytest
from rove.llm import LLMDecision, make_llm_client, FakeLLMClient, OpenAIClient, AnthropicClient, LLMUnavailableError


def test_decision_from_dict_defaults():
    d = LLMDecision.from_dict({"action": "CONTINUE", "reasoning": "looks fine"})
    assert d.action == "CONTINUE"
    assert d.params == {}
    assert d.needs_human is False
    assert d.human_mode is None


def test_decision_escalation_parsed():
    d = LLMDecision.from_dict({
        "action": "ESCALATE_HUMAN",
        "reasoning": "password wall, cannot proceed",
        "needs_human": True,
        "human_mode": "browser_login",
    })
    assert d.needs_human is True
    assert d.human_mode == "browser_login"


def test_unknown_action_defaults_to_continue():
    d = LLMDecision.from_dict({"action": "DROP_TABLE", "reasoning": "x"})
    assert d.action == "CONTINUE"


def test_invalid_human_mode_nulled():
    d = LLMDecision.from_dict({"action": "ESCALATE_HUMAN", "human_mode": "rm -rf"})
    assert d.human_mode is None


def test_deprioritize_string_coerced_to_list():
    d = LLMDecision.from_dict({"action": "CONTINUE", "adjustments": {"deprioritize": "/admin"}})
    assert d.adjustments["deprioritize"] == ["/admin"]


def test_non_dict_params_coerced():
    d = LLMDecision.from_dict({"action": "CONTINUE", "params": "oops"})
    assert d.params == {}


def test_factory_none_provider_returns_none():
    assert make_llm_client("none", model="", api_key="") is None


def test_factory_nvidia_provider_uses_nvidia_base_url_and_default_model(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    client = make_llm_client("nvidia", model="", api_key="")
    assert isinstance(client, OpenAIClient)
    assert client._model == "nvidia/nemotron-3-ultra-550b-a55b"
    assert "integrate.api.nvidia.com/v1" in str(client._client.base_url)


def test_factory_nvidia_provider_respects_custom_model(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    client = make_llm_client("nvidia", model="minimaxai/minimax-m3", api_key="")
    assert client._model == "minimaxai/minimax-m3"


async def test_fake_client_returns_scripted_decision():
    fake = FakeLLMClient(scripted=[LLMDecision("DISMISS_OVERLAY", {"selector": "#accept"}, "cookie banner")])
    d = await fake.decide(system="x", context={}, actions=[])
    assert d.action == "DISMISS_OVERLAY"
    assert d.params["selector"] == "#accept"


class _FakeEmptyChoicesResponse:
    choices = []


class _FakeOpenAISDKClient:
    """Stands in for openai.AsyncOpenAI — returns a response with an empty choices
    list, as some OpenAI-compatible providers (observed: NVIDIA NIM with
    minimaxai/minimax-m3 under response_format=json_object) do instead of raising."""

    class _Completions:
        async def create(self, **kwargs):
            return _FakeEmptyChoicesResponse()

    class _Chat:
        def __init__(self):
            self.completions = _FakeOpenAISDKClient._Completions()

    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.chat = self._Chat()


async def test_openai_client_decide_handles_empty_choices(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAISDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    with pytest.raises(LLMUnavailableError) as exc_info:
        await client.decide(system="x", context={}, actions=[])
    assert exc_info.value.retryable is False


async def test_openai_client_complete_handles_empty_choices(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAISDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    with pytest.raises(LLMUnavailableError) as exc_info:
        await client.complete(system="x", prompt="y")
    assert exc_info.value.retryable is False


class _AlwaysFailingCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise ConnectionError("boom")


class _AlwaysFailingChat:
    def __init__(self):
        self.completions = _AlwaysFailingCompletions()


class _AlwaysFailingSDKClient:
    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.chat = _AlwaysFailingChat()


async def test_openai_decide_retries_transient_error_then_raises_retryable(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop())
    monkeypatch.setattr("openai.AsyncOpenAI", _AlwaysFailingSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    with pytest.raises(LLMUnavailableError) as exc_info:
        await client.decide(system="x", context={}, actions=[])
    assert exc_info.value.retryable is True
    assert client._client.chat.completions.calls == 3


async def _noop():
    return None


class _FailTwiceThenSucceedCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise ConnectionError("boom")
        return type("R", (), {"choices": [
            type("C", (), {"message": type("M", (), {"content": '{"action": "CONTINUE"}'})()})
        ]})()


class _FailTwiceThenSucceedChat:
    def __init__(self):
        self.completions = _FailTwiceThenSucceedCompletions()


class _FailTwiceThenSucceedSDKClient:
    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.chat = _FailTwiceThenSucceedChat()


async def test_openai_decide_succeeds_after_transient_retries(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop())
    monkeypatch.setattr("openai.AsyncOpenAI", _FailTwiceThenSucceedSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    d = await client.decide(system="x", context={}, actions=[])
    assert d.action == "CONTINUE"
    assert client._client.chat.completions.calls == 3


async def test_openai_decide_strips_markdown_fence_before_parsing(monkeypatch):
    """Regression: google/diffusiongemma-26b-a4b-it (via NVIDIA NIM) wraps its JSON in
    a ```json ... ``` fence even under response_format=json_schema — confirmed live
    against the real NVIDIA NIM endpoint. A bare json.loads on that raw text fails with
    'Expecting value: line 1 column 1', which used to be reported as an unrecoverable
    parse failure even though the JSON itself was perfectly valid once unwrapped."""
    class _FencedCompletions:
        async def create(self, **kwargs):
            return type("R", (), {"choices": [
                type("C", (), {"message": type("M", (), {
                    "content": '```json\n{\n  "action": "CONTINUE"\n}\n```'
                })()})
            ]})()

    class _FencedSDKClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            self.chat = type("Chat", (), {"completions": _FencedCompletions()})()

    monkeypatch.setattr("openai.AsyncOpenAI", _FencedSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    d = await client.decide(system="x", context={}, actions=[])
    assert d.action == "CONTINUE"


async def test_openai_decide_empty_content_raises_instead_of_defaulting_to_continue(monkeypatch):
    """Regression: some 'thinking' models (e.g. google/diffusiongemma-26b-a4b-it via
    NVIDIA NIM) return whitespace/empty content instead of JSON. Falling back to "{}"
    would silently parse into a legitimate-looking CONTINUE decision and hide the
    failure instead of escalating it."""
    class _EmptyContentCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            return type("R", (), {"choices": [
                type("C", (), {"message": type("M", (), {"content": "   "})()})
            ]})()

    completions = _EmptyContentCompletions()

    class _EmptyContentSDKClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            self.chat = type("Chat", (), {"completions": completions})()

    monkeypatch.setattr("openai.AsyncOpenAI", _EmptyContentSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    with pytest.raises(LLMUnavailableError) as exc_info:
        await client.decide(system="x", context={}, actions=[])
    assert exc_info.value.retryable is False
    assert completions.calls == 1


async def test_openai_decide_malformed_json_raises_immediately_without_retry(monkeypatch):
    class _BadJSONCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            return type("R", (), {"choices": [
                type("C", (), {"message": type("M", (), {"content": "not json"})()})
            ]})()

    completions = _BadJSONCompletions()

    class _BadJSONSDKClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            self.chat = type("Chat", (), {"completions": completions})()

    monkeypatch.setattr("openai.AsyncOpenAI", _BadJSONSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    with pytest.raises(LLMUnavailableError) as exc_info:
        await client.decide(system="x", context={}, actions=[])
    assert exc_info.value.retryable is False
    assert completions.calls == 1


async def test_openai_client_decide_uses_json_schema_not_bare_json_object(monkeypatch):
    """vLLM-backed OpenAI-compatible endpoints (e.g. NVIDIA NIM) reject a bare
    response_format={"type": "json_object"} with a 400 demanding an explicit schema —
    decide() must always send type=json_schema with the decision schema attached."""
    captured = {}

    class _RecordingResponse:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"action": "CONTINUE"}'})()})]

    class _RecordingCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _RecordingResponse()

    class _RecordingChat:
        def __init__(self):
            self.completions = _RecordingCompletions()

    class _RecordingSDKClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            self.chat = _RecordingChat()

    monkeypatch.setattr("openai.AsyncOpenAI", _RecordingSDKClient)
    client = OpenAIClient(model="some-model", api_key="x")
    await client.decide(system="x", context={}, actions=[{"name": "CONTINUE"}])

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["schema"]["properties"]["action"]["enum"] == ["CONTINUE"]


async def test_openai_client_decide_uses_json_object_when_schema_unsupported(monkeypatch):
    """local/openrouter backends often don't implement response_format=json_schema —
    OpenAIClient(supports_json_schema=False) must fall back to json_object instead of
    sending a request shape the backend will reject."""
    captured = {}

    class _RecordingResponse:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"action": "CONTINUE"}'})()})]

    class _RecordingCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _RecordingResponse()

    class _RecordingChat:
        def __init__(self):
            self.completions = _RecordingCompletions()

    class _RecordingSDKClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            self.chat = _RecordingChat()

    monkeypatch.setattr("openai.AsyncOpenAI", _RecordingSDKClient)
    client = OpenAIClient(model="some-model", api_key="x", supports_json_schema=False)
    await client.decide(system="x", context={}, actions=[{"name": "CONTINUE"}])

    assert captured["response_format"] == {"type": "json_object"}


def test_factory_local_provider_disables_json_schema():
    client = make_llm_client("local", model="some-model", api_key="x")
    assert client._supports_json_schema is False


def test_factory_openrouter_provider_disables_json_schema(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = make_llm_client("openrouter", model="", api_key="")
    assert client._supports_json_schema is False


def test_factory_openai_provider_keeps_json_schema():
    client = make_llm_client("openai", model="gpt-4o", api_key="x")
    assert client._supports_json_schema is True
