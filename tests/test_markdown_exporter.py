from pathlib import Path
from rove.exporters.builtin import MarkdownExporter
from rove.exporters.base import CrawlResult


def test_markdown_exporter_writes_one_file_per_page(tmp_path):
    result = CrawlResult(pages=[
        {"url": "https://x/a", "title": "A", "html": "<h1>Hi</h1><p>yo</p>"},
        {"url": "https://x/b", "title": "B", "html": "<p>second</p>"},
    ])
    out = MarkdownExporter().export(result, tmp_path)
    files = list(Path(out).glob("*.md"))
    assert len(files) == 2
    assert "Hi" in (Path(out) / files[0].name).read_text()
