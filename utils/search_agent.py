import json
import logging
import re
from services import web_search_service

logger = logging.getLogger(__name__)

MAX_SEARCH_ITERATIONS = 3

def _extract_json(text: str) -> dict | None:
    """
    Extracts a JSON object from a string, even if it's embedded in text or code blocks.
    """
    # Regex to find JSON wrapped in ```json ... ``` or just { ... }
    match = re.search(r"```json\n({.*?\n\s*})\n```|({.*?})", text, re.DOTALL)
    if not match:
        logger.warning("No JSON object found in the agent's response.")
        return None

    # Extract the JSON string from the first non-empty group
    json_str = next((g for g in match.groups() if g), None)

    if not json_str:
        logger.warning("Could not extract JSON string from regex match.")
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON from agent response: {e}")
        logger.debug(f"Invalid JSON string: {json_str}")
        return None

async def iterative_search(agent_service, agent_model: str, initial_query: str, history: list) -> str:
    """
    Performs an iterative search process using a planning agent to refine queries and synthesize an answer.

    Args:
        agent_service: The service object for the planning agent (e.g., GeminiService, OllamaService).
        agent_model: The specific model name to be used for planning.
        initial_query: The user's initial question.
        history: The preceding conversation history.

    Returns:
        A synthesized answer based on the search results.
    """
    search_results_context = ""

    for iteration in range(MAX_SEARCH_ITERATIONS):
        is_last_iteration = (iteration + 1) == MAX_SEARCH_ITERATIONS

        planning_prompt = f"""
        You are a research assistant on attempt {iteration + 1} of {MAX_SEARCH_ITERATIONS}.
        Your goal is to answer the user's query: '{initial_query}'.
        You have access to the following search results:
        {search_results_context}

        **Your Task:**

        1.  Review the user's query and the search results you have gathered.
        2.  Decide if you have enough information to provide a high-quality answer.

        **IMPORTANT:** If this is your last attempt ({is_last_iteration}), you MUST set 'answer_ready' to true and synthesize the best possible answer now.

        Respond in JSON format:
        {{
          "answer_ready": boolean,
          "next_query": "string (if answer_ready is false, otherwise empty)",
          "synthesis": "string (if answer_ready is true, otherwise empty)"
        }}
        """

        logger.info(f"Iterative search iteration {iteration + 1} for query: '{initial_query}'")

        # Call the planning agent
        try:
            response_generator = agent_service.generate_response(
                model=agent_model,
                prompt=planning_prompt,
                context_history=history
            )
            agent_response_text = "".join([chunk async for chunk in response_generator])

        except Exception as e:
            logger.exception("An error occurred while calling the planning agent.")
            return f"[Error: The planning agent failed on iteration {iteration + 1}.]"

        # Parse the JSON response
        plan = _extract_json(agent_response_text)
        if not plan:
            # Truncate the raw response to avoid excessively long error messages
            raw_response_preview = (agent_response_text[:250] + '...') if len(agent_response_text) > 250 else agent_response_text
            logger.exception(f"Failed to parse agent's plan. Raw response: {agent_response_text}")
            return f"[Error: Failed to parse agent's plan. Agent responded with: '{raw_response_preview}']"

        # Decide whether to answer or search again
        if plan.get("answer_ready"):
            synthesis = plan.get("synthesis")
            if synthesis:
                logger.info("Search complete. Synthesizing final answer.")
                return synthesis
            else:
                return "[Error: Agent indicated answer was ready but provided no synthesis.]"
        
        next_query = plan.get("next_query")
        if not next_query:
            return "[Error: Agent did not provide a next query or a final answer.]"

        # Perform the next web search
        logger.info(f"Performing new search: '{next_query}'")
        search_result = await web_search_service.perform_search(next_query)
        
        # Append results for the next iteration
        search_results_context += f"\n\n--- Search Result for '{next_query}' ---\n{search_result}"

    logger.warning("Search process reached max iterations without a final answer.")
    return "[Error: The search process did not complete within the maximum number of iterations.]"