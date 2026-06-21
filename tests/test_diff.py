import json, pytest
from pathlib import Path
from rove.diff import diff_crawls, diff_to_dict, diff_to_markdown, load_pages, CrawlDiff


def write_page(directory: Path, page_id: str, data: dict):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{page_id}.json").write_text(json.dumps(data), encoding="utf-8")


def test_added_page(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "aaa", {"url": "https://x/a", "title": "A", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "title": "A", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "bbb", {"url": "https://x/b", "title": "B", "fingerprint": "fp2", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.added_pages) == 1
    assert d.added_pages[0]["url"] == "https://x/b"
    assert d.removed_pages == []


def test_removed_page(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "aaa", {"url": "https://x/a", "title": "A", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(old, "bbb", {"url": "https://x/b", "title": "B", "fingerprint": "fp2", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "title": "A", "fingerprint": "fp1", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.removed_pages) == 1
    assert d.removed_pages[0]["url"] == "https://x/b"


def test_changed_page_fingerprint(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp_old", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp_new", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.changed_pages) == 1
    assert d.changed_pages[0]["old_fingerprints"] == ["fp_old"]
    assert d.changed_pages[0]["new_fingerprints"] == ["fp_new"]


def test_missing_fingerprint_not_silently_ignored(tmp_path):
    # A page with a real fingerprint in old but no fingerprint field in new
    # must appear in changed_pages (old guard `if old_fp and new_fp` would skip it).
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp_old", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "forms": [], "links": []})  # no fingerprint key
    d = diff_crawls(old, new)
    assert len(d.changed_pages) == 1
    assert d.changed_pages[0]["old_fingerprints"] == ["fp_old"]
    assert d.changed_pages[0]["new_fingerprints"] == []


def test_added_form(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    form = {"action": "/search", "method": "get", "fields": [{"name": "q", "type": "text", "tag": "input"}]}
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [form], "links": []})
    d = diff_crawls(old, new)
    assert len(d.added_forms) == 1
    assert d.added_forms[0]["url"] == "https://x/a"


def test_removed_form(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    form = {"action": "/login", "method": "post", "fields": [{"name": "user", "type": "text", "tag": "input"}]}
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [form], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.removed_forms) == 1
    assert d.removed_forms[0]["url"] == "https://x/a"


def test_no_double_reporting_new_page_with_form(tmp_path):
    # A brand-new page that has a form should appear in added_pages only,
    # NOT also in added_forms (forms on new pages are covered by added_pages).
    old = tmp_path / "old"
    new = tmp_path / "new"
    form = {"action": "/search", "method": "get", "fields": []}
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "bbb", {"url": "https://x/b", "fingerprint": "fp2", "forms": [form], "links": []})
    d = diff_crawls(old, new)
    assert len(d.added_pages) == 1
    assert d.added_forms == []  # form on a new page must not be double-reported


def test_no_double_reporting_removed_page_with_form(tmp_path):
    # A fully-removed page that had a form should appear in removed_pages only.
    old = tmp_path / "old"
    new = tmp_path / "new"
    form = {"action": "/login", "method": "post", "fields": []}
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(old, "bbb", {"url": "https://x/b", "fingerprint": "fp2", "forms": [form], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.removed_pages) == 1
    assert d.removed_forms == []  # form on a removed page must not be double-reported


def test_no_changes(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    page = {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []}
    write_page(old, "aaa", page)
    write_page(new, "aaa", page)
    d = diff_crawls(old, new)
    assert d.added_pages == [] and d.removed_pages == [] and d.changed_pages == []


def test_spa_state_added(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    write_page(new, "bbb", {"url": "https://x/a::state2", "fingerprint": "fp2", "parent_state": "aaa", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.added_spa_states) == 1


def test_spa_url_collision_not_dropped(tmp_path):
    # SPA states that share a URL with their parent must all be tracked.
    # Old crawl: base page + one tab state, both at same URL.
    # New crawl: base page + different tab state at same URL.
    # diff should report the URL as changed (fingerprint sets differ).
    old = tmp_path / "old"
    new = tmp_path / "new"
    write_page(old, "base", {"page_id": "base", "url": "https://x/a", "fingerprint": "fp_base", "forms": [], "links": []})
    write_page(old, "state1", {"page_id": "state1", "url": "https://x/a", "fingerprint": "fp_s1", "parent_state": "base", "forms": [], "links": []})
    write_page(new, "base", {"page_id": "base", "url": "https://x/a", "fingerprint": "fp_base", "forms": [], "links": []})
    write_page(new, "state2", {"page_id": "state2", "url": "https://x/a", "fingerprint": "fp_s2", "parent_state": "base", "forms": [], "links": []})
    d = diff_crawls(old, new)
    assert len(d.changed_pages) == 1
    assert "fp_s1" in d.changed_pages[0]["old_fingerprints"]
    assert "fp_s2" in d.changed_pages[0]["new_fingerprints"]


def test_nonexistent_dir_raises(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    write_page(real, "aaa", {"url": "https://x/a", "fingerprint": "fp1", "forms": [], "links": []})
    with pytest.raises(FileNotFoundError, match="pages directory not found"):
        diff_crawls(real, tmp_path / "does_not_exist")


def test_diff_to_dict_structure(tmp_path):
    d = CrawlDiff()
    result = diff_to_dict(d)
    assert "added_pages" in result and "removed_pages" in result and "changed_pages" in result


def test_diff_to_markdown_contains_sections(tmp_path):
    d = CrawlDiff(added_pages=[{"url": "https://x/new", "title": "New"}])
    md = diff_to_markdown(d)
    assert "Added Pages" in md
    assert "https://x/new" in md


def test_diff_to_markdown_changed_pages_shows_fingerprints(tmp_path):
    d = CrawlDiff(changed_pages=[{"url": "https://x/a", "old_fingerprints": ["fp_old"], "new_fingerprints": ["fp_new"]}])
    md = diff_to_markdown(d)
    assert "https://x/a" in md
    assert "fp_old" in md
    assert "fp_new" in md
