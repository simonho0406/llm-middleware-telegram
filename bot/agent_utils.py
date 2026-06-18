import logging
import config
from utils.llm_utilities import get_robust_llm_response

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

        res = await get_robust_llm_response(
            provider_name=config.get_utility_model_provider(),
            model=config.get_utility_model_name(),
            prompt=meta_prompt,
            history=[],
            role_name="Search Classifier",
            request_timeout=config.get_search_classifier_timeout(),
            fallback_provider=config.get_utility_model_fallback_provider(),
            fallback_model=config.get_utility_model_fallback_model(),
        )
        decision = (res.get("response") or "").strip().upper()
        error_occurred = res.get("is_error", False)
        # Use first-token exact match so "Definitely YES" or "YESSS" don't incorrectly pass.
        first_token = decision.split()[0].rstrip('.!?,') if decision.split() else ""
        logger.info(f"Search detection agent decided: {decision}")
        return first_token == "YES", error_occurred

    except Exception as e:
        logger.exception(f"Search detection agent failed: {e}")
        return False, True