import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils.text_processing import format_for_telegram_v2

class TestTelegramV2Renderer:
    def test_headings(self):
        assert format_for_telegram_v2('# Hello') == '*Hello*'

    def test_bold(self):
        assert format_for_telegram_v2('**bold**') == '*bold*'

    def test_italic(self):
        assert format_for_telegram_v2('*italic*') == '_italic_'

    def test_inline_code(self):
        assert format_for_telegram_v2('`code`') == '`code`'

    def test_links(self):
            md = '[Google](https://google.com)'
            expected = '[Google](https://google\\.com)'
            assert format_for_telegram_v2(md) == expected

    def test_unordered_list(self):
        md = '- one\n- two'
        expected = '\- one\n\- two'
        assert format_for_telegram_v2(md).strip() == expected

    def test_ordered_list(self):
        md = '1. one\n2. two'
        expected = '1\\. one\n2\\. two'
        assert format_for_telegram_v2(md).strip() == expected

    def test_escaping_simple(self):
        md = 'Hello. World! (test)'
        expected = 'Hello\. World\! \(test\)'
        assert format_for_telegram_v2(md) == expected

    def test_code_block(self):
        md = '```python\nprint("hello.world")\n```'
        expected = '```python\nprint("hello.world")\n```'
        assert format_for_telegram_v2(md) == expected

    def test_unordered_list_escaping(self):
        """Test that unordered list markers are properly escaped."""
        md = '- item 1\n- item 2'
        expected = '\\- item 1\n\\- item 2'
        assert format_for_telegram_v2(md).strip() == expected

    def test_ordered_list_dot_escaping(self):
        """Test that the dot in ordered list markers is properly escaped."""
        md = '1. First item\n2. Second item'
        expected = '1\\. First item\n2\\. Second item'
        assert format_for_telegram_v2(md).strip() == expected

    def test_mixed_special_characters_escaping(self):
        """Test that a mix of special characters are correctly escaped."""
        md = 'This is a test with - hyphens, . dots, ! exclamations, (parentheses), [brackets], {braces}, `code`, ~tildes, >quotes, #hashes, +plus, =equals, |pipes.'
        expected = 'This is a test with \\- hyphens, \\. dots, \\! exclamations, \\(parentheses\\), \\[brackets\\], \\{braces\\}, `code`, \\~tildes, \\>quotes, \\#hashes, \\+plus, \\=equals, \\|pipes\\.'
        assert format_for_telegram_v2(md).strip() == expected

    def test_telegram_helpers_escape_markdown_is_correct(self):
        """
        This test validates that the core `telegram.helpers.escape_markdown`
        function behaves exactly as expected for all special characters.
        This is NOT a test of our renderer, but of our core dependency.
        """
        from telegram.helpers import escape_markdown

        # All characters that need escaping in MarkdownV2
        # The backslash is a special case for the test string itself
        raw_string = r'_*[]()~`>#+-=|{}.!'
        
        # The expected result after escaping
        expected_escaped_string = r'\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!'
        
        # Perform the escape
        actual_escaped_string = escape_markdown(raw_string, version=2)
        
        # Assert that the function works as documented
        assert actual_escaped_string == expected_escaped_string

from utils.text_processing import parse_markdown_to_ast, split_document_ast_aware

class TestMessageSplitter:
    def test_splitter_does_not_split_unnecessarily(self):
        """
        Tests that a document shorter than max_len is NOT split and remains as one chunk.
        This is the regression test for the "hyperactive splitting" bug.
        """
        markdown_text = "# Title\n\nThis is the first paragraph.\n\n- Item 1\n- Item 2\n\nThis is the second paragraph."
        
        # Ensure the text is much shorter than the max length
        assert len(markdown_text) < 4096

        # Parse the text into an AST (token stream)
        tokens = parse_markdown_to_ast(markdown_text)

        # Run the splitter
        chunks = split_document_ast_aware(tokens)

        # The document should NOT be split
        assert len(chunks) == 1, "Document was split unnecessarily"
