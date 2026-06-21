"""Tests for rove/mcp_server.py helper functions.

Uses an in-memory SQLite DB with the same schema as rove/storage/db.py.
Does NOT start the actual MCP server — calls the _*_impl helpers directly.
"""

import sqlite3
from pathlib import Path

import pytest

from rove.mcp_server import (
    _find_path_impl,
    _get_page_impl,
    _list_pages_impl,
    _search_elements_impl,
)

# ---------------------------------------------------------------------------
# Schema (mirrors rove/storage/db.py exactly)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    title TEXT,
    depth INTEGER,
    crawled_at TEXT,
    fingerprint TEXT,
    parent_state TEXT,
    screenshot_path TEXT,
    priority_score INTEGER,
    UNIQUE(url, fingerprint)
);
CREATE TABLE elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER REFERENCES pages(id),
    tag TEXT,
    elem_type TEXT,
    text TEXT,
    frame_path TEXT,
    shadow_path TEXT,
    locators_json TEXT
);
CREATE TABLE links (
    from_page_id INTEGER REFERENCES pages(id),
    to_page_id   INTEGER REFERENCES pages(id),
    transition_type TEXT DEFAULT 'link',
    via_element TEXT,
    PRIMARY KEY (from_page_id, to_page_id, transition_type)
);
"""

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create a temporary SQLite DB with the rove schema."""
    path = tmp_path / "test_graph.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def _insert_page(conn: sqlite3.Connection, url: str, title: str = "T", depth: int = 0) -> int:
    cursor = conn.execute(
        "INSERT INTO pages (url, title, depth, crawled_at, fingerprint) VALUES (?,?,?,?,?)",
        (url, title, depth, "2024-01-01T00:00:00", "fp_" + url[-3:]),
    )
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# list_pages
# ---------------------------------------------------------------------------


