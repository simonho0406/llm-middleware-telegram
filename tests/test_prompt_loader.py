import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from bot.prompt_loader import prompt_manager

def test_get_prompt_not_found():
    """Test that get_prompt raises FileNotFoundError for a non-existent prompt."""
    with pytest.raises(FileNotFoundError):
        prompt_manager.get_prompt('non_existent_prompt')


def test_get_prompt_happy_path():
    """An existing prompt loads and returns a non-empty string (the path the whole
    panel/chat pipeline depends on, previously untested)."""
    prompt = prompt_manager.get_prompt('panel_proposer', inject_environment=False)
    assert isinstance(prompt, str)
    assert prompt.strip(), "loaded prompt should not be empty"
