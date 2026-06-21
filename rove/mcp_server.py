"""MCP server exposing read-only graph queries over the rove site graph DB."""

import sqlite3
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from rove.coordinator import DEFAULT_STAGNATION_LIMIT

mcp = FastMCP("rove")
DB_PATH = Path("output/db/site_graph.db")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Helper implementations (accept db_path so tests can pass ":memory:" paths)
# ---------------------------------------------------------------------------

def _list_pages_impl(db_path: Path, limit: int = 50, offset: int = 0) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, title, depth, fingerprint, crawled_at "
            "FROM pages ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_page_impl(db_path: Path, page_id: int) -> dict:
    with _conn(db_path) as conn:
        page_row = conn.execute(
            "SELECT * FROM pages WHERE id = ?", (page_id,)
        ).fetchone()
        if page_row is None:
            return {"error": "page not found"}

        elements = conn.execute(
            "SELECT tag, elem_type, text, frame_path FROM elements WHERE page_id = ?",
            (page_id,),
        ).fetchall()

        links = conn.execute(
            "SELECT p.url AS to_url, l.transition_type AS link_type "
            "FROM links l JOIN pages p ON l.to_page_id = p.id "
            "WHERE l.from_page_id = ?",
            (page_id,),
        ).fetchall()

    return {
        "page": dict(page_row),
        "elements": [dict(e) for e in elements],
        "links": [dict(lk) for lk in links],
    }


def _search_elements_impl(db_path: Path, query: str, limit: int = 20) -> list[dict]:
    pattern = f"%{query}%"
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT p.url, e.tag, e.elem_type, e.text, e.frame_path "
            "FROM elements e JOIN pages p ON e.page_id = p.id "
            "WHERE e.text LIKE ? OR e.tag LIKE ? "
            "LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _find_path_impl(
    db_path: Path,
    from_url: str,
    to_url: str,
    max_depth: int = 10,
) -> dict:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT p.url AS from_url, p2.url AS to_url "
            "FROM links l "
            "JOIN pages p  ON l.from_page_id = p.id "
            "JOIN pages p2 ON l.to_page_id   = p2.id",
        ).fetchall()

    adjacency: dict[str, list[str]] = {}
    for r in rows:
        adjacency.setdefault(r["from_url"], []).append(r["to_url"])

    # BFS
    queue: deque[tuple[str, list[str]]] = deque()
    queue.append((from_url, [from_url]))
    visited: set[str] = {from_url}

    while queue:
        current, path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        for neighbour in adjacency.get(current, []):
            if neighbour == to_url:
                full_path = path + [neighbour]
                return {"path": full_path, "hops": len(full_path) - 1}
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, path + [neighbour]))

    return {"path": None, "error": "no path found"}


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

@mcp.tool()
def list_pages(limit: int = 50, offset: int = 0) -> list[dict]:
    """List crawled pages with basic metadata.

    Args:
        limit: Maximum number of pages to return (default 50).
        offset: Number of pages to skip (default 0).
    """
    return _list_pages_impl(DB_PATH, limit, offset)


@mcp.tool()
def get_page(page_id: int) -> dict:
    """Return a single page with its elements and outbound links.

    Args:
        page_id: The integer primary key of the page.
    """
    return _get_page_impl(DB_PATH, page_id)


@mcp.tool()
def search_elements(query: str, limit: int = 20) -> list[dict]:
    """Full-text search over element text and tag across all pages.

    Args:
        query: Search term (LIKE match against text and tag fields).
        limit: Maximum number of results (default 20).
    """
    return _search_elements_impl(DB_PATH, query, limit)


@mcp.tool()
def find_path(from_url: str, to_url: str, max_depth: int = 10) -> dict:
    """BFS shortest path between two URLs in the link graph.

    Args:
        from_url: Starting URL.
        to_url: Target URL.
        max_depth: Maximum number of hops to explore (default 10).
    """
    return _find_path_impl(DB_PATH, from_url, to_url, max_depth)


# ---------------------------------------------------------------------------
# Crawl-control tools — start/steer a live crawl through MCP, including
# resolving login walls/CAPTCHAs and reviewing agent actions. rove.crawl is
# only imported once a crawl actually starts (inside rove.mcp_jobs), so these
# tools don't force a Playwright dependency on clients that only query an
# already-finished crawl's DB via the tools above.
# ---------------------------------------------------------------------------

