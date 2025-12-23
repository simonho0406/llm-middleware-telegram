from utils.text_processing import format_thinking_content, format_for_telegram_v2

def test_thinking_formatter():
    print("--- Test 1: Simple Thinking ---")
    raw = "Here is the answer.\n<think>\nSTEP 1: Analyze user input.\nSTEP 2: Formulate response.\n</think>\nFinal answer."
    formatted = format_thinking_content(raw)
    print(f"Original:\n{raw}\n\nFormatted:\n{formatted}")
    
    expected = "Here is the answer.\n> **Thought Process**\n> STEP 1: Analyze user input.\n> STEP 2: Formulate response.\n\n\nFinal answer."
    assert expected in formatted
    print("Test 1 Passed!")

    print("\n--- Test 2: Telegram V2 Integration ---")
    rendered = format_for_telegram_v2(raw)
    print(f"Rendered V2:\n{rendered}")
    # Check if > is escaped as \> and newlines are preserved
    assert "\\> *Thought Process*" in rendered
    print("Test 2 Passed!")

if __name__ == "__main__":
    test_thinking_formatter()
