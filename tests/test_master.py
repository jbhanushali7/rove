import asyncio
import pytest

from rove.master import MasterAgent, Observation
from rove.blocker import BlockerResult, BlockerType
from rove.coordinator import CrawlStats
from rove.llm import FakeLLMClient, LLMDecision, LLMUnavailableError
from tests.test_prompt_channel import FakePromptChannel


def make_master(*, llm_client=None, prompt_channel=None, autonomy="review"):
    return MasterAgent(
        context=None,
        llm_client=llm_client,
        blocker_queue=asyncio.Queue(),
        resume_event=asyncio.Event(),
        stop_flag=asyncio.Event(),
        in_flight_ref=[0],
        paused_ref=[False],
        lock=asyncio.Lock(),
        deprioritize_set=set(),
        stats=CrawlStats(),
        autonomy=autonomy,
        playwright_instance=None,
        prompt_channel=prompt_channel,
    )


def make_observation(url="https://x/login", requires_pause=True):
    heuristic = BlockerResult(BlockerType.LOGIN_WALL, 0.95, "login wall")
    return Observation(url=url, page_data={"url": url, "title": "Sign in", "forms": [], "links": []},
                        heuristic=heuristic, requires_pause=requires_pause)


class FakeLocator:
    def __init__(self):
        self.first = self

    async def click(self, timeout=None, no_wait_after=None):
        pass

    async def count(self):
        return 1

    async def fill(self, value, timeout=None):
        pass


class FakePage:
    """Stands in for a Playwright Page: scripted snapshots are returned from successive
    evaluate() calls (one per _verify() call after an action)."""

    def __init__(self, snapshots):
        self.url = ""
        self._snapshots = list(snapshots)

    async def route(self, pattern, handler):
        pass

    async def add_init_script(self, script):
        pass

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.url = url

    def locator(self, selector):
        return FakeLocator()

    async def evaluate(self, js):
        return self._snapshots.pop(0)

    async def title(self):
        return "OK"

    async def close(self):
        pass


class FakeContext:
    def __init__(self, pages):
        self._pages = list(pages)

    async def new_page(self):
        return self._pages.pop(0)


def test_default_prompt_channel_is_terminal_channel():
    master = make_master()
    from rove.prompt_channel import TerminalPromptChannel
    assert isinstance(master.prompt_channel, TerminalPromptChannel)


async def test_escalation_skipped_when_channel_non_interactive():
    channel = FakePromptChannel(interactive=False)
    master = make_master(autonomy="auto", prompt_channel=channel)
    obs = make_observation()
    decision = LLMDecision("ESCALATE_HUMAN", reasoning="login wall", needs_human=True, human_mode="browser_login")

    await master._escalate(obs, decision)

    assert len(master.action_log) == 1
    assert master.action_log[0].result == "skipped (non-interactive)"


async def test_terminal_question_answer_is_captured_in_action_log():
    channel = FakePromptChannel(scripted=["my answer"])
    master = make_master(autonomy="auto", prompt_channel=channel)
    obs = make_observation()
    decision = LLMDecision("ESCALATE_HUMAN", reasoning="need info", needs_human=True,
                            human_mode="terminal_question", params={"question": "What's the API key?"})

    await master._escalate(obs, decision)

    assert len(master.action_log) == 1
    assert "9 chars" in master.action_log[0].result
    # The prompt channel was asked with kind="escalation"
    assert channel.asked[0][1] == "escalation"


async def test_human_review_gate_uses_prompt_channel_with_approval_kind():
    # autonomy="manual" gates every action, including CONTINUE
    channel = FakePromptChannel(scripted=[""])  # "" == approve
    master = make_master(autonomy="manual", prompt_channel=channel)
    obs = make_observation(requires_pause=False)
    decision = LLMDecision("CONTINUE", reasoning="nothing to do")

    await master._act(obs, decision)

    assert channel.asked[0][1] == "approval"
    assert master.action_log[0].result == "continue"


async def test_human_review_cancel_sets_stop_flag():
    channel = FakePromptChannel(scripted=["c"])
    master = make_master(autonomy="manual", prompt_channel=channel)
    obs = make_observation(requires_pause=False)
    decision = LLMDecision("CONTINUE", reasoning="nothing to do")

    await master._act(obs, decision)

    assert master.stop_flag.is_set()


async def test_heuristic_only_hard_blocker_escalates_with_browser_login():
    master = make_master(llm_client=None)
    obs = make_observation()
    decision = await master._decide(obs)
    assert decision.action == "ESCALATE_HUMAN"
    assert decision.human_mode == "browser_login"


