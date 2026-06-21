import asyncio
import logging
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from rove.blocker import BlockerType, BlockerResult, detect_blocker
from rove.session import inject_session_into_context, save_session
from rove import actions as actions_mod
from rove.fingerprint import SPA_HOOK_JS
from rove.llm import LLMDecision, LLMUnavailableError
from rove.prompt_channel import PromptChannel, TerminalPromptChannel
from rove.resource_blocking import DEFAULT_BLOCKED_RESOURCES, install_resource_blocking

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None

logger = logging.getLogger(__name__)

# Cacheable actions: a selector that dismissed a cookie banner / clicked "continue as
# guest" on one page of a domain is very likely to work on other pages of that same
# domain, since these widgets are usually injected by shared site-wide JS/templates.
# FILL_FORM is excluded — its params are page-specific field values, not a reusable
# selector, so caching it would just replay stale form input on an unrelated page.
_CACHEABLE_ACTIONS = {"DISMISS_OVERLAY", "CLICK"}


class _RetryLLMFailure(Exception):
    """Internal signal: the human chose 'retry' during an LLM-failure escalation."""


def _parse_llm_failure_answer(answer: str) -> str:
    """Mirrors actions.py::human_review()'s single-letter convention (s=skip, c=cancel)
    plus a new 'retry' outcome specific to LLM-failure escalations, so the human isn't
    learning a second mini-language. Returns 'retry' | 'skip' | 'stop'."""
    a = answer.strip().lower()
    if a.startswith("r"):
        return "retry"
    if a.startswith("c") or a.startswith("stop"):
        return "stop"
    return "skip"  # 's', empty, or anything unrecognized -> safest default

# Minimal DOM snapshot — just enough fields for blocker.detect_blocker to re-judge
# whether a page is still blocked after the master acted on it. Deliberately cheaper
# than crawl.py's full EXTRACT_JS (no locators/shadow-DOM walk needed here).
_QUICK_SNAPSHOT_JS = """
() => {
    const forms = Array.from(document.querySelectorAll('form')).map(f => ({
        fields: Array.from(f.querySelectorAll('input,select,textarea')).map(el => ({
            type: el.type || el.tagName.toLowerCase(),
        })),
    }));
    return {
        forms,
        links: Array.from(document.querySelectorAll('a[href]')).map(() => 1),
        elements: Array.from(document.querySelectorAll('a,button,input,select,textarea')).map(() => 1),
    };
}
"""

# Stamps a `data-rove-idx` attribute onto currently-visible clickable elements so the
# LLM can reference one by a guaranteed-valid selector (`[data-rove-idx="N"]`) instead
# of guessing real page markup it has never seen.
_INTERACTION_SNAPSHOT_JS = """
() => {
    const els = Array.from(document.querySelectorAll(
        'button, a[href], [role="button"], [role="menuitem"], [role="tab"], input[type="submit"]'
    )).slice(0, 30);
    return els.map((el, i) => {
        el.setAttribute('data-rove-idx', String(i));
        const text = (el.innerText || el.getAttribute('aria-label') || el.value || '').trim().slice(0, 60);
        return {index: i, selector: `[data-rove-idx="${i}"]`, tag: el.tagName.toLowerCase(), text};
    });
}
"""

SYSTEM_PROMPT = (
    "You are the master agent managing an autonomous web crawler. You receive the current page "
    "and crawl statistics. Choose ONE action from the provided vocabulary to keep the crawl making "
    "progress. You may dismiss cookie/consent overlays, fill NON-PASSWORD forms (search/filter), "
    "click 'continue as guest', deprioritize unproductive URL prefixes, or stop. "
    "NEVER attempt to fill password fields or solve CAPTCHAs yourself. When the page is a real "
    "login wall or CAPTCHA you cannot pass, choose ESCALATE_HUMAN and set human_mode to "
    "'browser_login' (human logs in manually) or 'terminal_question' (you need a specific piece of "
    "info). Be conservative: escalate rather than guess credentials."
)


@dataclass
class Observation:
    url: str
    page_data: dict
    heuristic: object          # BlockerResult
    requires_pause: bool


@dataclass
class ActionLogEntry:
    url: str
    action: str
    params: dict
    reasoning: str
    result: str


