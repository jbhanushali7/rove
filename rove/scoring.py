import re
from urllib.parse import urlparse

FUNCTIONAL_PATHS = ("/login", "/signup", "/checkout", "/search", "/cart", "/account")
BOILERPLATE_PATHS = ("/blog", "/privacy", "/terms", "/about")
PAGINATION_RE = re.compile(r"(?:(?:^|[/?&-])page-|[?&]page=)(\d+)", re.IGNORECASE)


def score_url(url, depth, *, linking_page_has_forms=False,
              unique_elem_types_on_linking_page=0, max_depth=3):
    """Higher score = crawled sooner. Logged per page so the formula can be tuned."""
    path = urlparse(url).path.lower()
    score = 0
    if linking_page_has_forms:
        score += 30
    if any(p in path for p in FUNCTIONAL_PATHS):
        score += 25
    if unique_elem_types_on_linking_page >= 5:
        score += 10
    score += 5 * max(0, max_depth - depth)
    if any(p in path for p in BOILERPLATE_PATHS):
        score -= 20
    m = PAGINATION_RE.search(url.lower())
    if m and int(m.group(1)) > 3:
        score -= 15
    return score
