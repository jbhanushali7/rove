"""Abstraction over how the master agent asks a human for input.

The CLI path (TerminalPromptChannel) blocks on real stdin, exactly as before this
abstraction existed. An MCP-driven crawl supplies a different channel (see
rove/mcp_jobs.py::MCPPromptChannel) that surfaces the same question as a pending
MCP tool call instead of a terminal prompt — MasterAgent itself doesn't know which
kind of channel it's talking to.
"""
import asyncio
import sys
from abc import ABC, abstractmethod


class PromptChannel(ABC):
    @abstractmethod
    async def ask(self, message: str, *, kind: str = "escalation") -> str:
        """Ask a human a question and return their answer.

        kind="escalation": free-text answer, or "" to mean "I'm done" (browser login
        wait) / non-interactive default.
        kind="approval": the human_review() mini-language ("" approve, "e k=v,..",
        "s" skip, "c" cancel) — parsed entirely by actions.py, not here.
        """

    @abstractmethod
    def is_interactive(self) -> bool:
        """Whether this channel can actually produce a human answer right now."""


class TerminalPromptChannel(PromptChannel):
    """Blocks on real terminal stdin — the crawler's original CLI behavior."""

    def __init__(self):
        self._interactive = sys.stdin.isatty()

    def is_interactive(self) -> bool:
        return self._interactive

    async def ask(self, message: str, *, kind: str = "escalation") -> str:
        if not self._interactive:
            return ""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, input, message)
        except EOFError:
            return ""
