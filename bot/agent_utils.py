import logging
import config
from bot import providers

logger = logging.getLogger(__name__)

async def is_search_required(prompt: str) -> tuple[bool, bool]:
    """
    Uses a fast LLM to determine if a user's prompt requires a web search.
    Returns a tuple: (is_required, error_occurred)
    """
    if not config.TAVILY_API_KEY:
        return False, False

    try:
        meta_prompt = (
            f"You are a high-speed, efficient query classifier. Your sole purpose is to determine if a user's query "
            f"requires a real-time web search to answer accurately. You must only respond with a single word: YES or NO.\n\n"
            f"Does the following query require a web search? Topics that need a search include current events, "
            f"real-time information (e.g., stock prices, weather), or questions about recent discoveries or developments.\n\n"
            f"Query: \"{prompt}\"\n\n"
            f"Respond with only YES or NO."
        )

        service = providers.get_service_for_provider('gemini')
        response_chunks = [
            chunk async for chunk in service.generate_response(
                model='gemini-1.5-flash-latest',
                prompt=meta_prompt,
                context_history=None,
                request_timeout=15
            )
        ]
        decision = "".join(response_chunks).strip().upper()

        logger.info(f"Search detection agent decided: {decision}")
        return "YES" in decision, False

    except Exception as e:
        logger.error(f"Search detection agent failed: {e}")
        return False, True