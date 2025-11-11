
# TICKET-002: Fix `NotImplementedError` in Markdown Renderer

**Status:** CLOSED (Superseded)

**Problem:** The application crashes if an LLM returns any raw HTML, because our custom renderer in `utils/text_processing.py` does not know how to handle `HTMLSpan` tokens. A robust renderer must handle all possible token types.

**Evidence:**
```
NotImplementedError: no render method set for HTMLSpan()
```

**Definition of Done:**
1. In `utils/text_processing.py`, find the `TelegramV2Renderer` class.
2. Add a new method to the class to handle `HTMLSpan` tokens by ignoring them and rendering nothing. The method should be:
   ```python
   def render_html_span(self, token):
       return ""
   ```
3. Add the new method to the `render_map` in the `__init__` function of the `TelegramV2Renderer` class: `'HTMLSpan': self.render_html_span`.