class FakeHeadedPage:
    async def goto(self, url, wait_until="domcontentloaded"):
        pass


class FakeHeadedContext:
    """storage_state() raises, simulating a window the user closed before pressing
    ENTER instead of cancelling — there is nothing left to read the session from."""

    async def new_page(self):
        return FakeHeadedPage()

    async def storage_state(self):
        raise Exception("Target page, context or browser has been closed")


class FakeHeadedBrowser:
    def __init__(self, ctx):
        self._ctx = ctx
        self.closed = False

    async def new_context(self, no_viewport=None):
        return self._ctx

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, headless=False, args=None):
        return self._browser


class FakePlaywrightInstance:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


async def test_browser_login_window_closed_before_enter_is_logged_not_raised():
    """Regression test: closing the popup window yourself (instead of pressing ENTER)
    used to raise out of _escalate uninspected, leaving action_log empty and silently
    abandoning the login — see master.py _escalate's browser_login branch."""
    headed_ctx = FakeHeadedContext()
    headed_browser = FakeHeadedBrowser(headed_ctx)
    channel = FakePromptChannel(scripted=[""])  # user presses ENTER (window already closed)
    master = make_master(autonomy="auto", prompt_channel=channel)
    master._p = FakePlaywrightInstance(headed_browser)
    obs = make_observation()
    decision = LLMDecision("ESCALATE_HUMAN", reasoning="login wall", needs_human=True,
                            human_mode="browser_login")

    await master._escalate(obs, decision)

    assert "session capture failed" in master.action_log[-1].result
    assert headed_browser.closed is True
    assert master.stop_flag.is_set() is False


async def test_successful_action_is_cached_and_reused_on_same_domain():
    click = LLMDecision("CLICK", params={"selector": "#guest"}, reasoning="continue as guest")
    llm = FakeLLMClient(scripted=[click])
    master = make_master(llm_client=llm, autonomy="auto")
    resolved_snapshot = {"forms": [], "links": [f"u{i}" for i in range(20)], "elements": [{}] * 30}
    master.context = FakeContext([FakePage([resolved_snapshot])])

    obs = make_observation(url="https://x.com/login")
    await master._handle(obs)

    assert master._domain_strategy[("x.com", "login_wall")] == {
        "action": "CLICK", "params": {"selector": "#guest"},
    }

    # A second page on the same domain/blocker type reuses the cached strategy without
    # consuming another scripted LLM decision (none are left).
    obs2 = make_observation(url="https://x.com/other-login")
    decision = await master._decide(obs2, domain="x.com")
    assert decision.action == "CLICK"
    assert decision.params == {"selector": "#guest"}
    assert "reusing strategy" in decision.reasoning


async def test_retry_loop_escalates_after_max_failed_attempts():
    click = LLMDecision("CLICK", params={"selector": "#guest"}, reasoning="try continue as guest")
    llm = FakeLLMClient(scripted=[click, click, click])
    channel = FakePromptChannel(interactive=False)
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    still_blocked = {
        "forms": [{"fields": [{"type": "password"}]}],
        "links": ["u1"],
        "elements": [{}] * 5,
    }
    master.context = FakeContext([FakePage([still_blocked]) for _ in range(3)])

    obs = make_observation(url="https://y.com/login", requires_pause=False)
    await master._handle(obs)

    still_blocked_entries = [e for e in master.action_log if "still_blocked=True" in e.result]
    assert len(still_blocked_entries) == 3
    assert master.action_log[-1].result == "skipped (non-interactive)"
    assert ("y.com", "login_wall") not in master._domain_strategy


async def test_action_that_unmasks_a_different_blocker_is_not_cached_as_a_fix():
    """A CLICK that dismisses a cookie banner but reveals a CAPTCHA underneath did not
    resolve the original login_wall blocker — it must not be cached as a fix for it, and
    the new (captcha) blocker should get its own fresh decide/act cycle."""
    class _CaptchaPage(FakePage):
        async def title(self):
            return "are you a human"

    click = LLMDecision("CLICK", params={"selector": "#guest"}, reasoning="dismiss cookie banner")
    captcha_decision = LLMDecision("ESCALATE_HUMAN", reasoning="captcha", needs_human=True,
                                    human_mode="browser_login")
    llm = FakeLLMClient(scripted=[click, captcha_decision])
    channel = FakePromptChannel(interactive=False)
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    captcha_snapshot = {"forms": [], "links": ["u1"], "elements": [{}] * 3}
    master.context = FakeContext([_CaptchaPage([captcha_snapshot])])

    obs = make_observation(url="https://z.com/login", requires_pause=False)
    await master._handle(obs)

    assert ("z.com", "login_wall") not in master._domain_strategy
    assert master.action_log[-1].result == "skipped (non-interactive)"
    # Both scripted LLM decisions were consumed: the CLICK, then a fresh decision for
    # the captcha that emerged — proving the loop restarted rather than treating the
    # blocker-type change as resolution.
    assert llm._scripted == []


