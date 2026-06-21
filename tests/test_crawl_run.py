import sys

import pytest

import rove.crawl as crawl_mod


async def test_main_maps_argparse_defaults_to_run_crawl_kwargs(monkeypatch):
    captured = {}

    async def fake_run_crawl(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return {"pages_crawled": 0, "stats": {}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(sys, "argv", ["rove", "--url", "https://example.com"])

    await crawl_mod.main()

    assert captured["url"] == "https://example.com"
    assert captured["kwargs"] == {
        "max_pages": 50,
        "depth": 3,
        "concurrency": 2,
        "master_provider": "none",
        "master_model": "",
        "master_autonomy": "review",
        "no_human_in_loop": False,
        "ignore_robots": False,
        "export": [],
        "schema": None,
        "headless": True,
        "wait_until": "domcontentloaded",
        "block_resources": ["image", "font", "media"],
        "stagnation_limit": 15,
    }


async def test_main_maps_argparse_overrides_to_run_crawl_kwargs(monkeypatch):
    captured = {}

    async def fake_run_crawl(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return {"pages_crawled": 0, "stats": {}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(sys, "argv", [
        "rove", "--url", "https://example.com", "--max-pages", "10", "--depth", "1",
        "--concurrency", "3", "--master-provider", "anthropic", "--master-model", "claude",
        "--master-autonomy", "auto", "--no-human-in-loop", "--ignore-robots",
        "--export", "markdown", "--export", "json", "--schema", "schema.json",
        "--headed", "--wait-until", "networkidle",
        "--block-resources", "none",
        "--stagnation-limit", "unlimited",
    ])

    await crawl_mod.main()

    assert captured["url"] == "https://example.com"
    assert captured["kwargs"] == {
        "max_pages": 10,
        "depth": 1,
        "concurrency": 3,
        "master_provider": "anthropic",
        "master_model": "claude",
        "master_autonomy": "auto",
        "no_human_in_loop": True,
        "ignore_robots": True,
        "export": ["markdown", "json"],
        "schema": "schema.json",
        "headless": False,
        "wait_until": "networkidle",
        "block_resources": [],
        "stagnation_limit": None,
    }


async def test_main_rejects_non_numeric_stagnation_limit(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "rove", "--url", "https://example.com", "--stagnation-limit", "banana",
    ])

    with pytest.raises(SystemExit):
        await crawl_mod.main()

    assert "--stagnation-limit" in capsys.readouterr().err
