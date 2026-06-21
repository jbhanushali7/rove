"""Crawl coordinator: stats in, priority adjustments out.
All control decisions live in decide() so an LLM agent can later replace it.
"""
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

LOG_PATH = "output/crawl_log.md"
REPORT_EVERY = 20
DEFAULT_STAGNATION_LIMIT = 15


@dataclass
class Adjustments:
    concurrency: int | None = None
    stop: bool = False
    deprioritize_prefixes: list = field(default_factory=list)


class CrawlStats:
    def __init__(self):
        self.start = time.monotonic()
        self.pages = 0
        self.elements = 0
        self.a11y_nodes = 0
        self.queue_depth = 0
        self.recent = deque(maxlen=10)
        self.known_elem_types = set()
        self.pages_since_new_type = 0
        self.section_pages = defaultdict(int)
        # Per-prefix running totals used by decide() for deprioritization.
        # Tracked across ALL pages, not just the recent window, so a prefix
        # is correctly flagged even when multiple sections share the queue.
        self._prefix_pages = defaultdict(int)
        self._prefix_new_type_pages = defaultdict(int)

    @staticmethod
    def prefix_of(url):
        parts = urlparse(url).path.split("/")
        return "/" + parts[1] if len(parts) > 1 and parts[1] else "/"

    def record_page(self, url, *, ok, new_elem_types, n_elements=0):
        prefix = self.prefix_of(url)
        self.pages += 1
        self.elements += n_elements
        self.section_pages[prefix] += 1
        truly_new = new_elem_types - self.known_elem_types
        self.known_elem_types |= new_elem_types
        self.pages_since_new_type = 0 if truly_new else self.pages_since_new_type + 1
        self.recent.append((prefix, ok, bool(truly_new)))
        self._prefix_pages[prefix] += 1
        if truly_new:
            self._prefix_new_type_pages[prefix] += 1

    def error_rate(self):
        if not self.recent:
            return 0.0
        return sum(1 for _, ok, _ in self.recent if not ok) / len(self.recent)

    def pages_per_min(self):
        mins = (time.monotonic() - self.start) / 60
        return self.pages / mins if mins > 0 else 0.0


def decide(stats: CrawlStats, stagnation_limit: int | None = DEFAULT_STAGNATION_LIMIT) -> Adjustments:
    """THE rule engine. Replaceable by an LLM agent later.

    stagnation_limit: stop once this many pages in a row contribute no new
    element type. None disables the stagnation stop entirely (crawl the whole
    site regardless of how repetitive the page templates are). 0 is the
    strictest setting — it stops as soon as the very first page fails to
    contribute a new element type, since 0 stagnant pages are tolerated.
    """
    adj = Adjustments()
    if stats.error_rate() > 0.30:
        adj.concurrency = 1
    # Deprioritize any prefix that has been visited at least 10 times but
    # has never contributed a new element type. Uses per-prefix running totals
    # accumulated across all pages (not just the recent-10 window), so this
    # fires correctly even when multiple site sections share the queue.
    for prefix, total in stats._prefix_pages.items():
        if total >= 10 and stats._prefix_new_type_pages[prefix] == 0:
            adj.deprioritize_prefixes.append(prefix)
    if stagnation_limit is not None and stats.pages_since_new_type >= stagnation_limit:
        adj.stop = True
    return adj


def stats_snapshot(stats: CrawlStats) -> dict:
    """Plain-dict view of CrawlStats for callers that can't hold a live reference
    (e.g. an MCP job registry's progress callback)."""
    return {
        "pages": stats.pages,
        "elements": stats.elements,
        "error_rate": stats.error_rate(),
        "pages_per_min": stats.pages_per_min(),
        "queue_depth": stats.queue_depth,
        "pages_since_new_type": stats.pages_since_new_type,
    }


def write_status(stats: CrawlStats, adj: Adjustments):
    import os
    os.makedirs("output", exist_ok=True)
    sections = ", ".join(f"{p}:{n}" for p, n in sorted(stats.section_pages.items()))
    line = (
        f"- pages={stats.pages} rate={stats.pages_per_min():.1f}/min "
        f"elements={stats.elements} queue={stats.queue_depth} "
        f"errors={stats.error_rate():.0%} sections=[{sections}] "
        f"adjustments={adj}\n"
    )
    with open(LOG_PATH, "a") as f:
        f.write(line)