class MasterAgent:
    def __init__(self, *, context, llm_client, blocker_queue, resume_event, stop_flag,
                 in_flight_ref, paused_ref, lock, deprioritize_set, stats, autonomy, playwright_instance,
                 prompt_channel: PromptChannel | None = None, blocked_types=None):
        self.context = context
        self.llm = llm_client                 # may be None → heuristic-only
        self.blocker_queue = blocker_queue
        self.resume_event = resume_event
        self.stop_flag = stop_flag
        self._in_flight = in_flight_ref        # mutable [int]
        self._paused = paused_ref              # mutable [bool], guarded by lock
        self.lock = lock
        self.deprioritize_set = deprioritize_set
        self.stats = stats
        self.autonomy = autonomy               # 'auto' | 'review' | 'manual'
        self._p = playwright_instance
        self.action_log: list[ActionLogEntry] = []
        self._auth_fails: dict[str, int] = {}
        self.prompt_channel = prompt_channel or TerminalPromptChannel()
        self.blocked_types = blocked_types if blocked_types is not None else DEFAULT_BLOCKED_RESOURCES
        # (domain, blocker_type) -> {"action": str, "params": dict} for an action that
        # previously resolved this blocker type on this domain. Reused on the next page
        # with the same (domain, blocker_type) so the LLM isn't re-consulted for what is
        # usually a site-wide cookie banner / guest-checkout widget.
        self._domain_strategy: dict[tuple[str, str], dict] = {}
        self.MAX_ACTION_ATTEMPTS = 3
        # Safety cap on live in-page interaction rescues (see handle_interaction_failure)
        # across the whole crawl, so a chaotic page can't drive unbounded LLM calls.
        self.MAX_INTERACTION_RESCUES = 20
        self._interaction_rescues = 0
        # Hard ceiling on how many state-discovery clicks decide_click_targets can ever
        # hand back, regardless of what the LLM picks — bounds a single page's click cost
        # even against an adversarial or confused response.
        self.MAX_CLICK_BUDGET = 25

    async def run(self):
        logger.info(f"Master agent started (llm={'on' if self.llm else 'heuristic-only'}, autonomy={self.autonomy})")
        while not self.stop_flag.is_set():
            try:
                obs = await asyncio.wait_for(self.blocker_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            self._drain_dupes(obs.url)
            self.blocker_queue.task_done()
            try:
                await self._handle(obs)
            except Exception as e:
                logger.error(f"[MASTER] handling {obs.url} failed: {e}")
                if obs.requires_pause and not self.stop_flag.is_set():
                    await self._resume_workers()

    async def _pause_workers(self):
        # Set the paused flag UNDER the lock so no worker can claim an in-flight slot
        # between our drain check and its own increment (both happen under the same lock).
        async with self.lock:
            self._paused[0] = True
        self.resume_event.clear()
        await self._wait_for_drain()

    async def _resume_workers(self):
        async with self.lock:
            self._paused[0] = False
        self.resume_event.set()

    def _drain_dupes(self, url):
        """Drop only queued observations for the SAME url (the dupes); re-enqueue distinct
        blockers on other URLs so they still get handled."""
        kept = []
        while not self.blocker_queue.empty():
            try:
                o = self.blocker_queue.get_nowait()
                self.blocker_queue.task_done()
            except asyncio.QueueEmpty:
                break
            if o.url != url:
                kept.append(o)
        for o in kept:
            self.blocker_queue.put_nowait(o)

    async def _handle(self, obs: Observation):
        if obs.requires_pause:
            await self._pause_workers()

        domain = urlparse(obs.url).netloc
        current = obs
        attempts = 0
        last_action = None

        while True:
            decision = await self._decide(current, domain, attempts, last_action)

            # Apply optional crawl-management adjustments the LLM attached.
            adj = decision.adjustments or {}
            for p in adj.get("deprioritize", []):
                self.deprioritize_set.add(p)
            if adj.get("stop"):
                self.stop_flag.set()

            if decision.action == "ESCALATE_HUMAN" or decision.needs_human:
                try:
                    await self._escalate(current, decision)
                except _RetryLLMFailure:
                    continue  # human said 'retry' -> re-enter the loop, call _decide() again
                break

            outcome = await self._act(current, decision)
            if outcome is None or not outcome["verifiable"]:
                # Not a DOM action (or skipped/cancelled by human review) — nothing to
                # verify, no retry loop to run.
                break

            blocker_type = current.heuristic.type
            new_type = outcome["heuristic"].type
            if new_type == BlockerType.NONE:
                # The blocker is genuinely gone. Remember the fix.
                if decision.action in _CACHEABLE_ACTIONS:
                    self._domain_strategy[(domain, blocker_type.value)] = {
                        "action": decision.action, "params": decision.params,
                    }
                break

            if new_type != blocker_type:
                # The action didn't resolve anything — a DIFFERENT blocker just took the
                # original one's place (e.g. dismissing a cookie banner reveals a login
                # wall underneath). Caching this action against the OLD blocker_type would
                # be wrong: it didn't fix that blocker, it just unmasked a new one. Drop any
                # stale cache entry and restart the attempt counter so the new blocker gets
                # its own fresh decide/act cycle instead of inheriting this one's attempts.
                self._domain_strategy.pop((domain, blocker_type.value), None)
                current = replace(current, heuristic=outcome["heuristic"])
                attempts = 0
                last_action = None
                continue

            # Still blocked by the same thing. A cached strategy that just failed isn't
            # trustworthy anymore — drop it so the next page re-consults the LLM.
            self._domain_strategy.pop((domain, blocker_type.value), None)
            attempts += 1
            last_action = decision.action
            if attempts >= self.MAX_ACTION_ATTEMPTS:
                self._log(current.url, decision,
                          f"{decision.action} did not resolve the blocker after {attempts} attempts — escalating")
                await self._escalate(current, LLMDecision(
                    "ESCALATE_HUMAN",
                    reasoning=f"{decision.action} did not resolve a {blocker_type.value} blocker after {attempts} attempts",
                    needs_human=True, human_mode="browser_login"))
                break

            current = replace(current, heuristic=outcome["heuristic"])

        if obs.requires_pause and not self.stop_flag.is_set():
            await self._resume_workers()

    async def _decide(self, obs, domain=None, attempts=0, last_action=None):
        if self.llm is None:
            # Heuristic-only: hard blocker → escalate (browser login); soft → ask terminal.
            if obs.heuristic.type in (BlockerType.LOGIN_WALL, BlockerType.CAPTCHA):
                return LLMDecision("ESCALATE_HUMAN", reasoning=obs.heuristic.reason,
                                   needs_human=True, human_mode="browser_login")
            # Soft form gates need an LLM to judge whether/how to fill. Without one we
            # just keep crawling rather than pestering the human about every search box.
            return LLMDecision("CONTINUE", reasoning=obs.heuristic.reason)

        # Only reuse a cached strategy on the first attempt — a retry means the cache
        # (if any) was already invalidated by _handle for this exact (domain, blocker).
        if attempts == 0 and domain is not None:
            cached = self._domain_strategy.get((domain, obs.heuristic.type.value))
            if cached:
                return LLMDecision(cached["action"], params=cached["params"],
                                    reasoning=f"reusing strategy that resolved this blocker earlier on {domain}")

        context = self._build_context(obs)
        if attempts:
            context["retry_info"] = {"attempts": attempts, "previous_action_failed": last_action}
        try:
            return await self.llm.decide(SYSTEM_PROMPT, context, actions_mod.ACTIONS)
        except LLMUnavailableError as e:
            logger.error(f"[MASTER] LLM decide() failed for {obs.url}: {e} (retryable={e.retryable})")
            return LLMDecision(
                "ESCALATE_HUMAN", reasoning=f"LLM failure, cannot decide: {e}",
                needs_human=True, human_mode="terminal_question",
                params={"question": (
                    f"The LLM failed to produce a decision for {obs.url} ({e}). "
                    "Reply 'retry' to ask the LLM again, 'skip' to give up on this page "
                    "and resume crawling, or 'stop' to halt the crawl."
                ), "_llm_failure": True},
            )

    def _build_context(self, obs):
        pd = obs.page_data
        # Send field NAMES/TYPES, never values; keep payload small.
        forms = [{"fields": [{"name": f.get("name"), "type": f.get("type")} for f in form.get("fields", [])]}
                 for form in pd.get("forms", [])]
        return {
            "page": {
                "url": pd.get("url"), "title": pd.get("title"),
                "n_links": len(pd.get("links", [])), "n_elements": len(pd.get("elements", [])),
                "forms": forms,
            },
            "heuristic_hint": {"type": obs.heuristic.type.value, "reason": obs.heuristic.reason},
            "crawl_stats": {"pages": self.stats.pages, "error_rate": round(self.stats.error_rate(), 2),
                            "pages_since_new_type": self.stats.pages_since_new_type},
            "recent_actions": [f"{e.action}:{e.result}" for e in self.action_log[-5:]],
        }

    async def _act(self, obs, decision):
        """Execute one action. Returns None if nothing was applied (skipped/cancelled by
        human review), or a dict {"verifiable": bool, "heuristic": BlockerResult | None}
        — "heuristic" is the freshly re-detected blocker state after a DOM-touching
        action, so _handle can tell whether the action actually resolved the blocker."""
        approved, params = await actions_mod.human_review(decision, self.autonomy, self.prompt_channel.ask)
        if params.get("_cancel"):
            self.stop_flag.set()
            return None
        if not approved:
            self._log(obs.url, decision, "skipped by human")
            return None

        # Only DOM-touching actions need a navigated page; CONTINUE / DEPRIORITIZE / STOP don't.
        if not actions_mod.needs_page(decision.action):
            result = await actions_mod.execute_action(
                decision, params, page=None,
                deprioritize_set=self.deprioritize_set, stop_flag=self.stop_flag)
            self._log(obs.url, decision, result)
            return {"verifiable": False, "heuristic": None}

        # Restrict FILL_FORM to field names that actually exist on the crawled page.
        allowed_fields = {f.get("name") for form in obs.page_data.get("forms", [])
                          for f in form.get("fields", []) if f.get("name")}
        page = await self.context.new_page()
        try:
            if stealth_async:
                await stealth_async(page)
            await page.add_init_script(SPA_HOOK_JS)
            await install_resource_blocking(page, self.blocked_types)
            await page.goto(obs.url, wait_until="domcontentloaded", timeout=30000)
            result = await actions_mod.execute_action(
                decision, params, page,
                deprioritize_set=self.deprioritize_set, stop_flag=self.stop_flag,
                allowed_fields=allowed_fields)
            heuristic = await self._verify(page)
        finally:
            await page.close()
        self._log(obs.url, decision, f"{result} (still_blocked={heuristic.type != BlockerType.NONE})")
        return {"verifiable": True, "heuristic": heuristic}

    async def _verify(self, page) -> BlockerResult:
        """Re-detect the blocker state on `page` after acting on it, so the act-observe
        loop in _handle knows whether the action actually worked."""
        try:
            snap = await page.evaluate(_QUICK_SNAPSHOT_JS)
        except Exception as e:
            logger.warning(f"[MASTER] verify snapshot failed: {e}")
            return BlockerResult(BlockerType.NONE, 0.0, "verify failed")
        snap["url"] = page.url
        snap["title"] = await page.title()
        return detect_blocker(snap)

    async def handle_interaction_failure(self, page, label: str, error_text: str) -> bool:
        """Called by crawl_page()'s state-discovery loop when a click fails on the LIVE
        page (e.g. a leftover dropdown/overlay from a previous click is intercepting
        pointer events). Unlike detect_blocker()'s persistent blockers (login wall,
        CAPTCHA — true every time you load that URL), this is a transient artifact of
        the current click sequence: there is nothing to reload-and-verify later, so we
        act directly on the SAME live page instead of routing through the normal
        Observation/blocker_queue pipeline. Returns True if an action was taken that's
        worth retrying the click after.
        """
        if self._interaction_rescues >= self.MAX_INTERACTION_RESCUES:
            return False
        self._interaction_rescues += 1

        fake_decision = LLMDecision("CONTINUE", reasoning=f"interaction failure: {label!r}: {error_text}")

        if self.llm is None:
            if self.prompt_channel.is_interactive():
                resp = await self.prompt_channel.ask(
                    f"[AGENT NEEDS INPUT] Could not click '{label}' on {page.url} ({error_text}). "
                    f"Press ENTER to skip, or type a CSS selector to click instead: ",
                    kind="escalation")
                if resp.strip():
                    try:
                        await page.locator(resp.strip()).first.click(timeout=3000, no_wait_after=True)
                        self._log(page.url, fake_decision, f"interaction-rescue: human selector {resp.strip()!r} clicked")
                        return True
                    except Exception as e:
                        self._log(page.url, fake_decision, f"interaction-rescue: human selector failed: {e}")
                        return False
            self._log(page.url, fake_decision, "interaction-rescue: heuristic-only, no LLM — skipped")
            return False

        try:
            candidates = await page.evaluate(_INTERACTION_SNAPSHOT_JS)
        except Exception:
            candidates = []

        context = {
            "page_url": page.url,
            "failed_click_label": label,
            "failed_click_error": error_text,
            "visible_clickable_elements": candidates,
            "instructions": (
                "A click failed, most likely because an overlay/dropdown left open by a "
                "previous click is covering the page. Pick an element from "
                "visible_clickable_elements (use its 'selector' field verbatim) to dismiss "
                "the overlay or click past it — e.g. DISMISS_OVERLAY/CLICK with that "
                "selector. If nothing looks like it would help, choose ESCALATE_HUMAN."
            ),
        }

        # Loop so a human's "retry" answer to an LLM-failure escalation actually re-asks
        # the LLM instead of being silently swallowed. Bounded by _escalate()'s own
        # per-URL escalation cap (self._auth_fails, max 3), which counts every escalation
        # attempt including retries — so this can't spin forever against a dead provider.
        while True:
            try:
                decision = await self.llm.decide(SYSTEM_PROMPT, context, actions_mod.ACTIONS)
            except LLMUnavailableError as e:
                logger.error(f"[MASTER] LLM decide() failed during interaction rescue on {page.url}: {e}")
                decision = LLMDecision(
                    "ESCALATE_HUMAN", reasoning=f"LLM failure during interaction rescue: {e}",
                    needs_human=True, human_mode="terminal_question",
                    params={"question": (
                        f"The LLM failed while trying to recover a failed click on {page.url} ({e}). "
                        "Reply 'retry' to ask again, 'skip' to abandon this click, or 'stop' to halt the crawl."
                    ), "_llm_failure": True},
                )

            if decision.action == "ESCALATE_HUMAN" or decision.needs_human:
                try:
                    await self._escalate(
                        Observation(url=page.url, page_data={}, heuristic=BlockerResult(BlockerType.NONE, 0.0, error_text),
                                    requires_pause=False),
                        decision,
                    )
                except _RetryLLMFailure:
                    continue  # human said 'retry' -> re-ask the LLM
                return False
            break

        approved, params = await actions_mod.human_review(decision, self.autonomy, self.prompt_channel.ask)
        if not approved:
            self._log(page.url, decision, "interaction-rescue: skipped by human")
            return False

        result = await actions_mod.execute_action(
            decision, params, page, deprioritize_set=self.deprioritize_set, stop_flag=self.stop_flag)
        self._log(page.url, decision, f"interaction-rescue: {result}")
        return "error" not in result and "not found" not in result and "missing" not in result

    async def decide_click_targets(self, url: str, candidates: list) -> list:
        """Called by crawl_page()'s state-discovery when a page has more clickable
        candidates than the default heuristic budget (crawl.DEFAULT_CLICK_CAP) — e.g. a
        row of product cards rendered as plain <button> elements. A fixed DOM-order cap
        either misses real content (cards past the cutoff) or, if raised globally, clicks
        unboundedly on every site (including risky ones like sign-up/delete/logout). The
        LLM looks at the candidate labels and picks which are worth clicking for content
        discovery. Heuristic-only (no LLM) falls back to the first DEFAULT_CLICK_CAP
        candidates, matching the old fixed-cap behavior."""
        default_indices = list(range(min(len(candidates), 10)))
        if self.llm is None:
            return default_indices

        context = {
            "page_url": url,
            "click_candidates": candidates,
            "instructions": (
                "This page has more clickable elements than the default discovery budget. "
                "Return, in params.indices, the candidate 'index' values worth clicking to "
                "discover new content/pages (e.g. product/feature cards, 'Explore'/'Learn "
                "more' links, tabs). Skip elements that perform risky or destructive actions "
                "(sign up, sign in, delete, logout, payment) or are duplicates of an already-"
                f"picked one. Return at most {self.MAX_CLICK_BUDGET} indices. Use action=CONTINUE."
            ),
        }
        try:
            decision = await self.llm.decide(SYSTEM_PROMPT, context, actions_mod.ACTIONS)
        except LLMUnavailableError as e:
            logger.warning(f"[MASTER] click-budget decision failed for {url}: {e} — using default cap")
            return default_indices

        indices = decision.params.get("indices") if isinstance(decision.params, dict) else None
        if not isinstance(indices, list):
            return default_indices
        valid = {c["index"] for c in candidates}
        clean = [i for i in indices if isinstance(i, int) and i in valid]
        return clean[:self.MAX_CLICK_BUDGET] or default_indices

    async def _escalate(self, obs, decision):
        # Human review can still veto/redirect the escalation.
        approved, params = await actions_mod.human_review(decision, self.autonomy, self.prompt_channel.ask)
        if params.get("_cancel"):
            self.stop_flag.set()
            return
        if not approved:
            self._log(obs.url, decision, "escalation skipped by human")
            return

        if not self.prompt_channel.is_interactive():
            logger.warning(f"[MASTER] {obs.url} needs human input but the prompt channel is non-interactive — skipping")
            self._log(obs.url, decision, "skipped (non-interactive)")
            return

        mode = decision.human_mode or "browser_login"
        # Guard against an escalation loop: count every escalation ATTEMPT for this URL
        # (both modes). If a blocker keeps re-triggering past the cap, stop re-asking.
        attempts = self._auth_fails.get(obs.url, 0)
        if attempts >= 3:
            logger.warning(f"[MASTER] {obs.url} escalated {attempts}x already — skipping")
            self._log(obs.url, decision, "skipped (repeat escalation)")
            return
        self._auth_fails[obs.url] = attempts + 1

        if mode == "terminal_question":
            q = params.get("question") or decision.reasoning or "Information needed to proceed:"
            answer = await self.prompt_channel.ask(f"[AGENT NEEDS INPUT] {q}\n  > ", kind="escalation")
            if params.get("_llm_failure"):
                outcome = _parse_llm_failure_answer(answer)
                self._log(obs.url, decision, f"LLM-failure escalation -> human said {outcome!r}")
                if outcome == "stop":
                    self.stop_flag.set()
                elif outcome == "retry":
                    raise _RetryLLMFailure()
                # 'skip' falls through: return normally, same as any other escalation-skip.
                return
            self._log(obs.url, decision, f"terminal answer captured ({len(answer)} chars)")
            # The answer is stored for the LLM's next turn via the action log; for forms the human
            # can also re-run with --master-autonomy review to drive FILL_FORM with these values.
            return

        # browser_login: open a headed browser at the URL, capture session, inject.
        print(f"\n{'='*60}\n[AGENT ESCALATION] Manual login required at:\n  {obs.url}\n"
              f"  reasoning: {decision.reasoning}\n"
              f"  A browser window will open — log in / solve the challenge, then come back here "
              f"and press ENTER (do NOT close the window yourself — that loses the session).\n{'='*60}\n")
        headed = None
        try:
            headed = await self._p.chromium.launch(headless=False, args=["--start-maximized"])
            hctx = await headed.new_context(no_viewport=True)
            hpage = await hctx.new_page()
            await hpage.goto(obs.url, wait_until="domcontentloaded")
            resp = await self.prompt_channel.ask(
                "Press ENTER when done (or type CANCEL to skip site): ", kind="escalation")
            if resp.strip().upper() == "CANCEL":
                self.stop_flag.set()
                self._log(obs.url, decision, "user cancelled crawl")
                return
            try:
                storage_state = await hctx.storage_state()
                await inject_session_into_context(self.context, storage_state)
                await save_session(self.context)
                self._log(obs.url, decision, "session injected")
            except Exception as e:
                # Most commonly: the user closed the browser window directly instead of
                # pressing ENTER first, so the context is already gone by the time we try
                # to read its storage state — there is no session to capture.
                logger.warning(f"[MASTER] {obs.url}: couldn't capture session after login "
                                f"(was the browser window closed before pressing ENTER?): {e}")
                self._log(obs.url, decision, f"session capture failed: {e}")
        finally:
            if headed:
                try:
                    await headed.close()
                except Exception:
                    pass

    async def _wait_for_drain(self, timeout=30.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            async with self.lock:
                if self._in_flight[0] == 0:
                    return
            if asyncio.get_event_loop().time() > deadline:
                logger.warning("[MASTER] drain timeout — proceeding")
                return
            await asyncio.sleep(0.5)

    def _log(self, url, decision, result):
        entry = ActionLogEntry(url, decision.action, decision.params, decision.reasoning, result)
        self.action_log.append(entry)
        logger.info(f"[MASTER] {decision.action} @ {url} -> {result}")

    def write_action_log(self, path="output/agent_actions.md"):
        import os
        os.makedirs("output", exist_ok=True)
        with open(path, "w") as f:
            f.write("# Master Agent Action Log\n\n")
            for e in self.action_log:
                f.write(f"- **{e.action}** @ `{e.url}`\n  - reasoning: {e.reasoning}\n"
                        f"  - params: `{e.params}`\n  - result: {e.result}\n")
