
import pytest
import json
from unittest.mock import patch, AsyncMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.handlers.panel_workflow import _plan_deep_dive_searches

@pytest.mark.asyncio
async def test_plan_deep_dive_searches_extracts_queries_from_messy_json():
    """
    Tests that _plan_deep_dive_searches can reliably extract a JSON list of
    queries from a realistic, messy LLM response.
    """
    # Arrange
    mock_user_prompt = "What is the future of AI in medicine?"
    mock_tavily_results = "AI in medicine is a broad field, encompassing diagnostics, drug discovery, and personalized treatment. Key areas include machine learning models for image analysis and natural language processing for clinical notes."
    
    # A realistic, messy response from an LLM
    messy_llm_response = """
Of course! Based on the summary, a deeper investigation is warranted. To provide an expert-level answer, we need to explore the specific sub-domains. Here are the Google search queries I recommend:

```json
[
    "AI applications in medical diagnostics 2024",
    "machine learning for drug discovery recent breakthroughs",
    "natural language processing in electronic health records",
    "ethical concerns of AI in medicine"
]
```

Executing these should provide the necessary depth.
"""
    
    # Mock the LLM call to return our messy response
    with patch('bot.handlers.panel_workflow.get_robust_llm_response', new_callable=AsyncMock) as mock_llm_call:
        mock_llm_call.return_value = {'response': messy_llm_response, 'retries': 0, 'fallback_used': False, 'is_error': False}
        
        # Act
        extracted_queries = await _plan_deep_dive_searches(
            orchestrator_provider="mock_provider",
            orchestrator_model="mock_model",
            user_prompt=mock_user_prompt,
            original_query="mock_query",
            initial_results=mock_tavily_results,
            timeout=30,
            fallback_provider="mock_fallback_provider",
            fallback_model="mock_fallback_model"
        )
        
        # Assert
        assert isinstance(extracted_queries, list)
        assert len(extracted_queries) == 4
        assert extracted_queries[0] == "AI applications in medical diagnostics 2024"
        assert extracted_queries[3] == "ethical concerns of AI in medicine"
        
        # Also assert that the LLM was called with the correct context
        call_args = mock_llm_call.call_args
        prompt_arg = call_args.kwargs.get('prompt')
        assert mock_user_prompt in prompt_arg
        assert mock_tavily_results in prompt_arg