@mcp.tool()
def start_crawl(
    url: str, max_pages: int = 50, depth: int = 3, concurrency: int = 2,
    master_provider: str = "none", master_model: str = "",
    master_autonomy: str = "review", no_human_in_loop: bool = False,
    ignore_robots: bool = False, export: list[str] | None = None,
    schema: str | None = None,
    headless: bool = True, wait_until: str = "domcontentloaded",
    block_resources: list[str] | None = None,
    stagnation_limit: int | None = DEFAULT_STAGNATION_LIMIT,
) -> dict:
    """Start a crawl as a background job. Returns immediately with a crawl_id —
    use get_crawl_status to follow progress and resolve_escalation /
    review_pending_action to respond when the agent needs human input.

    Args:
        url: Seed URL to crawl.
        max_pages: Page budget (default 50).
        depth: Max crawl depth (default 3) — the seed page is depth 0; pages up to and
            including depth N are crawled and have their own links followed.
        concurrency: Parallel tabs, hard-capped at 3 (default 2).
        master_provider: LLM provider for the master agent: none|anthropic|openai|local|nvidia|openrouter.
        master_model: Model id for the chosen provider.
        master_autonomy: auto|review|manual — how much the agent acts without approval.
        no_human_in_loop: Disable the master agent entirely.
        ignore_robots: Skip fetching/obeying robots.txt.
        export: Exporter name(s) to run after the crawl, e.g. ["markdown"].
        schema: Path to a JSON schema file for LLM data extraction.
        headless: Run Chromium headless (default True). Set False to watch the browser.
        wait_until: "domcontentloaded" (default, fast) or "networkidle" (slower —
            use for JS-rendered SPA nav/links that domcontentloaded misses).
        block_resources: A LIST of Playwright resource type strings to abort, e.g.
            ["image", "font", "media"] (the default if you omit this field or pass
            null/None — a bare string like "image" is invalid and raises an error).
            Pass an empty list [] (not omission) to load everything, including images.
        stagnation_limit: Stop after this many pages in a row contribute no new element
            type (default 15; pass 0 to stop as soon as the very first page fails to
            contribute a new type). Pass None to disable and crawl the whole site
            regardless of how repetitive the page templates are.
    """
    from rove.mcp_jobs import _start_job_impl
    return _start_job_impl(
        url, max_pages=max_pages, depth=depth, concurrency=concurrency,
        master_provider=master_provider, master_model=master_model,
        master_autonomy=master_autonomy, no_human_in_loop=no_human_in_loop,
        ignore_robots=ignore_robots, export=export, schema=schema,
        headless=headless, wait_until=wait_until, block_resources=block_resources,
        stagnation_limit=stagnation_limit,
    )


@mcp.tool()
def get_crawl_status(crawl_id: str) -> dict:
    """Get the live status of a crawl job: status, stats, pending questions
    (escalations/approvals awaiting a response), and recent action log.

    Args:
        crawl_id: The id returned by start_crawl.
    """
    from rove.mcp_jobs import _get_status_impl
    return _get_status_impl(crawl_id)


@mcp.tool()
def resolve_escalation(crawl_id: str, question_id: str, answer: str = "") -> dict:
    """Answer a pending escalation (login wall / CAPTCHA / terminal question).

    Args:
        crawl_id: The crawl job id.
        question_id: The pending question id from get_crawl_status.
        answer: Free-text answer, or "" to mean "I'm done" after logging in
            manually in the browser window the agent opened.
    """
    from rove.mcp_jobs import _resolve_escalation_impl
    return _resolve_escalation_impl(crawl_id, question_id, answer)


@mcp.tool()
def review_pending_action(crawl_id: str, question_id: str, decision: str = "") -> dict:
    """Approve, edit, skip, or cancel a pending agent action (when
    master_autonomy is "review" or "manual").

    Args:
        crawl_id: The crawl job id.
        question_id: The pending question id from get_crawl_status.
        decision: "" to approve, "e k=v,.." to edit params, "s" to skip,
            "c" to cancel the crawl.
    """
    from rove.mcp_jobs import _review_pending_action_impl
    return _review_pending_action_impl(crawl_id, question_id, decision)


@mcp.tool()
def stop_crawl(crawl_id: str) -> dict:
    """Stop a running crawl job and unblock any pending question so it can drain.

    Args:
        crawl_id: The crawl job id.
    """
    from rove.mcp_jobs import _stop_job_impl
    return _stop_job_impl(crawl_id)


@mcp.tool()
def list_jobs() -> list[dict]:
    """List all crawl jobs started in this MCP server's lifetime, with status
    and page count."""
    from rove.mcp_jobs import _list_jobs_impl
    return _list_jobs_impl()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