def test_list_pages_returns_pages(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    _insert_page(conn, "https://example.com/a")
    _insert_page(conn, "https://example.com/b")
    _insert_page(conn, "https://example.com/c")
    conn.commit()
    conn.close()

    pages = _list_pages_impl(db_path)
    assert len(pages) == 3
    urls = {p["url"] for p in pages}
    assert urls == {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    }
    # Check expected fields are present
    assert "id" in pages[0]
    assert "title" in pages[0]
    assert "depth" in pages[0]
    assert "fingerprint" in pages[0]
    assert "crawled_at" in pages[0]


def test_list_pages_pagination(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    for i in range(5):
        _insert_page(conn, f"https://example.com/page{i}")
    conn.commit()
    conn.close()

    first_two = _list_pages_impl(db_path, limit=2, offset=0)
    next_two = _list_pages_impl(db_path, limit=2, offset=2)
    last_one = _list_pages_impl(db_path, limit=2, offset=4)

    assert len(first_two) == 2
    assert len(next_two) == 2
    assert len(last_one) == 1

    # IDs should be in ascending order and non-overlapping
    first_ids = {p["id"] for p in first_two}
    next_ids = {p["id"] for p in next_two}
    assert first_ids.isdisjoint(next_ids)


# ---------------------------------------------------------------------------
# get_page
# ---------------------------------------------------------------------------


def test_get_page_returns_elements_and_links(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    id_a = _insert_page(conn, "https://example.com/a")
    id_b = _insert_page(conn, "https://example.com/b")

    # Add elements to page A
    conn.execute(
        "INSERT INTO elements (page_id, tag, elem_type, text, frame_path) VALUES (?,?,?,?,?)",
        (id_a, "button", "submit", "Click me", "main"),
    )
    conn.execute(
        "INSERT INTO elements (page_id, tag, elem_type, text, frame_path) VALUES (?,?,?,?,?)",
        (id_a, "input", "text", "Search", "main"),
    )

    # Add link A -> B
    conn.execute(
        "INSERT INTO links (from_page_id, to_page_id, transition_type) VALUES (?,?,'link')",
        (id_a, id_b),
    )
    conn.commit()
    conn.close()

    result = _get_page_impl(db_path, id_a)

    assert "error" not in result
    assert result["page"]["url"] == "https://example.com/a"
    assert len(result["elements"]) == 2
    element_texts = {e["text"] for e in result["elements"]}
    assert "Click me" in element_texts
    assert "Search" in element_texts

    assert len(result["links"]) == 1
    assert result["links"][0]["to_url"] == "https://example.com/b"
    assert result["links"][0]["link_type"] == "link"


def test_get_page_not_found(db_path: Path) -> None:
    result = _get_page_impl(db_path, 9999)
    assert result == {"error": "page not found"}


# ---------------------------------------------------------------------------
# search_elements
# ---------------------------------------------------------------------------


def test_search_elements_by_text(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    page_id = _insert_page(conn, "https://example.com/")
    conn.execute(
        "INSERT INTO elements (page_id, tag, elem_type, text, frame_path) VALUES (?,?,?,?,?)",
        (page_id, "button", "submit", "Add to cart", "main"),
    )
    conn.execute(
        "INSERT INTO elements (page_id, tag, elem_type, text, frame_path) VALUES (?,?,?,?,?)",
        (page_id, "input", "text", "Search products", "main"),
    )
    conn.execute(
        "INSERT INTO elements (page_id, tag, elem_type, text, frame_path) VALUES (?,?,?,?,?)",
        (page_id, "a", "link", "View details", "main"),
    )
    conn.commit()
    conn.close()

    results = _search_elements_impl(db_path, "cart")
    assert len(results) == 1
    assert results[0]["text"] == "Add to cart"
    assert results[0]["url"] == "https://example.com/"

    # Search by tag
    results_by_tag = _search_elements_impl(db_path, "button")
    assert len(results_by_tag) == 1
    assert results_by_tag[0]["tag"] == "button"

    # Search matching multiple
    results_multi = _search_elements_impl(db_path, "e")  # matches several
    assert len(results_multi) >= 2


# ---------------------------------------------------------------------------
# find_path
# ---------------------------------------------------------------------------


def test_find_path_direct_link(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    id_a = _insert_page(conn, "https://example.com/a")
    id_b = _insert_page(conn, "https://example.com/b")
    conn.execute(
        "INSERT INTO links (from_page_id, to_page_id, transition_type) VALUES (?,?,'link')",
        (id_a, id_b),
    )
    conn.commit()
    conn.close()

    result = _find_path_impl(db_path, "https://example.com/a", "https://example.com/b")
    assert result["path"] == ["https://example.com/a", "https://example.com/b"]
    assert result["hops"] == 1


def test_find_path_multi_hop(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    id_a = _insert_page(conn, "https://example.com/a")
    id_b = _insert_page(conn, "https://example.com/b")
    id_c = _insert_page(conn, "https://example.com/c")
    conn.execute(
        "INSERT INTO links (from_page_id, to_page_id, transition_type) VALUES (?,?,'link')",
        (id_a, id_b),
    )
    conn.execute(
        "INSERT INTO links (from_page_id, to_page_id, transition_type) VALUES (?,?,'link')",
        (id_b, id_c),
    )
    conn.commit()
    conn.close()

    result = _find_path_impl(db_path, "https://example.com/a", "https://example.com/c")
    assert result["path"] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert result["hops"] == 2


def test_find_path_no_path(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    _insert_page(conn, "https://example.com/a")
    _insert_page(conn, "https://example.com/b")
    # No links inserted — no path exists
    conn.commit()
    conn.close()

    result = _find_path_impl(db_path, "https://example.com/a", "https://example.com/b")
    assert result["path"] is None
    assert "error" in result


async def test_crawl_control_tools_are_registered() -> None:
    from rove.mcp_server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "list_pages", "get_page", "search_elements", "find_path",
        "start_crawl", "get_crawl_status", "resolve_escalation",
        "review_pending_action", "stop_crawl", "list_jobs",
    }
    assert expected <= names
