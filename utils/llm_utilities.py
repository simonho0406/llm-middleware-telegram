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
from config import EXPERT_PANEL_CONFIG

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
) -> str:
    """
    Centralized, robust LLM response function with built-in retry logic and fallback handling.
    
    This function provides consistent error handling across all LLM calls in the application:
    - Makes the primary API call to the specified provider/model
    - Includes comprehensive error handling for timeouts and provider failures  
    - Automatically falls back to fallback provider when primary provider fails
    - Implements retry logic with configurable attempts and delays
    - Returns either a successful response or a detailed error message
    
    Args:
        provider_name: Primary provider to use (e.g., "ollama", "gemini", "nvidia")
        model: Model name for the primary provider
        prompt: The prompt to send to the LLM
        history: Optional conversation history for context
        role_name: Descriptive name for logging purposes
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Delay between retries in seconds (default: 1)
        request_timeout: Optional timeout in seconds
        fallback_provider: Optional fallback provider name
        fallback_model: Optional fallback model name
        
    Returns:
        str: Either the LLM response or a formatted error message starting with "[Error:"
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"Attempting {role_name} call (attempt {attempt + 1}/{max_retries})")
            
            # Get the service for the primary provider
            service = providers.get_service_for_provider(provider_name)
            if service is None:
                raise ValueError(f"Service for '{provider_name}' not configured or available.")
            
            # Make the primary API call
            response_chunks = []
            async for chunk in service.generate_response(
                model=model,
                prompt=prompt,
                context_history=history,
                request_timeout=request_timeout
            ):
                response_chunks.append(chunk)
            
            response = ''.join(response_chunks)
            
            # Check for provider-level errors in the response
            if response.startswith("[Error:") or response.startswith("Error:"):
                raise ValueError(f"Provider returned error: {response}")
            
            logger.debug(f"{role_name} call succeeded on attempt {attempt + 1}")
            return response
            
        except asyncio.TimeoutError as e:
            last_error = f"Timeout after {request_timeout}s: {str(e)}"
            logger.warning(f"{role_name} timeout on attempt {attempt + 1}: {last_error}")
            
        except Exception as e:
            last_error = str(e)
            logger.warning(f"{role_name} failed on attempt {attempt + 1}: {last_error}")
        
        # Wait before retrying (except on the last attempt)
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)
    
    # All primary attempts failed - try fallback if configured
    if fallback_provider and fallback_model:
        logger.info(f"Primary {role_name} failed after {max_retries} attempts. Trying fallback: {fallback_provider}/{fallback_model}")
        
        try:
            fallback_service = providers.get_service_for_provider(fallback_provider)
            if fallback_service is not None:
                response_chunks = []
                async for chunk in fallback_service.generate_response(
                    model=fallback_model,
                    prompt=prompt,
                    context_history=history,
                    request_timeout=request_timeout
                ):
                    response_chunks.append(chunk)
                
                response = ''.join(response_chunks)
                
                if not response.startswith("[Error:") and not response.startswith("Error:"):
                    logger.info(f"{role_name} fallback succeeded")
                    return f"[Fallback by {fallback_provider}] {response}"
                    
        except Exception as fallback_error:
            logger.error(f"{role_name} fallback also failed: {fallback_error}")
    
    # Both primary and fallback failed
    error_msg = f"[Error: {role_name} failed after {max_retries} attempts. Last error: {last_error}]"
    logger.error(error_msg)
    return error_msg


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
    orchestrator_config = EXPERT_PANEL_CONFIG.get('orchestrator', {})
    fallback_provider = orchestrator_config.get('fallback_provider')
    fallback_model = orchestrator_config.get('fallback_model')
    
    return fallback_provider, fallback_model


async def format_text_for_telegram(raw_text: str) -> tuple[str, bool]:
    """
    Agentic Markdown Formatter - Converts raw text to Telegram MarkdownV2 format.
    
    This function implements a "Two-Pass Agentic" architecture where a dedicated,
    high-speed Formatter Agent converts raw text into 100% compliant MarkdownV2
    for Telegram, avoiding conflicts with primary agents' internal reasoning tags.
    
    Args:
        raw_text: The raw, unformatted text to be converted
        
    Returns:
        tuple[str, bool]: (formatted_text, agent_success)
        - formatted_text: MarkdownV2 formatted text, escaped fallback, or raw text
        - agent_success: True if Formatter Agent succeeded, False if fallback was used
    """
    if not raw_text.strip():
        return raw_text, True  # Empty text is always "successfully" formatted
    
    # Use precise, reliable model for formatting (prioritize accuracy over speed)
    formatter_provider = "groq"  # Fast and reliable for formatting tasks
    formatter_model = "llama-3.1-70b-versatile"  # More precise model for formatting accuracy
    
    # Dynamic timeout based on text length (Challenge B fix)
    base_timeout = 30
    dynamic_timeout = base_timeout + (len(raw_text) // 1000) * 5  # +5s per 1000 chars
    
    # Precise MarkdownV2 formatting prompt with examples
    formatter_prompt = f"""You are a Telegram MarkdownV2 formatter. Convert the text between XML tags to valid MarkdownV2.

**CRITICAL**: You must escape these characters when they appear in regular text: ( ) . ! - + = | {{ }} # 

**Examples of proper escaping**:
- Regular text: "Score (85/100)" → "Score \\(85/100\\)"
- Regular text: "Quality: 72/85" → "Quality: 72/85" 
- Regular text: "test.example.com" → "test\\.example\\.com"
- Keep markdown: "*bold text*" → "*bold text*"
- Keep code: "`code here`" → "`code here`"

**ESSENTIAL RULES**:
1. Escape ( and ) with backslashes: \\( and \\)
2. Escape dots with backslashes: \\.
3. Escape hyphens in regular text: \\-  
4. DO NOT escape characters inside `code` or ```code blocks```
5. DO NOT escape markdown formatting characters (*bold*, _italic_)

<raw_text_to_format>
{raw_text}
</raw_text_to_format>

Output the properly escaped text only:"""

    try:
        # Use robust LLM response with dynamic timeout
        formatted_response = await get_robust_llm_response(
            provider_name=formatter_provider,
            model=formatter_model,
            prompt=formatter_prompt,
            history=None,
            role_name="Formatter Agent",
            max_retries=2,  # Reduced retries for speed
            retry_delay=1,
            request_timeout=dynamic_timeout  # Dynamic timeout based on text length
        )
        
        # Check if formatting was successful
        if not formatted_response.strip() or formatted_response.startswith("[Error:"):
            raise ValueError(f"Formatter Agent failed: {formatted_response}")
        
        logger.info(f"Formatter Agent successfully processed {len(raw_text)} chars in {dynamic_timeout}s timeout")
        
        # Debug logging to see what the formatter actually produced
        formatted_text = formatted_response.strip()
        logger.debug(f"Formatter Agent output preview: {formatted_text[:200]}...")
        
        return formatted_text, True  # Success!
        
    except Exception as e:
        logger.warning(f"Formatter Agent failed, using clean raw text fallback: {e}")
        
        # Unified fallback: always return clean raw text to ensure consistent UX
        # This follows the UX hierarchy: MarkdownV2 > Clean Raw Text > Never Escaped Text
        logger.info("Falling back to clean raw text for optimal user experience")
        return raw_text, False  # Formatter Agent failed, return clean raw text