async def test_repeat_escalation_guard_skips_after_three_attempts():
    channel = FakePromptChannel(scripted=["my answer"] * 5)
    master = make_master(autonomy="auto", prompt_channel=channel)
    obs = make_observation()
    decision = LLMDecision("ESCALATE_HUMAN", reasoning="need info", needs_human=True,
                            human_mode="terminal_question", params={"question": "q?"})

    for _ in range(4):
        await master._escalate(obs, decision)

    assert master.action_log[-1].result == "skipped (repeat escalation)"


async def test_interaction_failure_heuristic_only_noninteractive_skips():
    channel = FakePromptChannel(interactive=False)
    master = make_master(autonomy="auto", prompt_channel=channel)
    page = FakePage([])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert rescued is False
    assert master.action_log[-1].result == "interaction-rescue: heuristic-only, no LLM — skipped"


async def test_interaction_failure_llm_dismisses_overlay_and_returns_true():
    decision = LLMDecision("DISMISS_OVERLAY", params={"selector": '[data-rove-idx="0"]'},
                            reasoning="closing leftover dropdown")
    llm = FakeLLMClient(scripted=[decision])
    channel = FakePromptChannel(interactive=False)
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    candidates = [{"index": 0, "selector": '[data-rove-idx="0"]', "tag": "button", "text": "Close"}]
    page = FakePage([candidates])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert rescued is True
    assert "interaction-rescue: dismissed" in master.action_log[-1].result


async def test_interaction_failure_escalates_when_llm_cannot_resolve():
    decision = LLMDecision("ESCALATE_HUMAN", reasoning="no idea what's blocking this",
                            needs_human=True, human_mode="terminal_question",
                            params={"question": "what is blocking Sign In?"})
    llm = FakeLLMClient(scripted=[decision])
    channel = FakePromptChannel(interactive=False)
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    page = FakePage([[]])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert rescued is False
    assert master.action_log[-1].result == "skipped (non-interactive)"


class FailingLLMClient:
    """Always raises LLMUnavailableError from decide() — simulates a provider that's
    permanently broken (bad creds, model can't produce parseable JSON, etc.)."""
    def __init__(self):
        self.calls = 0

    async def decide(self, system, context, actions):
        self.calls += 1
        raise LLMUnavailableError("provider returned no choices", retryable=False)

    async def complete(self, system, prompt):
        raise LLMUnavailableError("provider returned no choices", retryable=False)


class FlakyLLMClient:
    """Scripted sequence of decisions/exceptions, consumed one per decide() call."""
    def __init__(self, items):
        self.calls = 0
        self._items = list(items)

    async def decide(self, system, context, actions):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_decide_converts_llm_unavailable_into_escalate_human():
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto")
    obs = make_observation()

    decision = await master._decide(obs)

    assert decision.action == "ESCALATE_HUMAN"
    assert decision.needs_human is True
    assert decision.human_mode == "terminal_question"
    assert decision.params["_llm_failure"] is True


async def test_handle_interaction_failure_converts_llm_unavailable_into_escalate():
    channel = FakePromptChannel(interactive=False)
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto", prompt_channel=channel)
    page = FakePage([[]])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert rescued is False
    assert master.action_log[-1].result == "skipped (non-interactive)"


async def test_handle_interaction_failure_retry_reasks_llm_instead_of_giving_up():
    """A human answering 'retry' to an LLM-failure escalation during interaction rescue
    must actually re-ask the LLM, not be silently treated as a skip."""
    llm = FlakyLLMClient([
        LLMUnavailableError("transient", retryable=False),
        LLMDecision("DISMISS_OVERLAY", params={"selector": '[data-rove-idx="0"]'},
                    reasoning="closing leftover dropdown"),
    ])
    channel = FakePromptChannel(scripted=["retry"])
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    candidates = [{"index": 0, "selector": '[data-rove-idx="0"]', "tag": "button", "text": "Close"}]
    page = FakePage([candidates])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert llm.calls == 2
    assert rescued is True
    assert any("human said 'retry'" in e.result for e in master.action_log)
    assert "interaction-rescue: dismissed" in master.action_log[-1].result


