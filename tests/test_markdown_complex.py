import unittest
from utils.text_processing import split_document_ast_aware, format_for_telegram_v2, md, render_ast_to_telegram_v2

class TestComplexMarkdown(unittest.TestCase):
    def test_nested_lists_split(self):
        # Create a nested list that forces a split
        # 4096 limit. We need ~4000 chars then a split inside a nested list.
        
        prefix = "A" * 3500
        
        # Nested list structure
        # 1. Item 1
        #    - Subitem A
        #    - Subitem B (Split here)
        #    - Subitem C
        # 2. Item 2
        
        markdown_text = prefix + "\n\n"
        markdown_text += "1. Item 1\n"
        markdown_text += "   - Subitem A\n"
        markdown_text += "   - " + ("B" * 1000) + "\n" # This should force a split inside this item
        markdown_text += "   - Subitem C\n"
        markdown_text += "2. Item 2\n"
        
        # Wait, split_document_ast_aware takes TOKENS, not text.
        
        from utils.text_processing import md
        tokens = md.parse(markdown_text)
        chunks = split_document_ast_aware(tokens, max_len=4096)
        
        self.assertTrue(len(chunks) >= 2)
        
        # Verify Chunk 2 starts correctly
        # It should re-open Ordered List (start=1) -> List Item -> Bullet List -> List Item (hidden)
        
        from utils.text_processing import render_ast_to_telegram_v2
        chunk2_text = render_ast_to_telegram_v2(chunks[1])
        
        print(f"\n--- Chunk 2 Preview ---\n{chunk2_text[:200]}...")
        
        # Check for correct re-opening
        # The renderer should NOT show "1. " again if it's inside the item?
        # Wait, if we split INSIDE "Subitem B", we are inside:
        # Ordered List -> Item 1 -> Bullet List -> Subitem B
        
        # So Chunk 2 starts with:
        # Ordered List (start=1)
        #   Item (hidden)
        #     Bullet List
        #       Item (hidden) -> Text (continuation)
        
        # Renderer output for hidden items:
        # render_list_item_open checks hidden -> returns.
        # So no "1. " and no "- ". Just text.
        
        self.assertFalse(chunk2_text.strip().startswith("1\\."))
        self.assertFalse(chunk2_text.strip().startswith("- "))
        self.assertTrue(chunk2_text.strip().startswith("BBBB"))

    def test_code_block_split(self):
        prefix = "A" * 100 # Short prefix
        # Code block MUST be larger than 4096 to force split
        code = "print('Hello World')\n" * 300 # ~6000 chars
        markdown_text = f"{prefix}\n\n```python\n{code}```"
        
        tokens = md.parse(markdown_text)
        chunks = split_document_ast_aware(tokens, max_len=4096)
        
        self.assertTrue(len(chunks) >= 3)
        
        chunk1_text = render_ast_to_telegram_v2(chunks[1])
        chunk2_text = render_ast_to_telegram_v2(chunks[2])
        
        # Chunk 1 (Code Part 1) should end with code block closing
        self.assertTrue(chunk1_text.strip().endswith("```"))
        
        # Chunk 2 should start with code block opening
        self.assertTrue(chunk2_text.strip().startswith("```python"))

if __name__ == '__main__':
    unittest.main()
