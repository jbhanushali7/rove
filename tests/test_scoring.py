from rove.scoring import score_url


def test_functional_path_bonus():
    assert score_url("https://x.com/login", depth=1) > score_url("https://x.com/foo", depth=1)


def test_form_bonus():
    base = score_url("https://x.com/a", depth=1)
    assert score_url("https://x.com/a", depth=1, linking_page_has_forms=True) == base + 30


def test_boilerplate_penalty():
    assert score_url("https://x.com/privacy", depth=1) < score_url("https://x.com/a", depth=1)


def test_deep_pagination_penalty():
    p2 = score_url("https://x.com/catalogue/page-2.html", depth=1)
    p9 = score_url("https://x.com/catalogue/page-9.html", depth=1)
    assert p9 == p2 - 15


def test_shallow_depth_bonus():
    assert score_url("https://x.com/a", depth=0) == score_url("https://x.com/a", depth=2) + 10
