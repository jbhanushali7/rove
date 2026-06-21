"""Compare two rove crawl output directories and report interaction-surface changes."""
import dataclasses
import json
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class CrawlDiff:
    added_pages: list[dict] = field(default_factory=list)
    removed_pages: list[dict] = field(default_factory=list)
    changed_pages: list[dict] = field(default_factory=list)
    added_forms: list[dict] = field(default_factory=list)
    removed_forms: list[dict] = field(default_factory=list)
    added_spa_states: list[dict] = field(default_factory=list)
    removed_spa_states: list[dict] = field(default_factory=list)


def load_pages(pages_dir: str | Path) -> dict[str, dict]:
    """Load all page JSONs from a directory. Returns {page_id: page_data}.

    Raises FileNotFoundError if the directory does not exist.
    page_id is taken from the JSON's 'page_id' field (guaranteed unique per
    file in a real crawl output); falls back to the file stem.
    """
    dir_path = Path(pages_dir)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"pages directory not found: {pages_dir}")
    pages = {}
    for f in dir_path.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            page_id = data.get("page_id") or f.stem
            pages[page_id] = data
        except Exception:
            pass
    return pages


def _group_by_url(pages: dict[str, dict]) -> dict[str, list[dict]]:
    """Group page dicts by URL, preserving all SPA states as separate entries."""
    result: dict[str, list[dict]] = {}
    for p in pages.values():
        url = p.get("url")
        if url:
            result.setdefault(url, []).append(p)
    return result


def diff_crawls(old_dir: str | Path, new_dir: str | Path) -> CrawlDiff:
    """Compare old and new crawl output directories. Returns a CrawlDiff."""
    old = load_pages(old_dir)
    new = load_pages(new_dir)

    old_by_url = _group_by_url(old)
    new_by_url = _group_by_url(new)
    old_urls = set(old_by_url)
    new_urls = set(new_by_url)

    d = CrawlDiff()

    # Added / removed pages
    for url in new_urls - old_urls:
        p = new_by_url[url][0]
        d.added_pages.append({"url": url, "title": p.get("title", ""), "fingerprint": p.get("fingerprint", "")})
    for url in old_urls - new_urls:
        p = old_by_url[url][0]
        d.removed_pages.append({"url": url, "title": p.get("title", ""), "fingerprint": p.get("fingerprint", "")})

    # Changed pages: compare the SET of fingerprints present at each URL.
    # Using a set (rather than a single fingerprint per URL) correctly handles
    # SPA states that share a URL with their parent — each state is a distinct
    # page_id-keyed entry so none are silently overwritten.
    for url in old_urls & new_urls:
        old_fps = sorted({p.get("fingerprint") for p in old_by_url[url] if p.get("fingerprint")})
        new_fps = sorted({p.get("fingerprint") for p in new_by_url[url] if p.get("fingerprint")})
        if old_fps != new_fps:
            d.changed_pages.append({"url": url, "old_fingerprints": old_fps, "new_fingerprints": new_fps})

    # Form changes — restricted to the URL intersection so that forms on
    # brand-new or fully-removed pages are not double-reported alongside
    # added_pages / removed_pages.
    def form_key(form):
        fields = tuple(sorted(f.get("name", "") or "" for f in form.get("fields", [])))
        return (form.get("action", ""), form.get("method", ""), fields)

    for url in old_urls & new_urls:
        old_forms = {form_key(f) for p in old_by_url[url] for f in p.get("forms", [])}
        for p in new_by_url[url]:
            for form in p.get("forms", []):
                if form_key(form) not in old_forms:
                    d.added_forms.append({"url": url, "form": form})

        new_forms = {form_key(f) for p in new_by_url[url] for f in p.get("forms", [])}
        for p in old_by_url[url]:
            for form in p.get("forms", []):
                if form_key(form) not in new_forms:
                    d.removed_forms.append({"url": url, "form": form})

    # SPA state changes: URLs where any page has parent_state set.
    old_spa = {url for url, pages in old_by_url.items() if any(p.get("parent_state") for p in pages)}
    new_spa = {url for url, pages in new_by_url.items() if any(p.get("parent_state") for p in pages)}
    for url in new_spa - old_spa:
        p = next(p for p in new_by_url[url] if p.get("parent_state"))
        d.added_spa_states.append({"url": url, "parent_state": p.get("parent_state")})
    for url in old_spa - new_spa:
        p = next(p for p in old_by_url[url] if p.get("parent_state"))
        d.removed_spa_states.append({"url": url, "parent_state": p.get("parent_state")})

    return d


def diff_to_dict(d: CrawlDiff) -> dict:
    return dataclasses.asdict(d)


def diff_to_markdown(d: CrawlDiff) -> str:
    lines = ["# Crawl Diff\n"]

    def section(title, items):
        if not items:
            lines.append(f"## {title}\n_None_\n")
            return
        lines.append(f"## {title} ({len(items)})\n")
        for item in items:
            lines.append(f"- `{item.get('url', '?')}`")
        lines.append("")

    section("Added Pages", d.added_pages)
    section("Removed Pages", d.removed_pages)

    if not d.changed_pages:
        lines.append("## Changed Pages (fingerprint)\n_None_\n")
    else:
        lines.append(f"## Changed Pages (fingerprint) ({len(d.changed_pages)})\n")
        for item in d.changed_pages:
            old_fps = ", ".join(item.get("old_fingerprints", []))
            new_fps = ", ".join(item.get("new_fingerprints", []))
            lines.append(f"- `{item.get('url', '?')}` — was `{old_fps}` → now `{new_fps}`")
        lines.append("")

    section("Added Forms", d.added_forms)
    section("Removed Forms", d.removed_forms)
    section("Added SPA States", d.added_spa_states)
    section("Removed SPA States", d.removed_spa_states)

    return "\n".join(lines)


def main():
    import argparse, sys
    parser = argparse.ArgumentParser(description="Compare two rove crawl pages/ directories.")
    parser.add_argument("old_dir", help="Path to old crawl pages/ directory")
    parser.add_argument("new_dir", help="Path to new crawl pages/ directory")
    parser.add_argument(
        "--format",
        choices=["json", "markdown", "both"],
        default="markdown",
        help="Output format. Use 'both' with --output to write .json and .md files.",
    )
    parser.add_argument("--output", help="Output file path stem (extensions added automatically)")
    args = parser.parse_args()

    try:
        d = diff_crawls(args.old_dir, args.new_dir)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format in ("json", "both"):
        out = json.dumps(diff_to_dict(d), indent=2)
        if args.output:
            Path(args.output).with_suffix(".json").write_text(out, encoding="utf-8")
        else:
            print(out)

    if args.format in ("markdown", "both"):
        out = diff_to_markdown(d)
        if args.output:
            Path(args.output).with_suffix(".md").write_text(out, encoding="utf-8")
        else:
            print(out)


if __name__ == "__main__":
    main()
