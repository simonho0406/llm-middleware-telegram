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


try:
    import tiktoken
    try:
        # Global encoder instance to avoid overhead on every call (cached)
        _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENCODER = None
except ImportError:
    _TIKTOKEN_ENCODER = None

def count_tokens(text: str) -> int:
    """Enhanced token counting with better accuracy and caching."""
    if _TIKTOKEN_ENCODER:
        try:
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
             # Fallback if encoding fails for some reason
             pass
    
    # Fallback estimation: ~4 characters per token
    return len(text) // 4

import config

def get_model_context_limits(model: str, provider: str) -> ModelContextLimits:
    """
    Get context limits for a specific model and provider.
    Respects the global default_max_context_tokens from config as a hard cap.
    """
    # 1. Determine the physical/theoretical limits of the model
    if model in MODEL_CONTEXT_LIMITS:
        base_limits = MODEL_CONTEXT_LIMITS[model]
    else:
        # Unknown model: Assume it supports exactly what the user configured
        # This allows the user_max_tokens to be the sole limiting factor
        logger.debug(f"No hardcoded limits for {model}, defaulting to user configuration.")
        
        # Use values directly from config.yaml
        # This ensures no hardcoded magic numbers dictate the fallback behavior
        user_max_tokens_config = config.get_default_max_context_tokens()
        user_buffer_config = config.get_context_token_output_buffer()
        
        base_limits = ModelContextLimits(
            max_context_tokens=user_max_tokens_config,
            max_completion_tokens=user_buffer_config,
            buffer_tokens=user_buffer_config,
            supports_long_context=True
        )

    # 2. Apply User Configuration Cap
    # We want to use the SMALLER of the model's physical limit or the user's configured limit.
    # e.g. Model supports 2M, User wants 100k -> Use 100k
    # e.g. Model supports 8k, User wants 100k -> Use 8k (physical limit)
    
    user_max_tokens = config.get_default_max_context_tokens()
    
    # Create a new object to avoid mutating the global constants
    effective_max_context = min(base_limits.max_context_tokens, user_max_tokens)
    
    # Ensure buffer doesn't swallow the whole context if user sets a very low limit
    effective_buffer = base_limits.buffer_tokens
    if effective_buffer >= effective_max_context:
        effective_buffer = int(effective_max_context * 0.2) # Reduce buffer to 20% if it's too large
        
    return ModelContextLimits(
        max_context_tokens=effective_max_context,
        max_completion_tokens=base_limits.max_completion_tokens,
        buffer_tokens=effective_buffer,
        supports_long_context=base_limits.supports_long_context
    )

async def ensure_context_fits(
    prompt: str,
    history: List[Dict[str, str]],
    model: str,
    provider: str,
    safety_margin: float = 1.0
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    Ensure the context fits within the model's limits by truncating if necessary.
    Optimized to calculate usage and truncate in a single pass.
    
    Args:
        safety_margin (float): Multiplier for the available context limit (default 1.0). 
                              Use < 1.0 to leave extra room (e.g. 0.8 for 20% buffer).
    
    Returns:
        (final_history, info_message)
    """
    limits = get_model_context_limits(model, provider)
    prompt_tokens = count_tokens(prompt)
    
    # Calculate effective limit with safety margin
    # We apply the margin to the total input limit to effectively reserve more space
    effective_limit_tokens = int(limits.effective_input_limit * safety_margin)
    available_tokens = effective_limit_tokens - prompt_tokens
    
    # If prompt alone is too big, just return empty history (and let generation likely fail or truncate prompt elsewhere)
    if available_tokens <= 0:
        logger.warning(f"Prompt alone ({prompt_tokens} tokens) exceeds context limit for {model} (Limit: {limits.effective_input_limit})")
        return [], f"Prompt too long for {model} context window."

    # Process history in reverse (newest to oldest) to fill available space
    truncated_history = []
    current_tokens = 0
    messages_kept = 0
    
    # Iterate backwards
    for msg in reversed(history):
        content = msg.get("content", "")
        # Optimize: Avoid counting if content is empty
        msg_tokens = count_tokens(content) if content else 0
        
        if current_tokens + msg_tokens <= available_tokens:
            # We insert at 0 to reconstruct correct order
            truncated_history.insert(0, msg)
            current_tokens += msg_tokens
            messages_kept += 1
        else:
            # Context full. Stop.
            break
            
    # Check if we removed anything
    original_count = len(history)
    if messages_kept < original_count:
        tokens_removed = sum(count_tokens(m.get("content", "")) for m in history) - current_tokens # Estimate removed tokens
        # Optimization: calculating exact tokens_removed requires counting the excluded ones. 
        # For logging, we can just say "truncated".
        # But if we want exact number, we'd have to count the rest. 
        # For performance, let's just log message count difference.
        
        logger.info(f"Context truncated for {model}: {original_count} -> {messages_kept} messages. Used {current_tokens}/{available_tokens} tokens.")
        info_message = f"Context automatically adjusted for {model}: kept {messages_kept}/{original_count} messages"
        return truncated_history, info_message
    
    logger.debug(f"Context fits: {current_tokens}/{available_tokens} tokens.")
    return history, None