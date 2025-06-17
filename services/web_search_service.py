import httpx
import logging
import config
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"

async def perform_search(query: str) -> str:
    """
    Performs a web search using the Tavily API and returns a formatted string of results.
    """
    if not config.TAVILY_API_KEY:
        logger.warning("Tavily API key is not configured.")
        return "Error: Web search functionality is not configured."

    payload = {
        "api_key": config.TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "include_answer": False,
        "max_results": 5
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(TAVILY_API_URL, json=payload)
            response.raise_for_status()
            results = response.json()

            if not results.get("results"):
                return "No search results found."

            # Format the results into a string for the LLM context
            formatted_results = []
            for i, result in enumerate(results["results"]):
                formatted_results.append(
                    f"Source {i+1}: {result.get('url')}\n"
                    f"Content: {result.get('content')}"
                )
            
            return "\n\n---\n\n".join(formatted_results)

    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily API error: {e.response.status_code} - {e.response.text}")
        return f"Error: Web search failed with status {e.response.status_code}."
    except Exception as e:
        logger.exception(f"An unexpected error occurred during web search: {e}")
        return "Error: An unexpected error occurred during the web search."