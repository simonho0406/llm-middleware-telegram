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
from typing import List, Dict, Any, Optional, AsyncGenerator

from bot import providers
from config import get_expert_panel_config


logger = logging.getLogger(__name__)


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
    last_error = None
    retries = 0
    fallback_used = False
    
    for attempt in range(max_retries):
        retries = attempt
        try:
            logger.debug(f"Attempting {role_name} call (attempt {attempt + 1}/{max_retries})")
            
            # Get the service for the primary provider
            service = providers.get_service_for_provider(provider_name)
            if service is None:
                raise ValueError(f"Service for '{provider_name}' not configured or available.")
            
            from utils.context_manager import ensure_context_fits
            truncated_history, context_info = await ensure_context_fits(
                prompt=prompt,
                history=history if history is not None else [],
                model=model,
                provider=provider_name
            )
            if context_info:
                logger.debug(f"{role_name} Context Info: {context_info}")
            
            # Make the primary API call
            response_chunks = []
            async for chunk in service.generate_response(
                model=model,
                prompt=prompt,
                context_history=truncated_history,
                request_timeout=request_timeout
            ):
                response_chunks.append(chunk)
            
            response = ''.join(response_chunks)
            
            # Check for provider-level errors in the response
            if response.startswith("[Error:") or response.startswith("Error:"):
                raise ValueError(f"Provider returned error: {response}")
            
            logger.debug(f"{role_name} call succeeded on attempt {attempt + 1}")
            return {'response': response, 'retries': retries, 'fallback_used': fallback_used, 'is_error': False}
            
        except asyncio.TimeoutError as e:
            last_error = f"Timeout after {request_timeout}s: {str(e)}"
            logger.warning(f"{role_name} timeout on attempt {attempt + 1}: {last_error}")
            
        except Exception as e:
            last_error = str(e)
            logger.exception(f"{role_name} failed on attempt {attempt + 1}: {last_error}")
        
        # Wait before retrying (except on the last attempt)
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)
    
    # All primary attempts failed - try fallback if configured
    if fallback_provider and fallback_model:
        fallback_used = True
        logger.info(f"Primary {role_name} failed after {max_retries} attempts. Trying fallback: {fallback_provider}/{fallback_model}")
        
        try:
            fallback_service = providers.get_service_for_provider(fallback_provider)
            if fallback_service is not None:
                from utils.context_manager import ensure_context_fits
                truncated_fallback_history, fallback_context_info = await ensure_context_fits(
                    prompt=prompt,
                    history=history if history is not None else [],
                    model=fallback_model,
                    provider=fallback_provider
                )
                if fallback_context_info:
                    logger.debug(f"Fallback {role_name} Context Info: {fallback_context_info}")

                response_chunks = []
                async for chunk in fallback_service.generate_response(
                    model=fallback_model,
                    prompt=prompt,
                    context_history=truncated_fallback_history,
                    request_timeout=request_timeout
                ):
                    response_chunks.append(chunk)
                
                response = ''.join(response_chunks)
                
                if not response.startswith("[Error:") and not response.startswith("Error:"):
                    logger.info(f"{role_name} fallback succeeded")
                    return {'response': f"[Fallback by {fallback_provider}] {response}", 'retries': retries, 'fallback_used': fallback_used, 'is_error': False}
                    
        except Exception as fallback_error:
            logger.exception(f"{role_name} fallback also failed: {fallback_error}")
    
    # Both primary and fallback failed
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
