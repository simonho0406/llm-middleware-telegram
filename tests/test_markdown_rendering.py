import unittest
from utils.text_processing import format_for_telegram_v2

class TestTelegramRendering(unittest.TestCase):
    
    def test_basic_formatting(self):
        markdown = "**Bold** and *Italic* and `Code`"
        expected = "*Bold* and _Italic_ and `Code`"
        self.assertEqual(format_for_telegram_v2(markdown), expected)

    def test_links(self):
        markdown = "[Google](https://google.com)"
        # Telegram V2 escapes dots and hyphens in URLs usually, but our renderer might just escape the text part?
        # Let's check what our renderer actually does. It escapes the URL too.
        # https://google.com -> https://google\.com
        rendered = format_for_telegram_v2(markdown)
        self.assertIn("Google", rendered)
        self.assertIn("google\\.com", rendered)

    def test_headers(self):
        markdown = "# Header 1\n## Header 2"
        # We render headers as bold
        expected = "*Header 1*\n\n*Header 2*\n"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_lists_unordered(self):
        markdown = "- Item 1\n- Item 2"
        expected = "\\- Item 1\n\\- Item 2"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_lists_ordered(self):
        markdown = "1. First\n2. Second"
        expected = "1\\. First\n2\\. Second"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_table_rendering(self):
        markdown = """
| Header 1 | Header 2 |
| --- | --- |
| Cell 1 | Cell 2 |
| Cell 3 | Cell 4 |
"""
        rendered = format_for_telegram_v2(markdown)
        # Check that it's rendered as a code block
        self.assertIn("```", rendered)
        # Check for column padding and alignment
        self.assertIn("| Header 1 | Header 2 |", rendered)
        self.assertIn("| Cell 1   | Cell 2   |", rendered)
        self.assertIn("| Cell 3   | Cell 4   |", rendered)

    def test_blockquote(self):
        markdown = "> Quote line 1\n> Quote line 2"
        expected = "\\> Quote line 1\n\\> Quote line 2"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_code_block(self):
        markdown = "```python\nprint('hello')\n```"
        expected = "```python\nprint('hello')\n```"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_horizontal_rule(self):
        markdown = "---"
        expected = "\\-\\-\\-"
        self.assertEqual(format_for_telegram_v2(markdown).strip(), expected.strip())

    def test_image(self):
        markdown = "![Alt Text](http://image.com/img.png)"
        rendered = format_for_telegram_v2(markdown)
        self.assertIn("[Alt Text]", rendered)
        self.assertIn("http://image\\.com/img\\.png", rendered)

if __name__ == '__main__':
    unittest.main()
