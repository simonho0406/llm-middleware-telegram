import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

# We import the functions to test
from bot.handlers import ask_selected_handler

@pytest.mark.asyncio
@patch('bot.handlers.ask_selected_handler.get_models_for_provider')
async def test_build_model_keyboard_pagination(mock_get_models):
    # Mock a provider returning 15 models (more than ITEMS_PER_PAGE=8)
    mock_models = [
        {"id": f"model-{i}", "name": f"Test Model {i}"} for i in range(1, 16)
    ]
    mock_get_models.return_value = mock_models

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.user_data = {}

    # Test Page 1
    keyboard_markup = await ask_selected_handler.build_model_keyboard("test_provider", set(), context, page=1)
    
    # Assertions for Page 1
    assert isinstance(keyboard_markup, InlineKeyboardMarkup)
    assert context.user_data['ask_selected_page'] == 1
    
    # 8 models / 2 per row = 4 rows of models
    # + 1 row for navigation (Prev/Next)
    # + 1 row for actions (Back/Done)
    # + 1 row for Cancel
    # Total rows = 7
    assert len(keyboard_markup.inline_keyboard) == 7

    # Check that the navigation buttons correctly point to Next Page
    nav_row = keyboard_markup.inline_keyboard[-3]
    assert "Prev" in nav_row[0].text
    assert "Next" in nav_row[1].text
    
    # Test Page 2
    keyboard_markup_p2 = await ask_selected_handler.build_model_keyboard("test_provider", set(), context, page=2)
    assert context.user_data['ask_selected_page'] == 2
    
    # 7 models remaining / 2 per row = 4 rows (3 full, 1 half)
    # + 1 row for navigation
    # + 1 row for actions
    # + 1 row for Cancel
    # Test Page 20 (Out of bounds, should modulo to page 5 because 15 models // 8 = 2 pages)
    # Actually, 15 models // 8 ITEMS_PER_PAGE = 2 pages (Page 1: 8 models, Page 2: 7 models).
    # If total_pages = 2, then page 20: ((20 - 1) % 2) + 1 = (19 % 2) + 1 = 1 + 1 = 2.
    # So page 20 should cleanly modulo back to page 2!
    keyboard_markup_p20 = await ask_selected_handler.build_model_keyboard("test_provider", set(), context, page=20)
    assert context.user_data['ask_selected_page'] == 2 # verify it clamped properly
    assert len(keyboard_markup_p20.inline_keyboard) == 7
