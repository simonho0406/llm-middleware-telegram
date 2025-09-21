"""
Simplified Context Management System

Provides model-aware context window management that automatically truncates
conversation history to fit within model limits while preserving as much
recent context as possible.
"""

import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class ModelContextLimits:
    """Model-specific context window information."""
    max_context_tokens: int
    max_completion_tokens: int
    buffer_tokens: int
    supports_long_context: bool = False

    @property
    def effective_input_limit(self) -> int:
        """Calculate effective input limit accounting for completion buffer."""
        return self.max_context_tokens - self.buffer_tokens

# Model-specific context limits database
MODEL_CONTEXT_LIMITS = {
    # OpenRouter Models
    "x-ai/grok-4-fast:free": ModelContextLimits(2000000, 4096, 4096, True),
    "openai/gpt-oss-120b:free": ModelContextLimits(32000, 4096, 4096, False),
    "openai/gpt-oss-20b:free": ModelContextLimits(131072, 8192, 8192, True),
    "deepseek/deepseek-chat-v3.1:free": ModelContextLimits(163840, 8192, 8192, True),
    "google/gemma-3n-e2b-it:free": ModelContextLimits(8192, 2048, 2048, False),
    "tencent/hunyuan-a13b-instruct:free": ModelContextLimits(32768, 4096, 4096, False),
    "mistralai/mistral-small-3.2-24b:free": ModelContextLimits(32768, 4096, 4096, False),

    # Groq Models
    "llama-3.3-70b-versatile": ModelContextLimits(131072, 8192, 8192, True),
    "llama3-8b-8192": ModelContextLimits(8192, 2048, 2048, False),
    "mixtral-8x7b-32768": ModelContextLimits(32768, 4096, 4096, False),
    "gemma2-9b-it": ModelContextLimits(8192, 2048, 2048, False),

    # NVIDIA Models
    "nvidia/llama-3.3-nemotron-70b-instruct": ModelContextLimits(131072, 8192, 8192, True),
    "meta/llama3-70b-instruct": ModelContextLimits(8192, 2048, 2048, False),
    "meta/llama3-8b-instruct": ModelContextLimits(8192, 2048, 2048, False),

    # Gemini Models
    "gemini-1.5-flash-latest": ModelContextLimits(1048576, 8192, 8192, True),
    "gemini-1.5-pro-latest": ModelContextLimits(2097152, 8192, 8192, True),

    # Default fallback
    "_default": ModelContextLimits(4096, 1024, 1024, False)
}

def count_tokens(text: str) -> int:
    """Enhanced token counting with better accuracy."""
    try:
        import tiktoken
        encoder = tiktoken.get_encoding("cl100k_base")
        return len(encoder.encode(text))
    except ImportError:
        # Fallback estimation: ~4 characters per token
        return len(text) // 4
    except Exception:
        # Emergency fallback
        return len(text) // 4

def get_model_context_limits(model: str, provider: str) -> ModelContextLimits:
    """Get context limits for a specific model and provider."""
    # Try exact model match first
    if model in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model]

    # Try provider-specific defaults
    provider_defaults = {
        "groq": ModelContextLimits(32768, 4096, 4096, False),
        "openrouter": ModelContextLimits(32768, 4096, 4096, False),
        "nvidia": ModelContextLimits(131072, 8192, 8192, True),
        "gemini": ModelContextLimits(1048576, 8192, 8192, True),
        "ollama": ModelContextLimits(32768, 4096, 4096, False)
    }

    if provider in provider_defaults:
        logger.info(f"Using provider default context limits for {provider}/{model}")
        return provider_defaults[provider]

    # Final fallback
    logger.warning(f"No context limits found for {provider}/{model}, using default")
    return MODEL_CONTEXT_LIMITS["_default"]

# Removed complex user strategy selection - now using simple automatic truncation

async def calculate_context_usage(
    prompt: str,
    history: List[Dict[str, str]],
    model: str,
    provider: str
) -> Tuple[int, int, bool]:
    """
    Calculate current context usage and determine if truncation is needed.

    Returns:
        (current_tokens, max_allowed_tokens, needs_truncation)
    """
    limits = get_model_context_limits(model, provider)

    prompt_tokens = count_tokens(prompt)
    history_tokens = sum(count_tokens(msg.get("content", "")) for msg in history)
    total_tokens = prompt_tokens + history_tokens

    needs_truncation = total_tokens > limits.effective_input_limit

    logger.debug(f"Context usage: {total_tokens}/{limits.effective_input_limit} tokens (prompt: {prompt_tokens}, history: {history_tokens})")

    return total_tokens, limits.effective_input_limit, needs_truncation

async def truncate_to_fit_context(
    history: List[Dict[str, str]],
    prompt: str,
    model: str,
    provider: str
) -> Tuple[List[Dict[str, str]], int]:
    """
    Truncate conversation history to fit within model's context window.
    Keeps the most recent messages that fit, preserving as much context as possible.

    Returns:
        (truncated_history, tokens_removed)
    """
    limits = get_model_context_limits(model, provider)
    prompt_tokens = count_tokens(prompt)
    available_tokens = limits.effective_input_limit - prompt_tokens

    if available_tokens <= 0:
        logger.warning(f"Prompt alone ({prompt_tokens} tokens) exceeds context limit for {model}")
        return [], 0

    original_tokens = sum(count_tokens(msg.get("content", "")) for msg in history)

    if original_tokens <= available_tokens:
        # Everything fits, no truncation needed
        return history, 0

    # Truncate from the beginning, keeping most recent messages
    truncated_history = []
    current_tokens = 0

    for msg in reversed(history):
        msg_tokens = count_tokens(msg.get("content", ""))
        if current_tokens + msg_tokens <= available_tokens:
            truncated_history.insert(0, msg)
            current_tokens += msg_tokens
        else:
            # This message would exceed the limit, stop here
            break

    tokens_removed = original_tokens - current_tokens

    logger.info(f"Context truncated for {model}: {len(history)} -> {len(truncated_history)} messages, "
                f"removed {tokens_removed} tokens (kept {current_tokens}/{available_tokens} available)")

    return truncated_history, tokens_removed

async def ensure_context_fits(
    prompt: str,
    history: List[Dict[str, str]],
    model: str,
    provider: str
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    Ensure the context fits within the model's limits by truncating if necessary.

    Returns:
        (final_history, info_message)
    """
    current_tokens, max_tokens, needs_truncation = await calculate_context_usage(
        prompt, history, model, provider
    )

    if not needs_truncation:
        return history, None

    # Automatically truncate to fit
    truncated_history, tokens_removed = await truncate_to_fit_context(
        history, prompt, model, provider
    )

    if tokens_removed > 0:
        info_message = f"Context automatically adjusted for {model}: removed {tokens_removed} tokens from conversation history"
        return truncated_history, info_message
    else:
        return history, None