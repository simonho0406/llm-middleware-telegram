"""
Centralized LLM utilities for robust API calls across all handlers.

This module provides consistent, resilient LLM interaction patterns with:
- Automatic retry logic with configurable attempts and delays
- Provider fallback handling for improved reliability
- Comprehensive error handling and logging
- Timeout management and graceful degradation
- Consistent response formatting across all handlers
"""

import asyncio
import logging
import re
from typing import List, Dict, Any, Optional, AsyncGenerator

from bot import providers
from config import get_expert_panel_config
from utils.context_manager import ensure_context_fits


logger = logging.getLogger(__name__)


def format_tools_for_prompt(tools: list) -> str:
    """Convert a list of OpenAI-style tool dicts into a human-readable string for LLM prompts.

    Includes parameter names and types so the model knows how to call each tool, not just
    that it exists. Without schema info the model defaults to guessing 'query' for all tools,
    which only works for search-style tools.
    """
    if not tools:
        return "No tools available."
    lines = []
    for t in tools:
        func = t.get('function', {})
        name = func.get('name', '')
        desc = func.get('description', '')
        params = func.get('parameters', {})
        properties = params.get('properties', {})
        required = set(params.get('required', []))
        if properties:
            param_parts = []
            for prop_name, prop_schema in properties.items():
                prop_type = prop_schema.get('type', 'any')
                req_marker = '*' if prop_name in required else '?'
                param_parts.append(f"{prop_name}{req_marker}: {prop_type}")
            args_str = ", ".join(param_parts)
            lines.append(f"- {name}({args_str}): {desc}")
        else:
            lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def extract_json_object(text: str) -> str:
    """Extract the first complete top-level JSON object from LLM text.

    String-aware brace matching (ignores braces inside quoted strings, so a value
    like "{a}" doesn't throw off the counter), with regex fallbacks. Returns the
    JSON substring or "" if none found. Note: a returned string is brace-balanced
    but may still fail json.loads if the model left unescaped quotes inside a value
    — callers should retry the LLM in that case.
    """
    if not text:
        return ""
    start = text.find('{')
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
    # Fallbacks for malformed/oddly-nested output
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
    if m:
        return m.group(0)
    m = re.search(r'{[\s\S]*}', text)
    if m:
        return m.group(0)
    return ""


def is_error_response(s: str) -> bool:
    """True if `s` is a provider-level error sentinel, not legitimate model output."""
    return s.startswith("[Error:") or s.startswith("Error:")


async def _attempt_call(
    provider_name: str,
    model: str,
    prompt: str,
    history: List[Dict[str, Any]],
    request_timeout: Optional[int],
    role_name: str,
) -> str:
    """One provider call: resolve service, fit context, stream, return text. Raises on any failure."""
    service = providers.get_service_for_provider(provider_name)
    if service is None:
        raise ValueError(f"Service for '{provider_name}' not configured or available.")

    truncated_history, context_info = await ensure_context_fits(
        prompt=prompt, history=history, model=model, provider=provider_name
    )
    if context_info:
        logger.debug(f"{role_name} Context Info: {context_info}")

    response_chunks = []
    async for chunk in service.generate_response(
        model=model,
        prompt=prompt,
        context_history=truncated_history,
        request_timeout=request_timeout,
    ):
        response_chunks.append(chunk)

    response = "".join(response_chunks)
    if is_error_response(response):
        raise ValueError(f"Provider returned error: {response}")
    return response


async def get_robust_llm_response(
    provider_name: str,
    model: str,
    prompt: str,
    history: Optional[List[Dict[str, Any]]] = None,
    role_name: str = "LLM",
    max_retries: int = 3,
    retry_delay: int = 1,
    request_timeout: Optional[int] = None,
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Centralized, robust LLM response function with built-in retry logic and fallback handling.

    Returns:
        Dict[str, Any]: A dictionary containing:
            - 'response': The LLM's response string or an error message.
            - 'retries': The number of retries used.
            - 'fallback_used': A boolean indicating if the fallback provider was used.
            - 'is_error': True only when the response is an error sentinel, never when a
              valid response happens to quote an error string as evidence.
    """
    _history = history if history is not None else []
    last_error = None
    retries = 0
    fallback_used = False

    for attempt in range(max_retries):
        retries = attempt
        try:
            logger.debug(f"Attempting {role_name} call (attempt {attempt + 1}/{max_retries})")
            response = await _attempt_call(provider_name, model, prompt, _history, request_timeout, role_name)
            logger.debug(f"{role_name} call succeeded on attempt {attempt + 1}")
            return {'response': response, 'retries': retries, 'fallback_used': fallback_used, 'is_error': False}

        except asyncio.TimeoutError as e:
            last_error = f"Timeout after {request_timeout}s: {str(e)}"
            logger.warning(f"{role_name} timeout on attempt {attempt + 1}: {last_error}")

        except Exception as e:
            last_error = str(e)
            logger.exception(f"{role_name} failed on attempt {attempt + 1}: {last_error}")

        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)

    # All primary attempts failed — try fallback if configured.
    if fallback_provider and fallback_model:
        fallback_used = True
        logger.info(f"Primary {role_name} failed after {max_retries} attempts. Trying fallback: {fallback_provider}/{fallback_model}")
        try:
            response = await _attempt_call(fallback_provider, fallback_model, prompt, _history, request_timeout, role_name)
            logger.info(f"{role_name} fallback succeeded")
            return {'response': response, 'retries': retries, 'fallback_used': fallback_used, 'is_error': False}
        except Exception as fallback_error:
            logger.exception(f"{role_name} fallback also failed: {fallback_error}")

    error_msg = f"[Error: {role_name} failed after {max_retries} attempts. Last error: {last_error}]"
    logger.error(error_msg)
    return {'response': error_msg, 'retries': retries, 'fallback_used': fallback_used, 'is_error': True}


async def get_streaming_llm_response(
    provider_name: str,
    model: str,
    prompt: str,
    history: Optional[List[Dict[str, Any]]] = None,
    request_timeout: Optional[int] = None
) -> AsyncGenerator[str, None]:
    """
    Streaming LLM response generator for real-time chat applications.
    
    This function provides streaming responses with basic error handling.
    For robust non-streaming responses, use get_robust_llm_response instead.
    
    Args:
        provider_name: Provider to use (e.g., "ollama", "gemini", "nvidia")  
        model: Model name for the provider
        prompt: The prompt to send to the LLM
        history: Optional conversation history for context
        request_timeout: Optional timeout in seconds
        
    Yields:
        str: Response chunks as they arrive, or error messages
    """
    try:
        service = providers.get_service_for_provider(provider_name)
        if service is None:
            yield f"[Error: Service for '{provider_name}' not configured or available.]"
            return
        
        async for chunk in service.generate_response(
            model=model,
            prompt=prompt,
            context_history=history,
            request_timeout=request_timeout
        ):
            yield chunk
            
    except asyncio.TimeoutError:
        yield f"[Error: Request timed out after {request_timeout}s]"
    except Exception as e:
        yield f"[Error: {str(e)}]"


def get_expert_panel_fallback_config() -> tuple[Optional[str], Optional[str]]:
    """
    Extract fallback provider and model from expert panel configuration.
    
    Returns:
        tuple: (fallback_provider, fallback_model) or (None, None) if not configured
    """
    orchestrator_config = get_expert_panel_config().get('orchestrator', {})
    fallback_provider = orchestrator_config.get('fallback_provider')
    fallback_model = orchestrator_config.get('fallback_model')
    
    return fallback_provider, fallback_model
