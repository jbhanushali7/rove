import asyncio
import json
import argparse
import logging
import hashlib
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from itertools import count
from typing import Callable
from urllib.parse import urlparse, urljoin, urlunparse
from playwright.async_api import async_playwright
from rove.scoring import score_url
from rove import fingerprint as fp_mod
from rove import coordinator
from rove.coordinator import CrawlStats
try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None
from rove.blocker import detect_blocker, BlockerType
from rove.master import MasterAgent, Observation
from rove.session import inject_session_into_context, load_session, save_session, session_has_expired
from rove.llm import make_llm_client
from rove.plugins import get_exporters
from rove.robots import fetch_robots, RobotsRules
from rove.prompt_channel import PromptChannel
from rove.resource_blocking import resolve_blocked_types, install_resource_blocking

def resolve_exporters(names):
    """Split requested exporter names into available classes and unknown names."""
    available = get_exporters()
    chosen, missing = {}, []
    for n in names:
        if n in available:
            chosen[n] = available[n]
        else:
            missing.append(n)
    return chosen, missing

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

EXTRACT_JS = """
() => {
  const SEL = 'a, button, input, select, textarea, [role="button"], [role="tab"], [role="menuitem"]';
  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  }
  function collect(root, shadowPath, out) {
    root.querySelectorAll(SEL).forEach(el => out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      name: el.getAttribute('name'),
      aria_label: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder'),
      role: el.getAttribute('role'),
      text: (el.innerText || el.value || '').trim().slice(0, 100),
      css: cssPath(el),
      shadow_path: shadowPath
    }));
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) {
        const hostPath = (shadowPath ? shadowPath + ' >> ' : '') + cssPath(el);
        collect(el.shadowRoot, hostPath, out);
      }
    });
  }
  const out = [];
  collect(document, null, out);
  return out;
}
"""

def _count_a11y_nodes(tree: dict | None) -> int:
    """Count total nodes in the accessibility tree recursively."""
    if not tree:
        return 0
    return 1 + sum(_count_a11y_nodes(c) for c in tree.get("children", []))


# Matches a Playwright ARIA snapshot node line: optional indent + "- " + a word
# character (role name).  The \w guard avoids false positives on bare "- "
# separators that can appear inside quoted string values.
_ARIA_NODE_LINE = re.compile(r"^\s*- \w")


def _count_aria_snapshot_nodes(snapshot: str | None) -> int:
    """Count accessibility nodes in a Playwright ARIA snapshot YAML string.

    Playwright's ``page.aria_snapshot()`` returns a YAML document where each
    node is a list item of the form ``- role "name"``.  Every such line —
    regardless of indentation depth — represents exactly one node, so we count
    lines that match ``_ARIA_NODE_LINE``.
    """
    if not snapshot:
        return 0
    return sum(1 for line in snapshot.splitlines() if _ARIA_NODE_LINE.match(line))


def _same_site(netloc_a, netloc_b):
    """Compare netlocs by apex (registrable) domain, so any subdomain of the
    seed's domain counts as the same site.

    Sites are inconsistent about which form they canonicalize to (www. vs
    bare), and commonly split a product across subdomains — e.g. a marketing
    site on the bare/www domain with login/app pages on app./studio.<domain>.
    Comparing the full netloc would treat those subdomain links as off-site
    and drop them, starving the crawl of exactly the pages worth crawling.

    This uses a simple last-two-labels heuristic and does not handle
    multi-part public suffixes (e.g. "co.uk") — "foo.co.uk" and "bar.co.uk"
    would be incorrectly treated as the same site. Acceptable for this
    project's scope; revisit with a public-suffix-list lookup if it matters.
    """
    def apex(netloc):
        host = netloc.split(":")[0]
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    return apex(netloc_a) == apex(netloc_b)


def normalize_url(url):
    parsed = urlparse(url)
    # Lowercase host, strip fragments, strip trailing slashes from path
    netloc = parsed.netloc.lower()
    path = parsed.path
    if path.endswith('/') and path != '/':
        path = path.rstrip('/')
    
    # Reconstruct without fragment
    return urlunparse((
        parsed.scheme,
        netloc,
        path,
        parsed.params,
        parsed.query,
        ''
    ))

DEFAULT_CLICK_CAP = 10

