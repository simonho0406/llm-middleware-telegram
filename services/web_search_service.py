import httpx
import logging
import config
import asyncio
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"

async def _tavily_search(query: str) -> dict:
    """
    Performs a web search using the Tavily API and returns a formatted string of results.
    """
    if not config.TAVILY_API_KEY:
        logger.warning("Tavily API key is not configured.")
        return {'status': 'error', 'message': "Tavily search is not configured. Please set TAVILY_API_KEY."}

    payload = {
        "api_key": config.TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "include_answer": False,
        "max_results": 5
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(TAVILY_API_URL, json=payload, timeout=30.0)
            response.raise_for_status()
            results = response.json()

            if not results.get("results"):
                return {'status': 'success', 'content': "No search results found."}

            # Format the results into a string for the LLM context
            formatted_results = []
            for i, result in enumerate(results["results"]):
                formatted_results.append(
                    f"Source {i+1}: {result.get('url')}\n"
                    f"Content: {result.get('content')}"
                )
            
            return {'status': 'success', 'content': "\n\n---\n\n".join(formatted_results)}

    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily API error: {e.response.status_code} - {e.response.text}")
        return {'status': 'error', 'message': f"Web search failed with status {e.response.status_code}."}
    except Exception as e:
        logger.exception(f"An unexpected error occurred during web search: {e}")
        return {'status': 'error', 'message': "An unexpected error occurred during the web search."}

async def _google_search(query: str) -> dict:
    """
    Performs a web search using the Google Custom Search Engine API.
    """
    if not config.GOOGLE_API_KEY or not config.GOOGLE_CSE_ID:
        logger.warning("Google API key or CSE ID are not configured.")
        return {'status': 'error', 'message': "Google Search is not configured. Please set GOOGLE_API_KEY and GOOGLE_CSE_ID."}

    GOOGLE_CSE_API_URL = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": config.GOOGLE_API_KEY,
        "cx": config.GOOGLE_CSE_ID,
        "q": query
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(GOOGLE_CSE_API_URL, params=params, timeout=30.0)
            response.raise_for_status()
            results = response.json()

            if "items" not in results or not results["items"]:
                return {'status': 'success', 'content': "No search results found."}

            formatted_results = []
            for i, item in enumerate(results["items"]):
                link = item.get("link")
                snippet = item.get("snippet")
                formatted_results.append(
                    f"Source {i+1}: {link}\n"
                    f"Content: {snippet}"
                )
            
            return {'status': 'success', 'content': "\n\n---\n\n".join(formatted_results)}

    except httpx.HTTPStatusError as e:
        logger.error(f"Google CSE API error: {e.response.status_code} - {e.response.text}")
        return {'status': 'error', 'message': f"Google Search failed with status {e.response.status_code}."}
    except Exception as e:
        logger.exception(f"An unexpected error occurred during Google Search: {e}")
        return {'status': 'error', 'message': "An unexpected error occurred during the Google search."}

async def execute_parallel_google_searches(queries: list[str]) -> dict:
    """
    Executes multiple Google searches concurrently and returns a dictionary of successful results.
    """
    tasks = [_google_search(query) for query in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful_results = {}
    for query, result in zip(queries, results):
        if not isinstance(result, Exception) and result.get('status') == 'success':
            successful_results[query] = result['content']
        elif isinstance(result, Exception):
            logger.error(f"Error during parallel Google search for '{query}': {result}", exc_info=True)
            
    return successful_results

async def perform_search(query: str, manual: bool = False) -> dict:
    """
    Dispatcher function to perform a web search based on manual/automated intent.
    Manual searches use Tavily for high-quality summaries. Automated searches use Google CSE.
    """
    if manual:
        provider = config.get_manual_search_provider().lower()
    else:
        provider = config.get_automated_search_provider().lower()

    logger.info(f"Performing {'MANUAL' if manual else 'AUTOMATED'} web search for '{query}' using provider: {provider}")

    if provider == "tavily":
        return await _tavily_search(query)
    elif provider == "google":
        return await _google_search(query)
    else:
        logger.error(f"Unsupported web search provider: {provider}")
        return {'status': 'error', 'message': f"Unsupported web search provider '{provider}'."}

async def perform_multi_search(queries: list[str], manual: bool = False) -> dict:
    """
    Dispatcher function to perform multiple web searches concurrently.
    Returns a combined string of all results.
    """
    if manual:
        provider = config.get_manual_search_provider().lower()
    else:
        provider = config.get_automated_search_provider().lower()

    # Deduplicate queries while preserving order
    unique_queries = list(dict.fromkeys(queries))
    logger.info(f"Performing {'MANUAL' if manual else 'AUTOMATED'} multi-search for {len(unique_queries)} queries using provider: {provider}")

    if provider == "google":
        successful_results = await execute_parallel_google_searches(unique_queries)
        if not successful_results:
             return {'status': 'error', 'message': "All parallel search requests failed or returned no results."}
        
        combined_content = []
        for q, res in successful_results.items():
             combined_content.append(f"=== Search Results for: '{q}' ===\n{res}")
        return {'status': 'success', 'content': "\n\n".join(combined_content)}
        
    elif provider == "tavily":
        tasks = [_tavily_search(q) for q in unique_queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        combined_content = []
        has_success = False
        for q, res in zip(unique_queries, results):
             if not isinstance(res, Exception) and res.get('status') == 'success':
                 has_success = True
                 combined_content.append(f"=== Search Results for: '{q}' ===\n{res['content']}")
             else:
                 logger.error(f"Error during parallel Tavily search for '{q}': {res}")
        
        if not has_success:
             return {'status': 'error', 'message': "All parallel search requests failed or returned no results."}
             
        return {'status': 'success', 'content': "\n\n".join(combined_content)}
        
    else:
        logger.error(f"Unsupported web search provider: {provider}")
        return {'status': 'error', 'message': f"Unsupported web search provider '{provider}'."}