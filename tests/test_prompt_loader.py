import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from bot.prompt_loader import prompt_manager

def test_get_prompt_not_found():
    """Test that get_prompt raises FileNotFoundError for a non-existent prompt."""
    with pytest.raises(FileNotFoundError):
        prompt_manager.get_prompt('non_existent_prompt')
