"""Exa search tool.

Shared by the core agent and the auto-routing router. Uses the Exa `/search`
endpoint (https://api.exa.ai/search) with `contents.highlights` mode, which the
Exa docs recommend for agent workflows (most relevant excerpts, ~10x fewer
tokens than full text).

Design:
- The heavy lifting lives in `_exa_search` so both agents share one HTTP path.
- The public `web_search(query)` tool reads `settings.exa.search_type` (default
  "auto", quality-oriented) and is registered on the core agent.
- The router registers its own `web_search`-named tool that calls the same
  `_exa_search` helper with `settings.exa.router_search_type` (default "fast")
  to keep the cheap path low-latency.
- If `EXA_API_KEY` is unset or the request fails, the tool raises `ModelRetry`
  with a short, non-leaky message. The router then escalates; the core agent
  treats it as a normal tool failure.
"""

import httpx
from pydantic_ai import ModelRetry

from src.config import settings

EXA_SEARCH_URL = "https://api.exa.ai/search"


async def _exa_search(query: str, search_type: str, num_results: int) -> str:
    """Run an Exa highlights search and return a compact text digest.

    Args:
        query: Natural-language search query.
        search_type: Exa search type ("auto", "fast", "instant", ...).
        num_results: Number of results to request.

    Returns:
        A newline-joined digest of titles, URLs, and highlight excerpts.

    Raises:
        ModelRetry: If the API key is missing or the request errors out, so the
            caller (router or core agent) can fail fast / retry appropriately.
    """
    if not settings.exa.is_configured:
        # Fail fast: router escalates, core agent surfaces a retry to the model.
        raise ModelRetry("Web search is not available (EXA_API_KEY not configured).")

    payload = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "contents": {"highlights": True},
    }
    headers = {"Authorization": f"Bearer {settings.exa.api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=settings.exa.timeout) as client:
            response = await client.post(EXA_SEARCH_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise ModelRetry(f"Web search request failed: {exc.__class__.__name__}") from exc

    results = data.get("results", [])
    if not results:
        return "No web results found."

    digest_lines: list[str] = []
    for result in results:
        title = result.get("title", "")
        url = result.get("url", "")
        highlights = result.get("highlights", []) or []
        highlight_text = " ".join(highlights).strip()
        digest_lines.append(f"- {title}: {highlight_text} ({url})")

    return "\n\n".join(digest_lines)


async def web_search(query: str) -> str:
    """Search the web via Exa and return a compact digest of relevant excerpts.

    Use this to look up current information, facts, or references that are not
    already in the workspace. The tool returns titles, URLs, and short highlight
    excerpts; if you need the full page, fetch it separately with
    `get_content_from_url`.

    Args:
        query: A natural-language search query.

    Returns:
        A text digest of the top results with titles, highlights, and URLs.
    """
    return await _exa_search(query, settings.exa.search_type, settings.exa.num_results)
