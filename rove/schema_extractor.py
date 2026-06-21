import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def extract_schema(pages: list[dict], schema: dict, llm_client) -> list[dict]:
    """For each page, ask the LLM to extract data matching schema from the page HTML.
    Returns pages with an added "extracted" key (dict or None on failure)."""
    results = []
    for page in pages:
        extracted = await _extract_page(page, schema, llm_client)
        results.append({**page, "extracted": extracted})
    return results


async def _extract_page(page: dict, schema: dict, llm_client) -> dict | None:
    html = page.get("html", "")
    if not html:
        return None
    system = (
        "You are a data extractor. Extract data from the page HTML matching the JSON schema. "
        "Return ONLY valid JSON — no explanation, no markdown."
    )
    prompt = (
        f"Schema:\n{json.dumps(schema, indent=2)}\n\n"
        f"URL: {page.get('url', '')}\n\n"
        f"HTML:\n{html[:8000]}"
    )
    try:
        raw = await llm_client.complete(system=system, prompt=prompt)
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Schema extraction failed for {page.get('url', '')}: {e}")
        return None
