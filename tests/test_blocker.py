import pytest
from rove.blocker import detect_blocker, BlockerType


def _page(forms=None, links=None, title="", url="https://example.com/page", elements=None):
    return {"forms": forms or [], "links": links or [], "title": title, "url": url, "elements": elements or []}


def _pw_form(*types):
    return {"fields": [{"type": t, "name": "f", "tag": "input"} for t in types]}


def test_normal_page_is_none():
    assert detect_blocker(_page(links=[f"u{i}" for i in range(20)], elements=[{}] * 30)).type == BlockerType.NONE


def test_login_wall():
    r = detect_blocker(_page(forms=[_pw_form("text", "password")], links=["u"], title="Sign in",
                             url="https://x/login", elements=[{}] * 8))
    assert r.type == BlockerType.LOGIN_WALL


def test_login_link_in_nav_is_not_wall():
    r = detect_blocker(_page(forms=[_pw_form("text", "email")], links=[f"u{i}" for i in range(15)],
                             title="Sign in", url="https://x/login", elements=[{}] * 30))
    assert r.type == BlockerType.NONE


def test_captcha():
    r = detect_blocker(_page(title="Robot CAPTCHA", url="https://x/captcha", links=[], elements=[{}] * 5))
    assert r.type == BlockerType.CAPTCHA


def test_form_required():
    r = detect_blocker(_page(forms=[_pw_form("text", "search")], links=[f"u{i}" for i in range(12)],
                             title="Search", url="https://x/search", elements=[{}] * 25))
    assert r.type == BlockerType.FORM_REQUIRED
