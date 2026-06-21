import enum
from dataclasses import dataclass


class BlockerType(enum.Enum):
    NONE = "none"
    LOGIN_WALL = "login_wall"
    CAPTCHA = "captcha"
    FORM_REQUIRED = "form_required"


@dataclass
class BlockerResult:
    type: BlockerType
    confidence: float
    reason: str

    @property
    def is_hard(self) -> bool:
        # Hard blockers pause the crawl; soft ones (forms) do not require a pause.
        return self.type in (BlockerType.LOGIN_WALL, BlockerType.CAPTCHA)


MIN_LINKS_THRESHOLD = 5
MAX_ELEMENTS_FOR_WALL = 15
LOGIN_KEYWORDS = ("sign in", "log in", "login", "authenticate", "verify")
CAPTCHA_KEYWORDS = ("captcha", "robot", "human verification", "are you a human")
LOGIN_URL_PATTERNS = ("/login", "/signin", "/auth", "/authenticate", "/account/login")
CAPTCHA_URL_PATTERNS = ("/captcha", "/challenge", "/verify")


def _has_password(forms):
    return any(any(f.get("type") == "password" for f in form.get("fields", [])) for form in forms)


def _starved(links):
    return len(links) < MIN_LINKS_THRESHOLD


def _kw(text, kws):
    t = (text or "").lower()
    return any(k in t for k in kws)


def detect_blocker(page_data: dict) -> BlockerResult:
    url = page_data.get("url", "")
    title = page_data.get("title", "")
    forms = page_data.get("forms", [])
    links = page_data.get("links", [])
    elements = page_data.get("elements", [])

    if (_kw(title, CAPTCHA_KEYWORDS) or any(p in url.lower() for p in CAPTCHA_URL_PATTERNS)) and _starved(links):
        return BlockerResult(BlockerType.CAPTCHA, 0.9, f"captcha signals: {title!r} {url!r}")

    has_pw = _has_password(forms)
    if has_pw and _starved(links) and len(elements) < MAX_ELEMENTS_FOR_WALL:
        return BlockerResult(BlockerType.LOGIN_WALL, 0.95, f"login wall: {title!r} {url!r}")
    if has_pw and (_kw(title, LOGIN_KEYWORDS) or any(p in url.lower() for p in LOGIN_URL_PATTERNS)) and _starved(links):
        return BlockerResult(BlockerType.LOGIN_WALL, 0.85, f"login wall (url/title): {url!r}")

    # A form gate is a real content gate (search/filter), not a login-looking page that
    # merely lacks a password field (those are usually nav/marketing false positives).
    looks_like_login = _kw(title, LOGIN_KEYWORDS) or any(p in url.lower() for p in LOGIN_URL_PATTERNS)
    if forms and not has_pw and not _starved(links) and not looks_like_login:
        return BlockerResult(BlockerType.FORM_REQUIRED, 0.6, f"form gate: {url!r}")

    return BlockerResult(BlockerType.NONE, 1.0, "")