async def crawl_page(context, url, depth, semaphore, max_depth, domain, wait_until="domcontentloaded",
                      blocked_types=None, interaction_resolver=None, click_budget_resolver=None):
    """blocked_types: a frozenset of Playwright resource types to abort, or None for
    the default block list (see rove.resource_blocking.resolve_blocked_types) — this
    is re-resolved on every call so a caller that omits it (e.g. a test invoking
    crawl_page() directly) still gets the documented default blocking instead of
    silently loading everything.

    interaction_resolver: optional async (page, label, error_text) -> bool, called when
    a state-discovery click fails on the LIVE page (e.g. blocked by a leftover
    dropdown/overlay from a previous click). Bound to MasterAgent.handle_interaction_failure
    by the caller when a master agent is running; None when --no-human-in-loop. Returning
    True means it took an action worth retrying the click after.

    click_budget_resolver: optional async (url, candidates) -> list[int], called when a
    page has more state-discovery candidates than DEFAULT_CLICK_CAP (e.g. a row of
    product cards rendered as plain <button> elements with no shared nav/tab/menuitem
    role — a fixed cap misses some, clicking everything is unbounded). Bound to
    MasterAgent.decide_click_targets when a master agent is running; None when
    --no-human-in-loop, in which case the first DEFAULT_CLICK_CAP candidates are used."""
    blocked_types = resolve_blocked_types(blocked_types)
    async with semaphore:
        page = await context.new_page()
        await page.set_viewport_size({"width": 1280, "height": 720})
        try:
            if stealth_async:
                await stealth_async(page)
            await install_resource_blocking(page, blocked_types)

            await page.add_init_script(fp_mod.SPA_HOOK_JS)
            logger.info(f"Crawling {url} (depth {depth})")
            # Navigation itself always waits on the reliable domcontentloaded event —
            # passing "networkidle" straight into goto()'s hard wait_until makes the whole
            # page load fail/retry on sites with persistent background traffic (chat
            # widgets, analytics beacons) that never go fully idle. Instead, when the
            # caller wants JS-rendered content, we do a *soft* extra wait below — before
            # extraction, not after — that just proceeds with whatever's rendered on timeout.
            response = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            if not response or response.status >= 400:
                logger.warning(f"Failed to load {url}: {response.status if response else 'No response'}")
                return "PERMANENT_FAIL", []
            if wait_until == "networkidle":
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # proceed with whatever's rendered — JS-heavy sites may never go idle

            # Basic Page Info
            title = await page.title()
            page_html = await page.content()
            meta_desc = await page.evaluate(
                '() => document.querySelector(\'meta[name="description"]\')?.content || ""'
            )
            
            # Interactive Elements
            extracted_elements = []
            for frame in page.frames:
                frame_path = None if frame is page.main_frame else frame.url
                try:
                    raw = await frame.evaluate(EXTRACT_JS)
                except Exception as e:
                    logger.warning(f"Frame extract failed ({frame.url}): {e}")
                    continue
                for r in raw:
                    elem_type = ("link" if r["tag"] == "a"
                                 else "input" if r["tag"] in ("input", "select", "textarea")
                                 else "button")
                    extracted_elements.append({
                        "tag": r["tag"],
                        "type": elem_type,
                        "text": r["text"],
                        "frame_path": frame_path,
                        "shadow_path": r["shadow_path"],
                        "locators": {
                            "id": r["id"], "name": r["name"], "css": r["css"],
                            "text": r["text"], "aria_label": r["aria_label"],
                            "placeholder": r["placeholder"], "tag": r["tag"],
                        },
                    })

            # Forms
            forms = await page.locator("form").all()
            extracted_forms = []
            for form in forms:
                form_data = await form.evaluate('''el => {
                    const fields = Array.from(el.querySelectorAll("input, select, textarea")).map(f => ({
                        name: f.name || f.id,
                        type: f.type,
                        tag: f.tagName.toLowerCase()
                    }));
                    return {
                        action: el.action,
                        method: el.method,
                        fields: fields
                    }
                }''')
                extracted_forms.append(form_data)

            # Capture accessibility tree
            try:
                a11y_snapshot = await page.aria_snapshot()
            except Exception as e:
                logger.debug(f"a11y snapshot failed for {url}: {e}")
                a11y_snapshot = None

            # Internal Links
            links = await page.locator("a[href]").all()
            internal_links = []
            for link in links:
                href = await link.get_attribute("href")
                full_url = urljoin(url, href)
                norm_url = normalize_url(full_url)
                if _same_site(urlparse(norm_url).netloc, domain):
                    internal_links.append(norm_url)

            # Save JSON
            page_id = hashlib.md5(url.encode()).hexdigest()
            page_data = {
                "url": url,
                "page_id": page_id,
                "title": title,
                "depth": depth,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "meta_description": meta_desc,
                "html": page_html,
                "elements": extracted_elements,
                "forms": extracted_forms,
                "links": list(set(internal_links)),
                "screenshot_path": None,
                "a11y_tree": a11y_snapshot,
                "a11y_nodes": _count_aria_snapshot_nodes(a11y_snapshot),
            }

            # Capture screenshot after network idle
            screenshot_path = f"output/screenshots/{page_id}.jpg"
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # capture whatever is rendered
            try:
                await page.screenshot(path=screenshot_path, type="jpeg", quality=60, full_page=False)
                page_data["screenshot_path"] = screenshot_path
            except Exception as e:
                logger.warning(f"Screenshot failed for {url}: {e}")

            # Fingerprint + SPA state discovery
            fp = await fp_mod.page_fingerprint(page)
            page_data["fingerprint"] = fp
            page_data["parent_state"] = None
            try:
                page_data["spa_navigations"] = await page.evaluate("() => window.__spaNavigations || []")
            except Exception:
                page_data["spa_navigations"] = []

            discovered_states = []
            try:
                # Plain "button" (not just nav-scoped or role=tab/menuitem) catches
                # client-side-routed content like product cards rendered as <button>
                # with no shared nav/ARIA role — common on marketing sites, invisible
                # to <a href> extraction since there's no href at all.
                nav_locator = page.locator("[role='tab'], [role='menuitem'], button")
                total_count = await nav_locator.count()
                if total_count <= DEFAULT_CLICK_CAP:
                    click_indices = list(range(total_count))
                else:
                    # More candidates than the default budget — let the master agent
                    # judge which look like real content/navigation vs. noise or risky
                    # actions (sign up, delete, logout) instead of an arbitrary DOM-order
                    # cutoff that can miss real pages on button-heavy sites.
                    label_cap = min(total_count, 50)
                    labels = await nav_locator.all_inner_texts()
                    candidates = [{"index": idx, "text": labels[idx][:60] if idx < len(labels) else ""}
                                  for idx in range(label_cap)]
                    if click_budget_resolver is not None:
                        click_indices = await click_budget_resolver(url, candidates)
                    else:
                        click_indices = list(range(min(total_count, DEFAULT_CLICK_CAP)))
                seen_fps = {fp}
                for i in click_indices:
                    el = nav_locator.nth(i)
                    try:
                        label = (await el.inner_text(timeout=1000)).strip()[:60]
                        # Some CTAs (target="_blank", window.open) spawn a new tab instead of
                        # navigating in place. Unhandled, that tab is invisible to the
                        # page.url checks below and never gets closed — it accumulates as an
                        # extra open tab (visible in --headed) and does a full redundant page
                        # load even if the URL was already crawled. Registered on `page`
                        # (not `context`) so a popup opened by a DIFFERENT worker's page —
                        # concurrent workers share one browser context — is never captured
                        # here; Playwright's page-scoped "popup" event only fires for popups
                        # opened from this exact page.
                        popup_box = []
                        def _capture_popup(p):
                            popup_box.append(p)
                        page.on("popup", _capture_popup)
                        try:
                            try:
                                await el.click(timeout=3000, no_wait_after=True)
                            except Exception as click_err:
                                # A dropdown/menu opened by a previous iteration's click can
                                # leave a full-viewport backdrop in place that intercepts this
                                # click. Try a neutral dismiss click first (most dropdown
                                # backdrops close-on-outside-click) and retry once before
                                # escalating to the live master agent — only on an actual
                                # failure, not unconditionally on every iteration, since a
                                # blind click at a fixed coordinate can itself hit real
                                # content (a sidebar link, a sticky banner button) on pages
                                # where nothing was actually blocking the click.
                                try:
                                    await page.mouse.click(5, 400)
                                    await page.wait_for_timeout(200)
                                    await el.click(timeout=3000, no_wait_after=True)
                                except Exception:
                                    if interaction_resolver is not None:
                                        rescued = await interaction_resolver(page, label, str(click_err))
                                        if rescued:
                                            await el.click(timeout=3000, no_wait_after=True)
                                        else:
                                            raise
                                    else:
                                        raise
                            await page.wait_for_timeout(500)
                        finally:
                            page.remove_listener("popup", _capture_popup)
                        if popup_box:
                            # Process every popup this iteration produced (a failed first
                            # click whose handler still fired window.open before throwing,
                            # followed by a successful retry, can produce two) — closing
                            # only the first would leak the rest as open tabs.
                            for popup in popup_box:
                                try:
                                    await popup.wait_for_load_state("domcontentloaded", timeout=5000)
                                    popup_netloc = urlparse(popup.url).netloc
                                    if _same_site(popup_netloc, domain):
                                        internal_links.append(normalize_url(popup.url))
                                except Exception:
                                    pass
                                finally:
                                    await popup.close()
                            continue
                        current_netloc = urlparse(page.url).netloc
                        if not _same_site(current_netloc, domain):
                            await page.go_back(timeout=5000)
                            continue
                        norm_clicked_url = normalize_url(page.url)
                        if norm_clicked_url != normalize_url(url):
                            # Button caused a real same-site navigation (client-side
                            # router, not just a dropdown/tab toggle) — treat it like
                            # a discovered link so it gets fully crawled (elements,
                            # forms, blocker detection) instead of recorded as an
                            # empty state stub.
                            internal_links.append(norm_clicked_url)
                            try:
                                await page.go_back(timeout=5000)
                            except Exception:
                                pass
                            continue
                        new_fp = await fp_mod.page_fingerprint(page)
                        if new_fp not in seen_fps:
                            seen_fps.add(new_fp)
                            state_id = hashlib.md5(f"{page.url}::{new_fp}".encode()).hexdigest()
                            sp = f"output/screenshots/{state_id}.jpg"
                            try:
                                await page.screenshot(path=sp, type="jpeg", quality=60, full_page=False)
                            except Exception:
                                sp = None
                            state_data = {
                                "page_id": state_id,
                                "url": page.url,
                                "title": await page.title(),
                                "depth": depth,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "fingerprint": new_fp,
                                "parent_state": page_id,
                                "transition": {"type": "click", "via_element": label},
                                "screenshot_path": sp,
                                "elements": [],
                                "forms": [],
                                "links": [],
                                "priority_score": None,
                                "spa_navigations": [],
                                "discovered_states": [],
                            }
                            with open(f"output/pages/{state_id}.json", "w") as f:
                                json.dump(state_data, f, indent=2)
                            discovered_states.append(state_id)
                    except Exception as e:
                        logger.debug(f"State-discovery click {i} failed on {url}: {e}")
            except Exception as e:
                logger.debug(f"State discovery failed on {url}: {e}")

            page_data["discovered_states"] = discovered_states
            # internal_links gains click-discovered URLs (line ~378) after page_data["links"]
            # was already snapshotted (line ~287) — refresh it here so the persisted JSON
            # reflects every same-site URL found on this page, not just <a href> ones.
            page_data["links"] = list(set(internal_links))

            # JSON is written by the caller (worker) after priority_score is set.
            return page_data, internal_links

        except Exception as e:
            logger.error(f"Error crawling {url}: {e}")
            return None, []
        finally:
            await page.close()

