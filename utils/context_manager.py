"""
Simplified Context Management System

Provides model-aware context window management that automatically truncates
conversation history to fit within model limits while preserving as much
recent context as possible.
"""

import logging
from functools import lru_cache
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

    # Gemini Models (gemini-1.5-* aliases were retired by Google → 404; use current aliases)
    "gemini-flash-latest": ModelContextLimits(1048576, 8192, 8192, True),
    "gemini-flash-lite-latest": ModelContextLimits(1048576, 8192, 8192, True),
    "gemini-2.5-flash": ModelContextLimits(1048576, 8192, 8192, True),
    "gemini-2.5-flash-lite": ModelContextLimits(1048576, 8192, 8192, True),
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

@lru_cache(maxsize=2048)
def _cached_token_len(text: str) -> int:
    """Encode once per unique message and cache the length. ensure_context_fits re-counts
    the whole history every turn; without this the cost is O(N²) over a conversation (turn
    N re-encodes all N messages). With it, each message is encoded once → O(N), and the
    repeated per-turn counting becomes cheap dict lookups. Bounded at 2048 entries."""
    return len(_TIKTOKEN_ENCODER.encode(text))

def count_tokens(text: str) -> int:
    """Token count with per-message memoization (see _cached_token_len)."""
    if _TIKTOKEN_ENCODER:
        try:
            return _cached_token_len(text)
        except Exception:
             # Fallback if encoding fails for some reason
             pass

    # Fallback estimation: ~4 characters per token
    return len(text) // 4

def truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """
    Truncates a given string to exactly hit the model's token budget constraint.
    Reverts to heuristic truncation if tiktoken fails.
    """
    if _TIKTOKEN_ENCODER:
        try:
            tokens = _TIKTOKEN_ENCODER.encode(text)
            if len(tokens) > max_tokens:
                truncated_tokens = tokens[:max_tokens]
                return _TIKTOKEN_ENCODER.decode(truncated_tokens)
            return text
        except Exception as e:
            logger.exception(f"Tiktoken encode failed during hard truncation: {e}")
            pass
            
    # Fallback to heuristic (4 chars per token)
    max_chars = max_tokens * 4
    if len(text) > max_chars:
        return text[:max_chars]
    return text

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
        effective_buffer = int(effective_max_context * config.get_context_emergency_buffer_ratio())
        
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
    safety_margin: float = 1.0,
    max_input_tokens: Optional[int] = None
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    Ensure the context fits within the model's limits by truncating if necessary.
    Optimized to calculate usage and truncate in a single pass.

    Args:
        safety_margin (float): Multiplier for the available context limit (default 1.0).
                              Use < 1.0 to leave extra room (e.g. 0.8 for 20% buffer).
        max_input_tokens (int|None): Hard cap on the input budget, applied ON TOP of the
                              model/config limit. Chat passes a small value (e.g. 28k) so a
                              normal turn doesn't ship ~108k tokens — the dominant driver of
                              free-tier 429s, latency, and tiktoken CPU. Panels omit it and
                              keep the full model budget.

    Returns:
        (final_history, info_message)
    """
    limits = get_model_context_limits(model, provider)
    prompt_tokens = count_tokens(prompt)

    # Calculate effective limit with safety margin
    # We apply the margin to the total input limit to effectively reserve more space
    effective_limit_tokens = int(limits.effective_input_limit * safety_margin)

    # Caller-supplied hard cap (chat budget). Never raises the model limit, only lowers it.
    if max_input_tokens is not None:
        effective_limit_tokens = min(effective_limit_tokens, max_input_tokens)
    
    # Separate and protect system messages from truncation
    system_messages = [msg for msg in history if msg.get("role") == "system"]
    non_system_history = [msg for msg in history if msg.get("role") != "system"]
    
    system_tokens = 0
    for _msg in system_messages:
        _content = _msg.get("content", "")
        if isinstance(_content, str) and _content:
            system_tokens += count_tokens(_content)
        elif not isinstance(_content, str) and _content is not None:
            logger.warning(f"System message has non-string content (type: {type(_content).__name__}); skipping token count.")
    available_tokens = effective_limit_tokens - prompt_tokens - system_tokens
    
    # If prompt and system context alone exceed limits
    if available_tokens <= 0:
        logger.warning(f"Prompt + system context ({prompt_tokens + system_tokens} tokens) exceeds context limit for {model} (Limit: {limits.effective_input_limit})")
        return system_messages, f"Prompt and system instructions too long for {model} context window."

    # Process history in reverse (newest to oldest) to fill available space
    truncated_non_system = []
    current_tokens = 0
    messages_kept = len(system_messages)
    
    # Iterate backwards
    for msg in reversed(non_system_history):
        content = msg.get("content", "")
        # Optimize: Avoid counting if content is empty
        msg_tokens = count_tokens(content) if content else 0
        
        if current_tokens + msg_tokens <= available_tokens:
            # We insert at 0 to reconstruct correct order
            truncated_non_system.insert(0, msg)
            current_tokens += msg_tokens
            messages_kept += 1
        else:
            # Context full. Stop.
            break
            
    # Check if we removed anything
    original_count = len(history)
    final_history = system_messages + truncated_non_system

    # Repair any tool-call pairs that got split by truncation. The truncator removes
    # messages individually, so it can drop an assistant tool-call turn while keeping
    # the following tool-result turn (or vice versa). Both Gemini and OpenAI reject
    # histories with orphaned tool-call / tool-result messages.
    final_history = _repair_tool_call_pairs(final_history)

    if len(final_history) < original_count:
        logger.info(f"Context truncated for {model}: {original_count} -> {len(final_history)} messages. Used {current_tokens + system_tokens}/{effective_limit_tokens} tokens (system protected).")
        info_message = f"Context automatically adjusted for {model}: kept {len(final_history)}/{original_count} messages"
        return final_history, info_message

    logger.debug(f"Context fits: {current_tokens + system_tokens}/{effective_limit_tokens} tokens.")
    return final_history, None


def _repair_tool_call_pairs(history: List[Dict]) -> List[Dict]:
    """
    Ensure every assistant tool-call turn has matching tool-result turns and vice versa.
    Truncation can split a paired group; this pass removes whichever half was left behind
    so LLM APIs never see an orphaned function_call or tool-result.
    Runs at most 2 sweeps (sufficient for non-nested chains).
    """
    for _ in range(2):
        # IDs advertised by assistant tool-call turns present in history
        assistant_ids: set = set()
        for msg in history:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id"):
                        assistant_ids.add(tc["id"])

        # IDs actually answered by tool-result turns present in history
        tool_ids: set = set()
        for msg in history:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                tool_ids.add(msg["tool_call_id"])

        to_remove: set = set()
        for idx, msg in enumerate(history):
            role = msg.get("role")
            if role == "tool":
                if msg.get("tool_call_id") not in assistant_ids:
                    to_remove.add(idx)  # orphaned tool result — its call was truncated
            elif role == "assistant" and msg.get("tool_calls"):
                call_ids = {tc["id"] for tc in msg["tool_calls"] if tc.get("id")}
                if call_ids and not call_ids.issubset(tool_ids):
                    to_remove.add(idx)  # incomplete tool-call — some results were truncated

        if not to_remove:
            break
        history = [msg for idx, msg in enumerate(history) if idx not in to_remove]

    return history