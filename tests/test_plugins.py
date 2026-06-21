from rove.plugins import get_exporters
from rove.exporters.base import Exporter, CrawlResult


def test_builtin_json_exporter_is_registered():
    exporters = get_exporters()
    assert "json" in exporters
    assert issubclass(exporters["json"], Exporter)


def test_action_map_exporter_is_registered():
    assert "action-map" in get_exporters()


def test_unknown_exporter_absent():
    assert "nonexistent-exporter" not in get_exporters()


def test_json_exporter_emits_pages(tmp_path):
    result = CrawlResult(pages=[{"url": "https://x/", "title": "X", "elements": [], "links": []}])
    out = get_exporters()["json"]().export(result, tmp_path)
    assert out.exists()
    import json
    assert json.loads(out.read_text())[0]["url"] == "https://x/"