async def test_escalation_llm_failure_retry_reenters_decide_loop():
    llm = FlakyLLMClient([
        LLMUnavailableError("transient", retryable=False),
        LLMDecision("CONTINUE", reasoning="recovered"),
    ])
    channel = FakePromptChannel(scripted=["retry"])
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    obs = make_observation(requires_pause=False)

    await master._handle(obs)

    assert llm.calls == 2
    assert any("human said 'retry'" in e.result for e in master.action_log)


async def test_escalation_llm_failure_skip_resumes_crawl():
    channel = FakePromptChannel(scripted=["skip"])
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto", prompt_channel=channel)
    obs = make_observation(requires_pause=False)

    await master._handle(obs)

    assert master.stop_flag.is_set() is False
    assert any("human said 'skip'" in e.result for e in master.action_log)


async def test_escalation_llm_failure_stop_sets_stop_flag():
    channel = FakePromptChannel(scripted=["stop"])
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto", prompt_channel=channel)
    obs = make_observation(requires_pause=False)

    await master._handle(obs)

    assert master.stop_flag.is_set() is True
    assert any("human said 'stop'" in e.result for e in master.action_log)


async def test_escalation_llm_failure_unrecognized_answer_defaults_to_skip():
    channel = FakePromptChannel(scripted=["asdf"])
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto", prompt_channel=channel)
    obs = make_observation(requires_pause=False)

    await master._handle(obs)

    assert master.stop_flag.is_set() is False
    assert any("human said 'skip'" in e.result for e in master.action_log)


async def test_repeat_llm_failure_escalation_still_capped_at_three():
    llm = FailingLLMClient()
    channel = FakePromptChannel(scripted=["retry"] * 5)
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=channel)
    obs = make_observation(requires_pause=False)

    await master._handle(obs)

    assert master.action_log[-1].result == "skipped (repeat escalation)"
    assert len(channel.asked) == 3


async def test_interaction_failure_respects_max_rescue_cap():
    llm = FakeLLMClient(scripted=[])
    master = make_master(llm_client=llm, autonomy="auto", prompt_channel=FakePromptChannel(interactive=False))
    master.MAX_INTERACTION_RESCUES = 0
    page = FakePage([])
    page.url = "https://x.com/"

    rescued = await master.handle_interaction_failure(page, "Sign In", "Timeout")

    assert rescued is False
    assert master.action_log == []


def _candidates(n):
    return [{"index": i, "text": f"button {i}"} for i in range(n)]


async def test_decide_click_targets_heuristic_only_uses_default_cap():
    master = make_master(llm_client=None)
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == list(range(10))


async def test_decide_click_targets_llm_picks_subset():
    decision = LLMDecision("CONTINUE", params={"indices": [8, 9, 10, 11, 12, 13]}, reasoning="product cards")
    llm = FakeLLMClient(scripted=[decision])
    master = make_master(llm_client=llm, autonomy="auto")
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == [8, 9, 10, 11, 12, 13]


async def test_decide_click_targets_drops_out_of_range_indices():
    decision = LLMDecision("CONTINUE", params={"indices": [3, 99, "x", 5]}, reasoning="mixed")
    llm = FakeLLMClient(scripted=[decision])
    master = make_master(llm_client=llm, autonomy="auto")
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == [3, 5]


async def test_decide_click_targets_respects_max_click_budget():
    decision = LLMDecision("CONTINUE", params={"indices": list(range(25))}, reasoning="everything")
    llm = FakeLLMClient(scripted=[decision])
    master = make_master(llm_client=llm, autonomy="auto")
    master.MAX_CLICK_BUDGET = 5
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == list(range(5))


async def test_decide_click_targets_llm_failure_falls_back_to_default_cap():
    master = make_master(llm_client=FailingLLMClient(), autonomy="auto")
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == list(range(10))


async def test_decide_click_targets_malformed_params_falls_back_to_default_cap():
    decision = LLMDecision("CONTINUE", params={"indices": "not a list"}, reasoning="oops")
    llm = FakeLLMClient(scripted=[decision])
    master = make_master(llm_client=llm, autonomy="auto")
    indices = await master.decide_click_targets("https://x.com/", _candidates(25))
    assert indices == list(range(10))
