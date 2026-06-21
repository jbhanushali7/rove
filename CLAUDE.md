# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

All commands must run inside the venv. On Windows:
```
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

**Dependencies (`requirements.txt`):**

| Package | Purpose |
|---------|---------|
| `playwright` | Async browser automation — the core crawl engine |
| `pytest` + `pytest-asyncio` | Test runner; `asyncio_mode = auto` in `pytest.ini` so no `@pytest.mark.asyncio` decorators needed |
| `playwright-stealth` | Patches ~20 headless-detection signals (navigator.webdriver, plugins list, etc.) so bot-protected sites serve real content instead of CAPTCHAs |

## Common Commands

**Run a crawl:**
```
venv\Scripts\python -m rove.crawl --url <URL> --max-pages 25 --concurrency 2
```
Default concurrency is 2 (hard-coded max 3 — 2 GB RAM constraint). Never raise concurrency above 2 in production.

**Import crawl results to SQLite:**
```
venv\Scripts\python -m rove.storage.db
```
`init_db()` drops and recreates all tables on every run — the DB is derived data, safe to wipe.

**Run all tests:**
```
venv\Scripts\pytest -v
```

**Run a single test file:**
```
venv\Scripts\pytest tests\test_scoring.py -v
```

**Inspect the DB interactively:**
```
venv\Scripts\python -c "import sqlite3; c=sqlite3.connect('output/db/site_graph.db'); ..."
```

## Architecture

The system has two independent phases: **crawl** (writes JSON files) then **import** (reads JSON files into SQLite). Nothing talks to the DB during crawling.

### Crawl phase — `rove/`

`crawl.py` is the entry point and orchestrator. It runs a pool of async workers sharing one Chromium browser instance (never multiple browsers). Parallelism comes from multiple pages/tabs gated by an `asyncio.Semaphore`.

The frontier is an `asyncio.PriorityQueue` storing `(-score, seq, url, depth)` tuples. `seq` is a monotonic counter to break ties without comparing strings. Priority scores come from `scoring.py::score_url()` — higher score = crawled sooner. Scores are computed at enqueue time using metadata from the linking page (has forms? how many element types?).

Each page goes through this pipeline inside `crawl_page()`:
1. Block images/fonts/media via route interception
2. Install `SPA_HOOK_JS` as an init script (intercepts `pushState`/`replaceState`/`hashchange`)
3. Navigate, extract: meta, elements (all frames + shadow DOM via `EXTRACT_JS`), forms, internal links
4. Screenshot (JPEG, quality=60, 1280×720, after networkidle)
5. DOM fingerprint via `SKELETON_JS` → SHA-256 (`fingerprint.py`)
6. State-discovery: click candidate elements matching `[role=tab/menuitem]` / `button` (any `<button>`, not just ones inside `<nav>` — plain product/feature cards rendered as buttons with no shared role are a real-world case this needs to catch, since they have no `<a href>` either). If there are at most `DEFAULT_CLICK_CAP` (10, `crawl.py`) candidates, all are clicked. Above that, instead of an arbitrary DOM-order cutoff that can miss real content on button-heavy pages (or unboundedly clicking on every page if the cap were just raised globally), `crawl_page()` calls `click_budget_resolver(url, candidates)` — bound to `MasterAgent.decide_click_targets()` when a master agent is running — which asks the LLM to pick which candidates look like real content/navigation vs. noise or risky actions (sign up, delete, logout), capped at `MasterAgent.MAX_CLICK_BUDGET` (25) regardless of the LLM's answer. Heuristic-only (`--master-provider none`) or an `LLMUnavailableError` during this decision both fall back to the first `DEFAULT_CLICK_CAP` candidates, matching the old fixed-cap behavior. If a click causes a same-site URL change (client-side router navigation), that URL is added to the page's `links` and goes through the normal frontier/crawl/blocker-detection pipeline like any `<a href>` — it is not treated as a state. Only a click that changes the DOM fingerprint *without* changing the URL (dropdown, tab panel, dark-mode toggle, etc.) is saved as a separate state JSON
7. Write `output/pages/<md5_of_url>.json`

`EXTRACT_JS` (module-level constant in `crawl.py`) does extraction in a single `frame.evaluate()` call per frame, including recursive open-shadow-root traversal. This is intentionally one round-trip — do not split it back into per-element calls.

`coordinator.py` tracks live stats (`CrawlStats`) and every 20 pages calls `decide(stats) -> Adjustments`. `decide()` is the single function to modify when changing crawl-control logic — it's designed to be replaced by an LLM call later. It can: reduce concurrency to 1 (by permanently acquiring a semaphore permit), deprioritize URL prefixes (−50 score penalty), or set a stop flag.

Worker drain correctness: workers only exit on queue-empty timeout when `in_flight == 0`. Both `in_flight` and `pages_crawled` are mutated only inside `asyncio.Lock` to prevent race conditions.

### Import phase — `rove/storage/db.py`

Two-pass import: first pass inserts all pages and builds a `key_to_id` dict (keyed by `page_id` from JSON, plus URL as fallback). Second pass inserts elements and link edges. SPA state nodes get a `transition_type='click'` edge from their `parent_state`.

Page identity in the DB is `UNIQUE(url, fingerprint)` — the same URL with a different DOM fingerprint is a distinct state node.

### Output layout

```
output/
  pages/          # one JSON per crawled page/state, named by md5(url)
  screenshots/    # one JPEG per page/state, same filename stem as JSON
  db/
    site_graph.db # SQLite: pages, elements, links
  crawl_log.md    # appended every 20 pages by coordinator
