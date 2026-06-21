import threading
import functools
import http.server
import socketserver
import pytest
from playwright.async_api import async_playwright
from rove.crawl import EXTRACT_JS


@pytest.fixture(scope="module")
def fixture_server():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory="tests/fixtures"
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()


@pytest.mark.asyncio
async def test_iframe_and_shadow_elements_extracted(fixture_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/test_page.html")
        await page.wait_for_load_state("networkidle")
        all_elems = []
        for frame in page.frames:
            raw = await frame.evaluate(EXTRACT_JS)
            for r in raw:
                r["frame_path"] = None if frame is page.main_frame else frame.url
                all_elems.append(r)
        await browser.close()

    shadow = [e for e in all_elems if e.get("shadow_path")]
    iframe = [e for e in all_elems if e.get("frame_path")]
    assert any(e["id"] == "shadow-btn" for e in shadow), "shadow-btn not found in shadow DOM"
    assert any(e["name"] == "shadow-input" for e in shadow), "shadow-input not found in shadow DOM"
    assert any(e["id"] == "iframe-btn" for e in iframe), "iframe-btn not found in iframe"
    assert all(e["shadow_path"].startswith("#shadow-host") for e in shadow), \
        f"Unexpected shadow_path prefix: {[e['shadow_path'] for e in shadow]}"
