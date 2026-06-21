import json
import pytest
from rove.schema_extractor import extract_schema
from rove.llm import FakeLLMClient, LLMDecision

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "price": {"type": "number"}}}
PAGE = {"url": "https://x/product", "html": "<h1>Widget</h1><span>$9.99</span>", "page_id": "abc"}


async def test_extract_schema_returns_enriched_pages():
    extracted_json = '{"title": "Widget", "price": 9.99}'
    fake = FakeLLMClient(scripted_completions=[extracted_json])
    result = await extract_schema([PAGE], SCHEMA, fake)
    assert len(result) == 1
    assert result[0]["extracted"] == {"title": "Widget", "price": 9.99}
    assert result[0]["url"] == PAGE["url"]   # original fields preserved


async def test_extract_schema_invalid_json_returns_none():
    fake = FakeLLMClient(scripted_completions=["not valid json at all"])
    result = await extract_schema([PAGE], SCHEMA, fake)
    assert result[0]["extracted"] is None


async def test_extract_schema_no_html_returns_none():
    fake = FakeLLMClient(scripted_completions=["{}"])
    result = await extract_schema([{**PAGE, "html": ""}], SCHEMA, fake)
    assert result[0]["extracted"] is None


async def test_extract_schema_multiple_pages():
    fake = FakeLLMClient(scripted_completions=['{"title": "A"}', '{"title": "B"}'])
    pages = [PAGE, {**PAGE, "url": "https://x/b", "html": "<h1>B</h1>"}]
    result = await extract_schema(pages, SCHEMA, fake)
    assert result[0]["extracted"]["title"] == "A"
    assert result[1]["extracted"]["title"] == "B"


async def test_fake_llm_client_complete():
    fake = FakeLLMClient(scripted_completions=["hello world"])
    result = await fake.complete(system="sys", prompt="hi")
    assert result == "hello world"