async def run_crawl(
    url: str, *, max_pages: int = 50, depth: int = 3, concurrency: int = 2,
    master_provider: str = "none", master_model: str = "",
    master_autonomy: str = "review", no_human_in_loop: bool = False,
    ignore_robots: bool = False, export: list[str] | None = None,
    schema: str | None = None,
    headless: bool = True, wait_until: str = "domcontentloaded",
    block_resources: list[str] | None = None,
    stagnation_limit: int | None = coordinator.DEFAULT_STAGNATION_LIMIT,
    prompt_channel: PromptChannel | None = None,
    stop_flag: asyncio.Event | None = None,
    progress_cb: Callable[[CrawlStats], None] | None = None,
) -> dict:
    """Run a full crawl end-to-end. Returns {"pages_crawled": int, "stats": dict,
    "stop_reason": str | None}.

    This is the programmatic entry point used by both the CLI (`main()`, a thin
    argparse wrapper around this function) and MCP-triggered crawls
    (`rove/mcp_jobs.py`). MCP-triggered crawls supply their own `prompt_channel` to
    route escalations/approvals over MCP instead of the terminal, their own
    `stop_flag` so a `stop_crawl` MCP tool can halt this coroutine from the outside,
    and `progress_cb` to keep a live status snapshot without re-parsing
    `output/crawl_log.md`.
    """
    concurrency = min(concurrency, 3)
    export = export or []
    blocked_types = resolve_blocked_types(block_resources)

    os.makedirs("output/pages", exist_ok=True)
    os.makedirs("output/db", exist_ok=True)
    os.makedirs("output/screenshots", exist_ok=True)

    start_url = normalize_url(url)
    domain = urlparse(start_url).netloc

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-US", timezone_id="America/New_York",
        )
        prior = load_session()
        if prior and not session_has_expired(prior):
            await inject_session_into_context(context, prior)
            logger.info("Loaded prior session from output/session.json")

        # Fetch robots.txt once at startup (before any page is crawled).
        if ignore_robots:
            rules = RobotsRules.allow_all()
            logger.info("ignore_robots set: skipping robots.txt fetch")
        else:
            rules = await fetch_robots(start_url)

        # One lock shared by all workers: only one worker sleeps for crawl_delay
        # at a time, so they stagger rather than all sleeping simultaneously.
        crawl_delay_lock = asyncio.Lock()

        queue = asyncio.PriorityQueue()
        _seq = count()
        initial_score = score_url(start_url, 0, max_depth=depth)
        await queue.put((-initial_score, next(_seq), start_url, 0))

        visited = set()
        visited.add(start_url)

        semaphore = asyncio.Semaphore(concurrency)
        pages_crawled = 0
        in_flight_ref = [0]                       # mutable so MasterAgent can read it
        lock = asyncio.Lock()

        stats = CrawlStats()
        deprioritized = set()
        stop_flag = stop_flag or asyncio.Event()
        reduced = False

        blocker_queue = asyncio.Queue()
        resume_event = asyncio.Event()
        resume_event.set()                        # starts set = workers run freely
        paused = [False]                          # mutable, guarded by `lock`
        llm_client = make_llm_client(master_provider, master_model)

        # Workers
        async def worker():
            nonlocal pages_crawled, reduced, deprioritized
            while True:
                try:
                    neg_score, _s, qurl, qdepth = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    async with lock:
                        if in_flight_ref[0] == 0:
                            return
                    continue

                if stop_flag.is_set():
                    queue.task_done()
                    return

                # Atomically wait out any master-initiated pause AND claim an in-flight slot:
                # both the paused-check and the in_flight increment happen under `lock`, the
                # same lock the master holds when it sets `paused` and drains. This closes the
                # race where a worker slips past the gate while the master thinks it has drained.
                while True:
                    async with lock:
                        if pages_crawled >= max_pages:
                            queue.task_done()
                            return
                        if not paused[0]:
                            in_flight_ref[0] += 1
                            break
                    if stop_flag.is_set():
                        queue.task_done()
                        return
                    await resume_event.wait()

                try:
                    result = None
                    internal_links = []
                    for attempt in range(3):
                        result, internal_links = await crawl_page(
                            context, qurl, qdepth, semaphore, depth, domain, wait_until, blocked_types,
                            interaction_resolver=master.handle_interaction_failure if master else None,
                            click_budget_resolver=master.decide_click_targets if master else None,
                        )
                        if result == "PERMANENT_FAIL":
                            result = None
                            break
                        if result:
                            break
                        logger.info(f"Retry {attempt+1} for {qurl}")
                        await asyncio.sleep(1)

                    if result:
                        async with lock:
                            pages_crawled += 1
                        result["priority_score"] = -neg_score
                        # Write JSON here (not inside crawl_page) so priority_score
                        # is included in the file and lands correctly in the DB.
                        with open(f"output/pages/{result['page_id']}.json", "w") as f:
                            json.dump(result, f, indent=2)
                        logger.info(f"CRAWLED score={-neg_score} depth={qdepth} url={qurl}")
                        if qdepth <= depth:
                            has_forms = bool(result.get("forms"))
                            uniq_types = len({e["tag"] for e in result.get("elements", [])})
                            for link in internal_links:
                                if link not in visited:
                                    if not rules.allowed(link):
                                        logger.debug(f"robots.txt disallows {link} — skipping")
                                        continue
                                    visited.add(link)
                                    s = score_url(
                                        link, qdepth + 1,
                                        linking_page_has_forms=has_forms,
                                        unique_elem_types_on_linking_page=uniq_types,
                                        max_depth=depth,
                                    )
                                    if stats.prefix_of(link) in deprioritized:
                                        s -= 50
                                    await queue.put((-s, next(_seq), link, qdepth + 1))

                        # Cheap heuristic pre-filter; flag blockers to the master agent.
                        if not no_human_in_loop:
                            hint = detect_blocker(result)
                            if hint.type != BlockerType.NONE:
                                blocker_queue.put_nowait(Observation(
                                    url=qurl, page_data=result, heuristic=hint,
                                    requires_pause=hint.is_hard))
                                logger.info(f"Worker signalled {hint.type.value} at {qurl} (pause={hint.is_hard})")

                    # Coordinator tracking — result is None on failure, dict on success.
                    if result:
                        stats.a11y_nodes += result.get("a11y_nodes", 0)
                    new_types = {e["tag"] for e in result.get("elements", [])} if result else set()
                    stats.record_page(
                        qurl,
                        ok=bool(result),
                        new_elem_types=new_types,
                        n_elements=len(result.get("elements", [])) if result else 0,
                    )
                    stats.queue_depth = queue.qsize()

                    if progress_cb:
                        progress_cb(stats)

                    if stats.pages > 0 and stats.pages % coordinator.REPORT_EVERY == 0:
                        adj = coordinator.decide(stats, stagnation_limit)
                        coordinator.write_status(stats, adj)
                        if adj.concurrency == 1 and not reduced:
                            # Consume one semaphore slot to cap active pages at
                            # concurrency-1. Release it when the crawl finishes
                            # (in the outer scope after gather) so the slot isn't
                            # leaked if this function is called again.
                            await semaphore.acquire()
                            reduced = True
                        deprioritized |= set(adj.deprioritize_prefixes)
                        if adj.stop:
                            stop_flag.set()

                    # Polite rate limiting: honour robots.txt Crawl-delay.
                    # Use a lock so only ONE worker sleeps at a time; others
                    # proceed immediately, which staggers requests naturally.
                    if rules.crawl_delay is not None:
                        async with crawl_delay_lock:
                            await asyncio.sleep(rules.crawl_delay)
                finally:
                    async with lock:
                        in_flight_ref[0] -= 1
                    queue.task_done()

        # Master agent runs alongside the workers (LLM-driven or heuristic-only).
        master = None
        if not no_human_in_loop:
            master = MasterAgent(
                context=context, llm_client=llm_client, blocker_queue=blocker_queue,
                resume_event=resume_event, stop_flag=stop_flag, in_flight_ref=in_flight_ref,
                paused_ref=paused, lock=lock, deprioritize_set=deprioritized, stats=stats,
                autonomy=master_autonomy, playwright_instance=p,
                prompt_channel=prompt_channel, blocked_types=blocked_types,
            )

        # Start workers
        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        master_task = asyncio.create_task(master.run()) if master else None
        await asyncio.gather(*workers)
        if master_task:
            master_task.cancel()
            try:
                await master_task
            except asyncio.CancelledError:
                pass
            master.write_action_log()

        # Return the semaphore slot consumed by the coordinator's concurrency
        # reduction so the semaphore is back to its original value and won't
        # leak if the browser/context is reused.
        if reduced:
            semaphore.release()

        try:
            await save_session(context)
        except Exception as e:
            logger.warning(f"Could not save session: {e}")
        await context.close()
        await browser.close()

        # Shared helpers for the post-crawl blocks below.
        def _load_page_jsons():
            import glob as _glob
            return [Path(f).read_text(encoding="utf-8") for f in _glob.glob("output/pages/*.json")]

        if export:
            chosen, missing = resolve_exporters(export)
            for name in missing:
                logger.warning(f"Exporter '{name}' is not available. Available exporters: {', '.join(get_exporters())}")
            if chosen:
                from rove.exporters.base import CrawlResult
                pages = [json.loads(t) for t in _load_page_jsons()]
                result = CrawlResult(pages=pages)
                for name, cls in chosen.items():
                    path = cls().export(result, Path("output"))
                    logger.info(f"Exported '{name}' -> {path}")

        if schema and llm_client:
            schema_path = Path(schema)
            if schema_path.exists():
                from rove.schema_extractor import extract_schema
                schema_dict = json.loads(schema_path.read_text(encoding="utf-8"))
                pages = [json.loads(t) for t in _load_page_jsons()]
                logger.info(f"Running schema extraction on {len(pages)} pages")
                enriched = await extract_schema(pages, schema_dict, llm_client)
                for p in enriched:
                    pid = p.get("page_id")
                    if pid:
                        out_path = Path("output/pages") / f"{pid}.json"
                        out_path.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info(f"Schema extraction complete for {len(enriched)} pages")
            else:
                logger.warning(f"Schema file not found: {schema}")
        elif schema and not llm_client:
            logger.warning("schema extraction requires a master_provider (LLM not configured)")

        logger.info(f"Crawling complete. Total pages: {pages_crawled}")

        return {
            "pages_crawled": pages_crawled,
            "stats": coordinator.stats_snapshot(stats),
            "stop_reason": "stopped" if stop_flag.is_set() else None,
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--master-provider", default="none", choices=["none", "anthropic", "openai", "local", "nvidia", "openrouter"])
    parser.add_argument("--master-model", default="")
    parser.add_argument("--master-autonomy", default="review", choices=["auto", "review", "manual"])
    parser.add_argument("--no-human-in-loop", action="store_true", help="Disable master agent entirely (CI).")
    parser.add_argument("--ignore-robots", action="store_true", help="Ignore robots.txt rules.")
    parser.add_argument("--export", action="append", default=[],
                        help="Exporter(s) to run after the crawl, e.g. --export markdown")
    parser.add_argument("--schema", metavar="FILE",
                        help="JSON schema file for LLM extraction (requires --master-provider)")
    parser.add_argument("--headed", action="store_true",
                        help="Show the browser window instead of running headless.")
    parser.add_argument("--wait-until", default="domcontentloaded",
                        choices=["domcontentloaded", "networkidle"],
                        help="Page-load wait strategy. Use networkidle for JS-rendered "
                             "nav/links on SPAs (slower, more memory).")
    parser.add_argument("--block-resources", default="image,font,media",
                        help="Comma-separated Playwright resource types to abort "
                             "(default: image,font,media). Pass 'none' to disable blocking "
                             "and load everything.")
    parser.add_argument("--stagnation-limit", default=str(coordinator.DEFAULT_STAGNATION_LIMIT),
                        help=f"Stop the crawl after this many pages in a row contribute no "
                             f"new element type (default: {coordinator.DEFAULT_STAGNATION_LIMIT}). "
                             "Pass 'unlimited' to disable and crawl the whole site regardless of "
                             "page-template repetition.")
    args = parser.parse_args()

    block_resources = [] if args.block_resources.strip().lower() == "none" \
        else [t.strip() for t in args.block_resources.split(",") if t.strip()]

    if args.stagnation_limit.strip().lower() == "unlimited":
        stagnation_limit = None
    else:
        try:
            stagnation_limit = int(args.stagnation_limit)
        except ValueError:
            parser.error(
                f"--stagnation-limit must be an integer or 'unlimited', got {args.stagnation_limit!r}"
            )

    await run_crawl(
        args.url, max_pages=args.max_pages, depth=args.depth, concurrency=args.concurrency,
        master_provider=args.master_provider, master_model=args.master_model,
        master_autonomy=args.master_autonomy, no_human_in_loop=args.no_human_in_loop,
        ignore_robots=args.ignore_robots, export=args.export, schema=args.schema,
        headless=not args.headed, wait_until=args.wait_until,
        block_resources=block_resources, stagnation_limit=stagnation_limit,
    )

def main_sync():
    """Console-script entry point (`rove ...` / `python -m rove.crawl`)."""
    asyncio.run(main())

if __name__ == "__main__":
    main_sync()