```

### Tests

`tests/test_extraction_local.py` spins up a stdlib `http.server` to serve `tests/fixtures/` and verifies that `EXTRACT_JS` extracts elements from both an iframe and an open shadow root. `pytest.ini` sets `asyncio_mode = auto` so all async tests run without explicit markers.

## Bot-Protected and Authenticated Sites

### Why the crawler stops at 1 page on sites like Amazon

When a site serves a CAPTCHA, a "Sign in to continue" wall, or a heavily JavaScript-rendered shell to headless browsers, `crawl_page()` successfully loads the page (HTTP 200) but the DOM contains no followable `<a href>` links — so `internal_links` is empty, the queue never fills, and all workers drain and exit normally after 1 page. This is correct behaviour, not a bug.

Three signals that this is the cause:
- `Total pages: 1` despite `--max-pages 25`
- No errors in the log — the page loaded fine
- Screenshots show a CAPTCHA, a login form, or a near-empty page

### Fix 1 — Apply playwright-stealth (always do this for public sites)

`playwright-stealth` patches navigator.webdriver, the plugins list, language headers, and ~18 other fingerprints that sites use to detect headless Chrome. Install it (`playwright-stealth` is already in `requirements.txt`) and apply it to every page in `crawl_page()`:

```python
from playwright_stealth import stealth_async

