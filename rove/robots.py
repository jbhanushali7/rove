import asyncio
import logging
import urllib.request
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# User-agent name to advertise when checking rules. urllib.robotparser.can_fetch
# already falls back to "*" automatically when no agent-specific rule exists, so
# a single check with our named agent is sufficient.
_AGENT = "rove"


@dataclass
class RobotsRules:
    _parser: urllib.robotparser.RobotFileParser
    crawl_delay: float | None

    def allowed(self, url: str) -> bool:
        # can_fetch("rove", url) falls back to the "*" block internally when
        # there is no "rove"-specific rule, so one call is enough.
        return self._parser.can_fetch(_AGENT, url)

    @classmethod
    def allow_all(cls) -> "RobotsRules":
        """Return a permissive instance that allows every URL and sets no delay."""
        p = urllib.robotparser.RobotFileParser()
        p.parse([])  # empty → allow all
        return cls(_parser=p, crawl_delay=None)


def _load_robots(robots_url: str) -> RobotsRules:
    try:
        with urllib.request.urlopen(robots_url, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(raw.splitlines())
        # Crawl-delay: prefer agent-specific value, fall back to wildcard.
        delay = parser.crawl_delay(_AGENT) or parser.crawl_delay("*")
        delay = float(delay) if delay is not None else None
        logger.info(f"robots.txt loaded from {robots_url} (crawl_delay={delay})")
        return RobotsRules(_parser=parser, crawl_delay=delay)
    except Exception as exc:
        logger.warning(f"Could not fetch {robots_url}: {exc!r} — treating as allow-all")
        return RobotsRules.allow_all()


async def fetch_robots(seed_url: str) -> RobotsRules:
    """Fetch and parse robots.txt for *seed_url*'s domain without blocking the event loop."""
    parsed = urllib.parse.urlparse(seed_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _load_robots, robots_url)
