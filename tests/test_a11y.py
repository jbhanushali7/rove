import asyncio
import functools
import http.server
import socketserver
import threading
import pytest
from playwright.async_api import async_playwright
from rove.crawl import _count_a11y_nodes, _count_aria_snapshot_nodes, crawl_page


# --- Unit tests for _count_a11y_nodes (dict-based tree) ---

def test_none_tree_returns_zero():
    assert _count_a11y_nodes(None) == 0


def test_single_node_returns_one():
    assert _count_a11y_nodes({"role": "button", "name": "Click me"}) == 1


def test_nested_tree_counts_all_nodes():
    tree = {
        "role": "document",
        "children": [
            {"role": "heading", "name": "Title"},
            {"role": "list", "children": [
                {"role": "listitem", "name": "A"},
                {"role": "listitem", "name": "B"},
            ]},
        ]
    }
    assert _count_a11y_nodes(tree) == 5  # root + heading + list + 2 listitems


def test_empty_children_list():
    assert _count_a11y_nodes({"role": "img", "children": []}) == 1


# --- Unit tests for _count_aria_snapshot_nodes (ARIA YAML string) ---

def test_aria_snapshot_none_returns_zero():
    assert _count_aria_snapshot_nodes(None) == 0


def test_aria_snapshot_empty_string_returns_zero():
    assert _count_aria_snapshot_nodes("") == 0


def test_aria_snapshot_single_node():
    # Minimal Playwright ARIA snapshot with one node
    snapshot = "- document\n"
    assert _count_aria_snapshot_nodes(snapshot) == 1


def test_aria_snapshot_nested_nodes():
    # Simulated snapshot: document > heading + button
    snapshot = (
        "- document:\n"
        "  - heading \"Hello\" [level=1]\n"
        "  - button \"Click me\"\n"
    )
    assert _count_aria_snapshot_nodes(snapshot) == 3


def test_aria_snapshot_bare_dash_not_counted():
    # A line with just "- " (no word char after) should not be counted
    snapshot = "- document:\n  - \n  - button \"OK\"\n"
    assert _count_aria_snapshot_nodes(snapshot) == 2


# --- Integration test using a local HTTP server ---

@pytest.fixture(scope="module")
def a11y_fixture_server():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory="tests/fixtures"
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()


@pytest.mark.asyncio
async def test_crawl_page_captures_a11y_tree(a11y_fixture_server):
    url = f"{a11y_fixture_server}/a11y_test.html"
    import asyncio
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        semaphore = asyncio.Semaphore(1)
        result, _ = await crawl_page(
            context, url, depth=0, semaphore=semaphore,
            max_depth=1, domain="127.0.0.1"
        )
        await context.close()
        await browser.close()

    assert result is not None, "crawl_page returned None"
    assert "a11y_tree" in result, "a11y_tree key missing from page_data"
    assert "a11y_nodes" in result, "a11y_nodes key missing from page_data"
    assert result["a11y_tree"] is not None, "a11y_tree should not be None for a real page"
    assert result["a11y_tree"] != "", "a11y_tree should not be empty for a real page"
    assert result["a11y_nodes"] > 0, "a11y_nodes should be > 0 for a page with content"