# inside crawl_page(), right after page = await browser.new_page(...)
await stealth_async(page)
```

### Fix 2 — Set a realistic User-Agent and locale

The browser context in `main()` uses Playwright's default user-agent which identifies itself as headless. Override it:

```python
context = await browser.new_context(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    locale="en-IN",          # match the site's target region
    timezone_id="Asia/Kolkata",
)
# then open pages from context: page = await context.new_page()
```

### Fix 3 — Inject cookies from a real logged-in session

For sites that require login (Amazon, LinkedIn, internal tools):

1. Log in with a real browser (Chrome/Firefox).
2. Export cookies — use the "EditThisCookie" Chrome extension → Export as JSON, or DevTools → Application → Cookies → copy values.
3. Load them before crawling:

```python
await context.add_cookies([
    {
        "name": "session-id",
        "value": "YOUR_VALUE",
        "domain": ".amazon.in",
        "path": "/",
    },
    {
        "name": "ubid-acbin",
        "value": "YOUR_VALUE",
        "domain": ".amazon.in",
        "path": "/",
    },
    # repeat for all session cookies
])
```

Cookie sessions expire — re-export when crawls start returning login pages again.

### Fix 4 — Wait for JavaScript-rendered content

Some sites (React/Vue SPAs) render links only after JavaScript runs. The crawler uses `wait_until="domcontentloaded"` for speed. Switch to `networkidle` for JS-heavy pages:

```python
response = await page.goto(url, timeout=30000, wait_until="networkidle")
```

This is slower and more memory-intensive — use only when `domcontentloaded` misses links.

### Domain filtering reminder

The crawler follows links whose normalized `netloc` shares the seed URL's *apex domain*, via `crawl.py::_same_site()` — compares the last two labels of the host, so `www.amazon.in`, `amazon.in`, and any subdomain like `app.amazon.in`/`studio.amazon.in` are all treated as the *same* site (real sites are frequently inconsistent about which form their internal nav links resolve to, and commonly split a product across subdomains — e.g. login/app pages on `app.`/`studio.<domain>` while the marketing site sits on the bare/www domain). Cross-domain links (a genuinely different apex domain) are still correctly excluded. Known limitation: this is a simple last-two-labels heuristic, not a public-suffix-list lookup, so it mishandles multi-part suffixes like `co.uk` (`foo.co.uk` and `bar.co.uk` would incorrectly count as the same site).

### Crawl depth semantics

`--depth N` (default 3): the seed page is depth 0. Pages up to and including depth `N` are crawled *and have their own links followed* (`crawl.py`'s enqueue check is `if qdepth <= depth`), so a depth-`N` page's links get enqueued at depth `N+1` and are crawled too — `--depth 3` therefore reaches pages at depth 0, 1, 2, and 3, and goes one generation further than that for newly-discovered leaf pages linked only from a depth-3 page. Raise `--depth` if a site's deepest content (e.g. individual case-study/client pages linked from a paginated listing) isn't showing up.

### Resource blocking and stagnation are configurable

`--block-resources` (CLI) / `block_resources` (MCP `start_crawl`) controls which Playwright resource types get aborted — default `["image", "font", "media"]`; pass `none`/`[]` to load everything (useful for visual verification in `--headed` mode, at the cost of slower/heavier page loads). `--stagnation-limit` (CLI) / `stagnation_limit` (MCP) controls `coordinator.decide()`'s auto-stop threshold (pages in a row with no new element type) — default `15`; pass `unlimited`/`None` to disable and crawl the whole site regardless of how template-repetitive it is.

## LLM-Driven Master Agent (Human-in-the-Loop)

A master agent (`rove/master.py`) watches the crawl and manages it agentically. You connect your own LLM:

```
venv\Scripts\python -m rove.crawl --url <URL> --master-provider anthropic --master-model claude-sonnet-4-6
```

Providers: `anthropic`, `openai`, `local` (OpenAI-compatible endpoint via `LOCAL_LLM_BASE_URL`, default `http://localhost:11434/v1` for Ollama), `nvidia` (NVIDIA's hosted NIM catalog — also OpenAI-compatible — via `NVIDIA_BASE_URL`, default `https://integrate.api.nvidia.com/v1`; pick any chat-capable model in the catalog with `--master-model`, e.g. `minimaxai/minimax-m3` or `nvidia/nemotron-3-ultra-550b-a55b`), `openrouter` (OpenRouter's proxy catalog — also OpenAI-compatible — via `OPENROUTER_BASE_URL`, default `https://openrouter.ai/api/v1`; pick any model slug with `--master-model`, e.g. `anthropic/claude-sonnet-4.5` or `google/gemini-2.5-flash`), or `none` (heuristic-only, the default). API keys come from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `LOCAL_LLM_API_KEY` (optional, only for auth-gated remote Ollama-compatible endpoints) / `NVIDIA_API_KEY` / `OPENROUTER_API_KEY`. The `anthropic`/`openai` SDKs are imported lazily inside the provider constructors — only needed if you actually select that provider (`nvidia`/`local`/`openrouter` reuse the `openai` SDK pointed at a different `base_url`). Every provider's underlying SDK client is constructed with a 30s request timeout (`rove/llm.py::_REQUEST_TIMEOUT_SECONDS`) so a degraded/unresponsive endpoint fails fast into the existing retry/escalation path instead of hanging — observed live against a degraded NVIDIA NIM endpoint, which otherwise blocked a single call for ~5 minutes before erroring.

