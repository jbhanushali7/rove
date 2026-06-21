import json
import hashlib
from pathlib import Path
from markdownify import markdownify as _md
from rove.exporters.base import Exporter, CrawlResult


class JsonExporter(Exporter):
    name = "json"

    def export(self, result: CrawlResult, dest: Path) -> Path:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "pages.json"
        out.write_text(json.dumps(result.pages, indent=2))
        return out


class MarkdownExporter(Exporter):
    name = "markdown"

    def export(self, result: CrawlResult, dest: Path) -> Path:
        dest = Path(dest) / "markdown"
        dest.mkdir(parents=True, exist_ok=True)
        for page in result.pages:
            html = page.get("html") or ""
            md = f"# {page.get('title','')}\n\n<{page.get('url','')}>\n\n" + _md(html)
            stem = hashlib.md5(page.get("url", "").encode()).hexdigest()
            (dest / f"{stem}.md").write_text(md, encoding="utf-8")
        return dest


class ActionMapExporter(Exporter):
    """Portable agent action-map: states (pages/SPA states) + transition edges +
    element locators an agent uses to drive each transition."""
    name = "action-map"

    def export(self, result: CrawlResult, dest: Path) -> Path:
        states, edges = [], []
        for p in result.pages:
            states.append({
                "id": p.get("page_id") or p.get("url"),
                "url": p.get("url"),
                "fingerprint": p.get("fingerprint"),
                "actions": [e.get("locators", {}) for e in p.get("elements", [])],
            })
            for link in p.get("links", []):
                edges.append({"from": p.get("page_id") or p.get("url"), "to": link, "type": "link"})
            parent = p.get("parent_state")
            if parent:
                t = p.get("transition") or {}
                edges.append({"from": parent, "to": p.get("page_id"),
                              "type": t.get("type", "click"), "via": t.get("via_element")})
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "action_map.json"
        out.write_text(json.dumps({"states": states, "edges": edges}, indent=2))
        return out
