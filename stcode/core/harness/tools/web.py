"""
Web access — one tool, three Tavily endpoints, chosen by which arguments arrive.

`web_search(query=...)` searches, `web_search(url=...)` extracts that page, and passing
both crawls from that URL looking for the query. One tool rather than three because the
distinction is not a decision the model should have to make correctly before it knows
what it will find: "read this page", "find pages about this", and "explore this site for
this" are the same intent at three scopes, and the arguments already say which.

Everything here is deliberately expensive to reach. CLAUDE.md §5 marks web output as
"always huge" and sub-agent-only, and the reason shows up immediately in practice — a
single search returns more tokens than most files in a repository. The default result
counts are low for that reason, not to save credits.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool

TAVILY_BASE_URL = "https://api.tavily.com"
TAVILY_KEY_ENV = "TAVILY_API_KEY"
WEB_MAX_OUTPUT = 24576
"""Higher than other tools, and still the tool most likely to elide. The remainder is
in `tool_out` — filter it in the REPL rather than asking for less next time."""

_REQUEST_TIMEOUT = 90.0


def _api_key() -> str:
    key = os.environ.get(TAVILY_KEY_ENV, "").strip()
    if not key:
        raise ToolError(
            f"Web access needs a Tavily API key in ${TAVILY_KEY_ENV}. Ask the user to "
            "set it, or answer from the repository and your own knowledge and say that "
            "you could not check the web."
        )
    return key


async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One request to Tavily, with its failures translated into advice.

    Status codes are mapped rather than surfaced raw: a model that reads "401" tries the
    call again, while a model that reads "the key is rejected" stops and says so.
    """
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        try:
            response = await client.post(
                f"{TAVILY_BASE_URL}/{endpoint}",
                headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                json={key: value for key, value in payload.items() if value is not None},
            )
        except httpx.TimeoutException:
            raise ToolError(f"Tavily /{endpoint} timed out after {_REQUEST_TIMEOUT:.0f}s.") from None
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not reach Tavily: {exc}") from exc

    if response.status_code == 401:
        raise ToolError(f"Tavily rejected the key in ${TAVILY_KEY_ENV}. Do not retry.")
    if response.status_code == 429:
        raise ToolError("Tavily rate limit or credit limit reached. Do not retry this turn.")
    if response.status_code >= 400:
        raise ToolError(f"Tavily /{endpoint} returned {response.status_code}: {response.text[:400]}")
    return response.json()  # type: ignore[no-any-return]


@tool(permission=ToolPermission.NETWORK, max_output=WEB_MAX_OUTPUT, timeout=120.0)
async def web_search(
    query: str | None = None,
    url: str | None = None,
    max_results: Annotated[int, Field(ge=1, le=20)] = 5,
    depth: Literal["basic", "advanced"] = "basic",
    include_domains: list[str] | None = None,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Search the web, read a specific page, or crawl a site — depending on what you pass.

    * `query` alone — search the web and return the best matches with excerpts.
    * `url` alone — fetch that one page and return its content as markdown.
    * both — crawl outward from `url`, keeping pages relevant to `query`. Use this for
      documentation sites, where the answer is on a page you cannot name in advance.

    Your training data has a cutoff and this does not. Check here whenever the answer
    depends on a current version number, a recent release, or an API that may have
    changed — and prefer a project's own documentation URL over a general search.

    Args:
        query: What to search for, or what to look for while crawling. Write it as a
            full question, not keywords.
        url: A page to read, or the site to crawl from.
        max_results: How many results to return. Keep it small; each one is long.
        depth: `advanced` searches harder and costs more. Try `basic` first.
        include_domains: Restrict a search to these domains, e.g. `["docs.python.org"]`.
    """
    if not query and not url:
        raise ToolError("Pass `query` to search, `url` to read a page, or both to crawl a site.")

    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    if query and url:
        await runtime.progress(f"Crawling {url} for {query!r}")
        payload = await _post(
            "crawl",
            {"url": url, "instructions": query, "limit": max_results,
             "extract_depth": depth, "format": "markdown"},
        )
        return _render_pages(payload.get("results", []), f"Crawl of {url} for {query!r}")

    if url:
        await runtime.progress(f"Reading {url}")
        payload = await _post(
            "extract", {"urls": [url], "extract_depth": depth, "format": "markdown"}
        )
        results = payload.get("results", [])
        if not results:
            failed = payload.get("failed_results", [])
            reason = failed[0].get("error", "no content") if failed else "no content returned"
            raise ToolError(f"Could not extract {url}: {reason}")
        return _render_pages(results, f"Content of {url}")

    await runtime.progress(f"Searching for {query!r}")
    payload = await _post(
        "search",
        {"query": query, "max_results": max_results, "search_depth": depth,
         "include_answer": True, "include_domains": include_domains},
    )
    return _render_search(payload, query or "")


def _render_search(payload: dict[str, Any], query: str) -> str:
    results = payload.get("results", [])
    if not results:
        return f"No results for {query!r}. Try different wording or a narrower question."

    parts: list[str] = [f"# Web search: {query}"]
    answer = payload.get("answer")
    if answer:
        # Tavily's synthesized answer is a summary of the same sources listed below,
        # so it is labelled as such — it is a starting point, not a citation.
        parts.append(f"\n**Summary (Tavily's, not a source):** {answer}")

    for index, result in enumerate(results, start=1):
        title = result.get("title") or "(untitled)"
        parts.append(
            f"\n## {index}. {title}\n{result.get('url', '')}\n\n"
            f"{(result.get('content') or '').strip()}"
        )
    return "\n".join(parts)


def _render_pages(results: list[dict[str, Any]], heading: str) -> str:
    if not results:
        return f"{heading}: nothing was returned."
    parts = [f"# {heading}"]
    for result in results:
        body = result.get("raw_content") or result.get("content") or ""
        parts.append(f"\n## {result.get('url', '(unknown url)')}\n\n{body.strip()}")
    return "\n".join(parts)


__all__ = ["TAVILY_KEY_ENV", "web_search"]
