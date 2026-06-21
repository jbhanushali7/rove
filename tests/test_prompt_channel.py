from rove.prompt_channel import PromptChannel, TerminalPromptChannel


class FakePromptChannel(PromptChannel):
    """Deterministic test double — returns scripted answers in order, like FakeLLMClient."""

    def __init__(self, scripted=None, interactive=True):
        self._scripted = list(scripted or [])
        self._interactive = interactive
        self.asked: list[tuple[str, str]] = []

    def is_interactive(self) -> bool:
        return self._interactive

    async def ask(self, message: str, *, kind: str = "escalation") -> str:
        self.asked.append((message, kind))
        return self._scripted.pop(0) if self._scripted else ""


def test_terminal_channel_interactive_reflects_isatty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert TerminalPromptChannel().is_interactive() is True

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert TerminalPromptChannel().is_interactive() is False


async def test_terminal_channel_non_interactive_returns_empty_without_input(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    channel = TerminalPromptChannel()

    called = False

    def fail_if_called(*a, **kw):
        nonlocal called
        called = True
        return "should not happen"

    monkeypatch.setattr("builtins.input", fail_if_called)
    result = await channel.ask("question?")
    assert result == ""
    assert called is False


async def test_terminal_channel_interactive_calls_input(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    channel = TerminalPromptChannel()
    monkeypatch.setattr("builtins.input", lambda msg: "answer")
    result = await channel.ask("question?")
    assert result == "answer"


async def test_terminal_channel_eof_returns_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    channel = TerminalPromptChannel()

    def raise_eof(msg):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    result = await channel.ask("question?")
    assert result == ""


async def test_fake_channel_scripts_answers_in_order():
    channel = FakePromptChannel(scripted=["first", "second"])
    assert await channel.ask("q1") == "first"
    assert await channel.ask("q2") == "second"
    assert await channel.ask("q3") == ""
    assert channel.asked == [("q1", "escalation"), ("q2", "escalation"), ("q3", "escalation")]


async def test_fake_channel_kind_is_recorded():
    channel = FakePromptChannel(scripted=[""])
    await channel.ask("approve?", kind="approval")
    assert channel.asked == [("approve?", "approval")]