**Flow:** workers crawl into a **shared `browser.new_context()`** (so injected sessions apply to all pages). A cheap heuristic (`rove/blocker.py`) pre-filters every page; only flagged pages are sent to the LLM, which picks ONE action from `rove/actions.py`: `DISMISS_OVERLAY`, `FILL_FORM` (never password fields), `CLICK`, `DEPRIORITIZE_PREFIX`, `STOP_CRAWL`, or `ESCALATE_HUMAN`. On escalation the agent chooses `browser_login` (a visible browser opens for manual login; the session is captured with `context.storage_state()`, injected, and saved to `output/session.json`) or `terminal_question` (it asks you for a specific value).

**Stateful act-observe-retry loop:** after a DOM-touching action (`DISMISS_OVERLAY`/`CLICK`/`FILL_FORM`), `MasterAgent._act()` re-checks the page with the same heuristic (`_verify()` → `detect_blocker()` on a fresh DOM snapshot) instead of trusting the action blindly. If the blocker is still present, `_handle()` loops: re-decides (telling the LLM which action just failed via `retry_info` in the context), and tries again, up to `MasterAgent.MAX_ACTION_ATTEMPTS` (3) attempts before forcing an `ESCALATE_HUMAN`. A `DISMISS_OVERLAY`/`CLICK` that *does* resolve a blocker is cached per `(domain, blocker_type)` in `self._domain_strategy` and replayed directly — without consulting the LLM — the next time the same blocker type shows up on the same domain (typical for site-wide cookie banners / guest-checkout widgets). A cached strategy that stops working is evicted immediately so the next occurrence re-consults the LLM. `FILL_FORM` is deliberately never cached — its params are page-specific field values, not a reusable selector.

**Heuristic-only fallback** (`--master-provider none`): hard blockers (login wall / CAPTCHA) escalate straight to a manual `browser_login`; soft form gates just `CONTINUE` (no LLM to reason about them, so the human is not pestered about every search box). The heuristic path has no DOM-action vocabulary to retry with, so the act-observe-retry loop and domain-strategy cache only apply when an LLM provider is configured.

**Live in-page interaction rescue:** `detect_blocker()`'s Observation pipeline only fits *persistent* page states (true every time you load that URL — a login wall, a CAPTCHA). Some failures are transient artifacts of a single click sequence — e.g. `crawl_page()`'s state-discovery loop (`crawl.py`) clicks up to 10 nav elements per page, and a dropdown opened by an earlier click can leave a backdrop that blocks every later click; reloading the URL later wouldn't reproduce it, so routing it through the normal Observation queue would have the master "fix" a problem that's already gone. Instead, a failed click calls `MasterAgent.handle_interaction_failure(page, label, error_text)` directly on the SAME live page (not a fresh `context.new_page()`), capped at `MAX_INTERACTION_RESCUES` (20) per crawl. It stamps visible clickable elements with `data-rove-idx` (`_INTERACTION_SNAPSHOT_JS`) so the LLM can reference one by a guaranteed-valid selector, picks an action from the normal vocabulary, and reports back whether the caller should retry the click. Heuristic-only (`--master-provider none`) has no model to guess a selector with, so it just asks the human (if interactive) or skips.

