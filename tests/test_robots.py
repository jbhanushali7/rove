"""Tests for rove/robots.py — robots.txt fetching and rule enforcement.

All tests use unittest.mock to avoid real network calls.
pytest-asyncio is configured with asyncio_mode=auto in pytest.ini, so no
@pytest.mark.asyncio decorator is needed.
"""

import asyncio
import urllib.robotparser
from unittest.mock import patch, MagicMock

import pytest

from rove.robots import RobotsRules, fetch_robots, _load_robots


# ---------------------------------------------------------------------------
# Helper: build a RobotsRules from raw robots.txt text
# ---------------------------------------------------------------------------

def _rules_from_text(text: str, crawl_delay: float | None = None) -> RobotsRules:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    return RobotsRules(_parser=parser, crawl_delay=crawl_delay)


# ---------------------------------------------------------------------------
# 1. allowed() returns False for a disallowed path
# ---------------------------------------------------------------------------

def test_disallowed_path_is_blocked():
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /private/\n"
    )
    rules = _rules_from_text(robots_txt)
    assert rules.allowed("https://example.com/private/secret") is False


# ---------------------------------------------------------------------------
# 2. allowed() returns True for an allowed path
# ---------------------------------------------------------------------------

def test_allowed_path_is_permitted():
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /private/\n"
    )
    rules = _rules_from_text(robots_txt)
    assert rules.allowed("https://example.com/public/page") is True


# ---------------------------------------------------------------------------
# 3. crawl_delay parsed correctly from robots.txt content
# ---------------------------------------------------------------------------

def test_crawl_delay_parsed():
    robots_txt = (
        "User-agent: *\n"
        "Crawl-delay: 5\n"
    )
    # Build parser manually and extract delay to mirror _load_robots logic.
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    delay = parser.crawl_delay("rove") or parser.crawl_delay("*")
    rules = RobotsRules(_parser=parser, crawl_delay=float(delay) if delay is not None else None)
    assert rules.crawl_delay == 5.0


def test_crawl_delay_for_specific_agent():
    """Agent-specific Crawl-delay takes precedence over wildcard."""
    robots_txt = (
        "User-agent: rove\n"
        "Crawl-delay: 2\n"
        "\n"
        "User-agent: *\n"
        "Crawl-delay: 10\n"
    )
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    # _load_robots checks "rove" first
    delay = parser.crawl_delay("rove")
    if delay is None:
        delay = parser.crawl_delay("*")
    rules = RobotsRules(_parser=parser, crawl_delay=float(delay) if delay is not None else None)
    assert rules.crawl_delay == 2.0


# ---------------------------------------------------------------------------
# 4. Fetch failure → allow-all, crawl_delay=None
# ---------------------------------------------------------------------------

def test_fetch_failure_returns_allow_all():
    """When urlopen raises, _load_robots should return a permissive RobotsRules."""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        rules = _load_robots("https://unreachable.example.com/robots.txt")

    # Must allow everything
    assert rules.allowed("https://unreachable.example.com/anything") is True
    assert rules.crawl_delay is None


def test_fetch_404_returns_allow_all():
    """A 404-like urllib.error.HTTPError is also treated as allow-all."""
    import urllib.error
    err = urllib.error.HTTPError(
        url="https://example.com/robots.txt",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    with patch("urllib.request.urlopen", side_effect=err):
        rules = _load_robots("https://example.com/robots.txt")

    assert rules.allowed("https://example.com/anything") is True
    assert rules.crawl_delay is None


# ---------------------------------------------------------------------------
# 5. fetch_robots() runs in executor and returns correct RobotsRules
# ---------------------------------------------------------------------------

async def test_fetch_robots_async_returns_rules():
    """fetch_robots() should run _load_robots in executor and return a RobotsRules."""
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /admin/\n"
        "Crawl-delay: 3\n"
    )

    def fake_urlopen(url, timeout=None):
        resp = MagicMock()
        resp.read.return_value = robots_txt.encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rules = await fetch_robots("https://example.com/")

    assert rules.allowed("https://example.com/public") is True
    assert rules.allowed("https://example.com/admin/panel") is False
    assert rules.crawl_delay == 3.0


async def test_fetch_robots_constructs_correct_url():
    """fetch_robots() must build the robots.txt URL from the seed domain."""
    captured = []

    def fake_load(robots_url: str) -> RobotsRules:
        captured.append(robots_url)
        parser = urllib.robotparser.RobotFileParser()
        parser.parse([])
        return RobotsRules(_parser=parser, crawl_delay=None)

    with patch("rove.robots._load_robots", side_effect=fake_load):
        await fetch_robots("https://shop.example.com/products/widget")

    assert captured == ["https://shop.example.com/robots.txt"]


# ---------------------------------------------------------------------------
# 6. Disallowed URL is skipped at enqueue (RobotsRules.allowed integration)
# ---------------------------------------------------------------------------

def test_disallowed_url_not_enqueued_via_allowed():
    """Simulate the enqueue guard in crawl.py: skip URLs where allowed() is False."""
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /secret/\n"
    )
    rules = _rules_from_text(robots_txt)

    candidate_links = [
        "https://example.com/home",
        "https://example.com/secret/data",
        "https://example.com/about",
    ]

    enqueued = [link for link in candidate_links if rules.allowed(link)]

    assert "https://example.com/home" in enqueued
    assert "https://example.com/about" in enqueued
    assert "https://example.com/secret/data" not in enqueued
