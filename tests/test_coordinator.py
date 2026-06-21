from rove.coordinator import CrawlStats, decide


def test_high_error_rate_reduces_concurrency():
    s = CrawlStats()
    for _ in range(7):
        s.record_page("https://x.com/a", ok=True, new_elem_types=set())
    for _ in range(4):
        s.record_page("https://x.com/b", ok=False, new_elem_types=set())
    adj = decide(s)
    assert adj.concurrency == 1


def test_saturation_stops_crawl():
    s = CrawlStats()
    s.record_page("https://x.com/a", ok=True, new_elem_types={"a", "button"})
    for i in range(15):
        s.record_page(f"https://x.com/p{i}", ok=True, new_elem_types=set())
    assert decide(s).stop is True


def test_stagnation_limit_none_disables_stop():
    s = CrawlStats()
    s.record_page("https://x.com/a", ok=True, new_elem_types={"a", "button"})
    for i in range(50):
        s.record_page(f"https://x.com/p{i}", ok=True, new_elem_types=set())
    assert decide(s, stagnation_limit=None).stop is False


def test_stagnation_limit_zero_stops_immediately():
    s = CrawlStats()
    s.record_page("https://x.com/a", ok=True, new_elem_types={"a", "button"})
    assert decide(s, stagnation_limit=0).stop is True


def test_dead_prefix_deprioritized():
    s = CrawlStats()
    for i in range(10):
        s.record_page(f"https://x.com/blog/p{i}", ok=True, new_elem_types=set())
    assert "/blog" in decide(s).deprioritize_prefixes


def test_healthy_crawl_no_adjustments():
    s = CrawlStats()
    s.record_page("https://x.com/a", ok=True, new_elem_types={"a"})
    adj = decide(s)
    assert adj.concurrency is None and adj.stop is False and not adj.deprioritize_prefixes