**Human review of agent actions:** `--master-autonomy` = `auto` (agent acts freely) | `review` (default; write/escalate actions require your approve / `e k=v` edit / skip / cancel) | `manual` (every action gated). Every action + reasoning + result is written to `output/agent_actions.md`.

**Coordination primitives in `main()`:** `blocker_queue` (workers → master), `resume_event` (master clears to pause workers, sets to resume; starts SET), `in_flight_ref` (mutable `[int]` the master reads to drain in-flight pages before acting). The agent's `STOP_CRAWL` sets the existing `stop_flag`; `DEPRIORITIZE_PREFIX` mutates the shared `deprioritized` set consulted at enqueue time. Only DOM-touching actions (`DISMISS_OVERLAY`/`CLICK`/`FILL_FORM`) open a page; the rest don't.

**Disable for CI:** `--no-human-in-loop` skips the master entirely (no observations enqueued, no headed browser, no LLM call). The master is also automatically non-interactive-safe: if stdin is not a TTY it logs and skips escalations instead of blocking on `input()`.

**Session resume:** on startup a non-expired `output/session.json` is injected before crawling, so a prior manual login carries into the next run.

**LLM-failure escalation:** `AnthropicClient`/`OpenAIClient` (`rove/llm.py`) raise a distinct `LLMUnavailableError` — never a disguised `LLMDecision("CONTINUE", ...)` — when the provider genuinely fails to produce a usable decision (empty `choices`, unparseable JSON, no `tool_use` block). The raw SDK call is retried up to 3x with exponential backoff (`_with_retry()`) for transient failures only; a deterministic failure (bad JSON, empty choices) raises immediately with no retry, since retrying won't change a model's inability to produce parseable output. `MasterAgent._decide()` and `handle_interaction_failure()` (`rove/master.py`) both catch `LLMUnavailableError` and convert it into a synthetic `LLMDecision(action="ESCALATE_HUMAN", human_mode="terminal_question", params={"_llm_failure": True, ...})`, which flows through the existing `_escalate()` path with no new branching in `_handle()`. `_escalate()`'s `terminal_question` branch recognizes the `_llm_failure` flag and interprets the human's free-text answer via `_parse_llm_failure_answer()` (retry/skip/stop, mirroring `actions.py::human_review()`'s s/c convention) instead of just logging it: `stop` sets `stop_flag`, `retry` raises the internal `_RetryLLMFailure` sentinel which `_handle()`'s loop catches to re-call `_decide()`, and `skip`/anything unrecognized falls through like a normal escalation-skip. The existing per-URL escalation cap (`self._auth_fails`, max 3) bounds repeated "retry" answers against a permanently broken provider, so the crawl can never get stuck in this loop. `handle_interaction_failure()` treats a "retry" answer the same as "skip" (no loop to rejoin there). `MasterAgent.run()`'s outer exception handler is unaffected and remains a last-resort net for genuinely unexpected bugs unrelated to LLM failures.

## Key Invariants

- `EXTRACT_JS` in `crawl.py` must remain a single JS function string — the entire extraction (including shadow DOM recursion) happens in one `frame.evaluate()` call.
- `coordinator.decide()` stays pure (`CrawlStats` → `Adjustments`). The master applies LLM crawl-management decisions by mutating the shared `deprioritized` set and setting `stop_flag` directly — it does not add logic inside `decide()`.
- Pages are opened from the shared `context` (`context.new_page()`), never `browser.new_page()`, so injected session cookies/localStorage apply everywhere.
- `page_id` in JSON = `md5(url)` for normal pages; `md5(url + "::" + fingerprint)` for SPA states. The importer uses `page_id` as its key, not URL alone.
- `rove/storage/db.py::init_db()` always drops and recreates tables — never try to migrate the schema, just update the `CREATE TABLE` statements.
- The `decide()` function in `coordinator.py` is the only place crawl-control rules live. Keep it a pure function: `CrawlStats` in, `Adjustments` out.
