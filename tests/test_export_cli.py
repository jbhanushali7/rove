from rove.crawl import resolve_exporters


def test_known_exporter_resolves():
    chosen, missing = resolve_exporters(["json"])
    assert "json" in chosen and missing == []


def test_action_map_exporter_resolves():
    chosen, missing = resolve_exporters(["action-map"])
    assert "action-map" in chosen and missing == []


def test_unknown_exporter_reported_missing():
    chosen, missing = resolve_exporters(["nonexistent"])
    assert chosen == {}
    assert "nonexistent" in missing
