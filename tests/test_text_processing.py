#!/usr/bin/env python3
"""
Unit tests for the Smart Escaper (pure_markdown_to_telegram_v2) function.

These tests will help us rapidly debug the Markdown rendering issues
without needing to run the full bot.
"""
import pytest
import sys
import os

# Add the parent directory to the Python path to import utils
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utils.text_processing import pure_markdown_to_telegram_v2


class TestSmartEscaper:
    """Test suite for the Smart Escaper function."""
    
    def test_simple_text_with_special_characters(self):
        """Test simple text with Telegram special characters that need escaping."""
        
        # Test parentheses (this is failing in production logs)
        result = pure_markdown_to_telegram_v2("This has (parentheses) in it")
        assert "\\(" in result and "\\)" in result, f"Parentheses not escaped: {result}"
        
        # Test dots in version numbers
        result = pure_markdown_to_telegram_v2("Version a.b.c is available")
        assert "a\\.b\\.c" in result, f"Dots not escaped: {result}"
        
        # Test number ranges
        result = pure_markdown_to_telegram_v2("Range 1-100 items")
        assert "1\\-100" in result, f"Hyphens not escaped: {result}"
        
        # Test underscores outside formatting
        result = pure_markdown_to_telegram_v2("file_name.txt")
        assert "file\\_name\\.txt" in result, f"Underscores/dots not escaped: {result}"
        
        # Test plus signs
        result = pure_markdown_to_telegram_v2("C++ programming")
        assert "C\\+\\+" in result, f"Plus signs not escaped: {result}"
        
        # Test equals signs
        result = pure_markdown_to_telegram_v2("x = y + 1")
        assert "x \\= y \\+ 1" in result, f"Equals/plus not escaped: {result}"
    
    def test_valid_markdown_preservation(self):
        """Test that valid Markdown formatting is preserved."""
        
        # Test bold
        result = pure_markdown_to_telegram_v2("This is *bold* text")
        assert "*bold*" in result, f"Bold formatting broken: {result}"
        
        # Test italic
        result = pure_markdown_to_telegram_v2("This is _italic_ text")
        assert "_italic_" in result, f"Italic formatting broken: {result}"
        
        # Test code
        result = pure_markdown_to_telegram_v2("This is `code` text")
        assert "`code`" in result, f"Code formatting broken: {result}"
        
        # Test underline
        result = pure_markdown_to_telegram_v2("This is __underlined__ text")
        assert "__underlined__" in result, f"Underline formatting broken: {result}"
    
    def test_nested_markdown(self):
        """Test nested Markdown formatting."""
        
        # Test bold and italic combined
        result = pure_markdown_to_telegram_v2("This is *bold and _italic_* text")
        # Should preserve the nested structure
        assert "*bold and _italic_*" in result, f"Nested formatting broken: {result}"
        
        # Test bold with code
        result = pure_markdown_to_telegram_v2("*Bold with `code` inside*")
        assert "*Bold with `code` inside*" in result, f"Bold+code nesting broken: {result}"
    
    def test_lists(self):
        """Test list formatting preservation."""
        
        # Test dash lists
        result = pure_markdown_to_telegram_v2("- item 1\n- item 2")
        assert "- item 1" in result and "- item 2" in result, f"Dash lists broken: {result}"
        
        # Test asterisk lists
        result = pure_markdown_to_telegram_v2("* item 1\n* item 2")
        assert "* item 1" in result and "* item 2" in result, f"Asterisk lists broken: {result}"
        
        # Test numbered lists
        result = pure_markdown_to_telegram_v2("1. item one\n2. item two")
        assert "1\\. item one" in result and "2\\. item two" in result, f"Numbered lists broken: {result}"
    
    def test_blockquotes(self):
        """Test blockquote formatting."""
        
        result = pure_markdown_to_telegram_v2("> This is a quote")
        assert "> This is a quote" in result, f"Blockquote broken: {result}"
        
        # Test multi-line blockquotes
        result = pure_markdown_to_telegram_v2("> First line\n> Second line")
        assert "> First line" in result and "> Second line" in result, f"Multi-line blockquotes broken: {result}"
    
    def test_unclosed_malformed_markdown(self):
        """Test handling of malformed/unclosed Markdown."""
        
        # Test unclosed bold
        result = pure_markdown_to_telegram_v2("This has *unclosed bold")
        # Should escape the asterisk since it's not valid formatting
        assert "\\*" in result, f"Unclosed bold not handled: {result}"
        
        # Test unclosed italic
        result = pure_markdown_to_telegram_v2("This has _unclosed italic")
        # Should escape the underscore since it's not valid formatting
        assert "\\_" in result, f"Unclosed italic not handled: {result}"
        
        # Test unmatched code
        result = pure_markdown_to_telegram_v2("This has `unmatched code")
        # Should escape the backtick since it's not valid formatting
        assert "\\`" in result, f"Unclosed code not handled: {result}"
    
    def test_complex_mixed_content(self):
        """Test complex content mixing special chars and markdown."""
        
        text = "Check version 1.2.3 (beta) with *bold* and `code_sample.py` - range 0-100!"
        result = pure_markdown_to_telegram_v2(text)
        
        # Should escape special chars but preserve valid markdown
        assert "1\\.2\\.3" in result, f"Version dots not escaped: {result}"
        assert "\\(" in result and "\\)" in result, f"Parentheses not escaped: {result}"
        assert "*bold*" in result, f"Bold formatting broken: {result}"
        assert "`code_sample.py`" in result, f"Code formatting broken: {result}"
        assert "0\\-100" in result, f"Range dash not escaped: {result}"
    
    def test_edge_cases(self):
        """Test edge cases and boundary conditions."""
        
        # Empty string
        result = pure_markdown_to_telegram_v2("")
        assert result == "", f"Empty string not handled: {result}"
        
        # Only special characters
        result = pure_markdown_to_telegram_v2("().-+_=")
        assert "\\(" in result and "\\)" in result, f"Special chars not escaped: {result}"
        assert "\\." in result and "\\-" in result, f"Dots/dashes not escaped: {result}"
        assert "\\+" in result and "\\_" in result, f"Plus/underscore not escaped: {result}"
        assert "\\=" in result, f"Equals not escaped: {result}"
        
        # Only markdown
        result = pure_markdown_to_telegram_v2("*bold* _italic_ `code`")
        assert "*bold*" in result and "_italic_" in result, f"Pure markdown broken: {result}"
        assert "`code`" in result, f"Code in pure markdown broken: {result}"
    
    def test_url_links_preservation(self):
        """Test that [text](url) Markdown link syntax is preserved correctly."""
        
        # Test basic URL link
        text = "Check out [the documentation](https://example.com/docs)"
        result = pure_markdown_to_telegram_v2(text)
        
        # Should preserve the link structure without escaping parentheses in URL
        assert "[the documentation](https://example.com/docs)" in result, f"Basic URL link broken: {result}"
        
        # Test URL with parentheses (common in Wikipedia links)
        text = "See [Python (programming language)](https://en.wikipedia.org/wiki/Python_(programming_language))"
        result = pure_markdown_to_telegram_v2(text)
        
        # Should preserve the link without escaping parentheses inside the URL
        expected = "[Python (programming language)](https://en.wikipedia.org/wiki/Python_(programming_language))"
        assert expected in result, f"URL with parentheses broken: {result}"
        
        # Test mixed content with URLs
        text = "Version 2.1.0 released! Check [release notes](https://github.com/project/releases/v2.1.0) for details."
        result = pure_markdown_to_telegram_v2(text)
        
        # Should escape version number dots but preserve URL link
        assert "2\\.1\\.0" in result, f"Version dots not escaped: {result}"
        assert "[release notes](https://github.com/project/releases/v2.1.0)" in result, f"URL link broken: {result}"
    
    def test_production_failure_cases(self):
        """Test specific cases that are failing in production logs."""
        
        # This is the exact pattern failing in logs
        text = "Some text (with parentheses) that should work"
        result = pure_markdown_to_telegram_v2(text)
        
        # The logs show: "character '(' is reserved and must be escaped with the preceding '\'"
        # So we need to ensure parentheses are properly escaped
        assert "\\(" in result and "\\)" in result, f"Production parentheses case failed: {result}"
        
        # Test another common failure pattern
        text = "Version 2.1.0 released - check https://example.com/v2.1"
        result = pure_markdown_to_telegram_v2(text)
        
        assert "2\\.1\\.0" in result, f"Version number dots not escaped: {result}"
        assert "\\-" in result, f"Dash not escaped: {result}"
        assert "v2\\.1" in result, f"URL dots not escaped: {result}"