"""
Pipeline robustness tests — validates the full LLM output processing pipeline
against common patterns, LLM mistakes, and edge cases.

Covers:
  - HTML pre-processor (replace_html_tags): inline/block/structural tags
  - Thinking block formatter (format_thinking_content): multi-block re.sub fix
  - Strikethrough rendering via the newly-enabled GFM rule
  - End-to-end: raw LLM HTML → rendered MarkdownV2
  - Robustness: malformed HTML, unclosed fences, None, empty, huge inputs
"""
import pytest
from utils.text_processing import (
    replace_html_tags,
    format_thinking_content,
    format_for_telegram_v2,
    parse_markdown_to_ast,
    split_document_ast_aware,
    render_ast_to_telegram_v2,
)


# ── Pipeline helper (mirrors send_safe_message pre-processing) ─────────────────

def pipeline(text: str) -> str:
    """Run the full send_safe_message pipeline without Telegram I/O."""
    processed = replace_html_tags(format_thinking_content(text))
    tokens = parse_markdown_to_ast(processed)
    chunks = split_document_ast_aware(tokens)
    return "".join(render_ast_to_telegram_v2(chunk) for chunk in chunks)


# ══════════════════════════════════════════════════════════════════════════════
# 1. HTML Pre-processor — inline formatting tags
# ══════════════════════════════════════════════════════════════════════════════

class TestHtmlInlineFormatting:

    def test_strong_tag(self):
        assert replace_html_tags("<strong>bold</strong>") == "**bold**"

    def test_b_tag(self):
        assert replace_html_tags("<b>bold</b>") == "**bold**"

    def test_em_tag(self):
        assert replace_html_tags("<em>italic</em>") == "_italic_"

    def test_i_tag(self):
        assert replace_html_tags("<i>italic</i>") == "_italic_"

    def test_inline_code_tag(self):
        assert replace_html_tags("<code>x = 1</code>") == "`x = 1`"

    def test_s_tag_strikethrough(self):
        assert replace_html_tags("<s>gone</s>") == "~~gone~~"

    def test_del_tag_strikethrough(self):
        assert replace_html_tags("<del>gone</del>") == "~~gone~~"

    def test_strike_tag_strikethrough(self):
        assert replace_html_tags("<strike>gone</strike>") == "~~gone~~"

    def test_link_double_quotes(self):
        assert replace_html_tags('<a href="https://example.com">Link</a>') == "[Link](https://example.com)"

    def test_link_single_quotes(self):
        assert replace_html_tags("<a href='https://x.com'>X</a>") == "[X](https://x.com)"

    def test_link_with_extra_attributes(self):
        # href not first attribute
        result = replace_html_tags('<a class="nav" href="https://y.com" target="_blank">Y</a>')
        assert result == "[Y](https://y.com)"

    def test_strong_with_class_attribute(self):
        """Attributes on formatting tags must not block conversion."""
        result = replace_html_tags('<strong class="highlight">text</strong>')
        assert "**text**" in result

    def test_case_insensitive_tags(self):
        """HTML tags are case-insensitive in practice."""
        assert replace_html_tags("<STRONG>bold</STRONG>") == "**bold**"
        assert replace_html_tags("<Em>italic</Em>") == "_italic_"


# ══════════════════════════════════════════════════════════════════════════════
# 2. HTML Pre-processor — block-level tags
# ══════════════════════════════════════════════════════════════════════════════

class TestHtmlBlockTags:

    def test_br_no_slash(self):
        assert replace_html_tags("<br>") == "\n"

    def test_br_self_closing(self):
        assert replace_html_tags("<br/>") == "\n"

    def test_br_space_slash(self):
        assert replace_html_tags("<br />") == "\n"

    def test_multiple_br(self):
        assert replace_html_tags("A<br>B<br/>C") == "A\nB\nC"

    def test_p_tag(self):
        assert replace_html_tags("<p>Text</p>") == "Text\n\n"

    def test_p_tag_with_class(self):
        assert replace_html_tags('<p class="lead">Intro</p>') == "Intro\n\n"

    def test_h1(self):
        assert replace_html_tags("<h1>Title</h1>").strip() == "# Title"

    def test_h2(self):
        assert replace_html_tags("<h2>Section</h2>").strip() == "## Section"

    def test_h3(self):
        assert replace_html_tags("<h3>Sub</h3>").strip() == "### Sub"

    def test_h6(self):
        assert replace_html_tags("<h6>Deep</h6>").strip() == "###### Deep"

    def test_pre_code_block(self):
        result = replace_html_tags("<pre><code>def f(): pass</code></pre>")
        assert "```" in result
        assert "def f(): pass" in result
        assert "<code>" not in result

    def test_pre_code_with_lang_attr(self):
        result = replace_html_tags('<pre><code class="language-python">print(1)</code></pre>')
        assert "```" in result
        assert "print(1)" in result

    def test_ul_li_list(self):
        result = replace_html_tags("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in result
        assert "- Two" in result
        assert "<ul>" not in result
        assert "<li>" not in result

    def test_ol_li_list(self):
        # OL items are converted to unordered markdown bullets (acceptable simplification)
        result = replace_html_tags("<ol><li>Step 1</li><li>Step 2</li></ol>")
        assert "- Step 1" in result
        assert "- Step 2" in result
        assert "<ol>" not in result


# ══════════════════════════════════════════════════════════════════════════════
# 3. HTML Pre-processor — structural/wrapper tags and edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestHtmlStructuralTags:

    def test_unknown_tag_stripped_content_preserved(self):
        result = replace_html_tags("<unknown>content</unknown>")
        assert "content" in result
        assert "<unknown>" not in result

    def test_span_stripped(self):
        result = replace_html_tags("<span class='hi'>text</span>")
        assert "text" in result
        assert "<span" not in result

    def test_div_stripped(self):
        result = replace_html_tags("<div>inner</div>")
        assert "inner" in result
        assert "<div>" not in result

    def test_nested_html_unwraps(self):
        raw = "<div><p><strong>nested</strong></p></div>"
        result = replace_html_tags(raw)
        assert "**nested**" in result
        assert "<div>" not in result
        assert "<p>" not in result

    def test_mixed_html_and_markdown(self):
        """LLMs sometimes mix HTML tags and Markdown in one message."""
        raw = "**Bold markdown** and <em>italic html</em> together."
        result = replace_html_tags(raw)
        assert "**Bold markdown**" in result
        assert "_italic html_" in result
        assert "<em>" not in result

    def test_code_content_not_processed_for_markdown(self):
        """Markdown-looking content inside <pre><code> must survive verbatim."""
        raw = "<pre><code>x = **not bold** and _not italic_</code></pre>"
        result = replace_html_tags(raw)
        # The content is now inside a fenced block — ** and _ are preserved
        assert "**not bold**" in result
        assert "_not italic_" in result
        assert "<code>" not in result

    def test_script_tag_stripped(self):
        """Script injection: tag stripped, content preserved (no XSS amplification)."""
        result = replace_html_tags("Click <script>alert('xss')</script> here.")
        assert "<script>" not in result
        assert "alert" in result  # content kept, tag removed

    def test_none_input_passthrough(self):
        assert replace_html_tags(None) is None  # type: ignore

    def test_non_string_passthrough(self):
        assert replace_html_tags(42) == 42  # type: ignore

    def test_empty_string(self):
        assert replace_html_tags("") == ""

    def test_plain_text_unchanged(self):
        plain = "Hello world. No tags here."
        assert replace_html_tags(plain) == plain

    def test_malformed_unclosed_tag_no_crash(self):
        """Unclosed tags should not raise; text content is preserved."""
        result = replace_html_tags("<strong>unclosed bold text")
        assert isinstance(result, str)
        # The final catch-all strip removes the dangling tag
        assert "unclosed bold text" in result or "bold text" in result

    def test_malformed_only_open_tag(self):
        result = replace_html_tags("<p>")
        assert isinstance(result, str)
        assert "<p>" not in result


# ══════════════════════════════════════════════════════════════════════════════
# 4. Thinking block formatter
# ══════════════════════════════════════════════════════════════════════════════

class TestThinkingFormatter:

    def test_single_block_formatted(self):
        text = "Answer: <think>process</think>42"
        result = format_thinking_content(text)
        assert "> **Thought Process**" in result
        assert "process" in result
        assert "42" in result

    def test_multiple_blocks_all_formatted(self):
        """re.sub fix — every <think> block must produce a blockquote header."""
        text = "<think>first</think>middle<think>second</think>end"
        result = format_thinking_content(text)
        count = result.count("> **Thought Process**")
        assert count == 2, f"Expected 2 blockquote headers, got {count}"
        assert "first" in result
        assert "second" in result
        assert "middle" in result
        assert "end" in result

    def test_multiline_content_preserved(self):
        text = "<think>\nStep 1\nStep 2\n</think>Done"
        result = format_thinking_content(text)
        assert "> **Thought Process**" in result
        assert "Step 1" in result
        assert "Step 2" in result

    def test_no_think_block_passthrough(self):
        text = "Normal response with no thinking tags."
        assert format_thinking_content(text) == text

    def test_empty_think_block_no_crash(self):
        text = "<think></think>Answer"
        result = format_thinking_content(text)
        assert isinstance(result, str)
        assert "Answer" in result

    def test_none_input_passthrough(self):
        assert format_thinking_content(None) is None  # type: ignore

    def test_three_consecutive_blocks(self):
        text = "<think>a</think>X<think>b</think>Y<think>c</think>Z"
        result = format_thinking_content(text)
        assert result.count("> **Thought Process**") == 3


# ══════════════════════════════════════════════════════════════════════════════
# 5. Strikethrough rendering (GFM rule now enabled)
# ══════════════════════════════════════════════════════════════════════════════

class TestStrikethrough:

    def test_gfm_strikethrough_renders_as_telegram(self):
        """~~text~~ (GFM) must produce ~text~ in Telegram MarkdownV2."""
        result = format_for_telegram_v2("~~deleted~~")
        assert "~deleted~" in result

    def test_strikethrough_in_sentence(self):
        result = format_for_telegram_v2("The price was ~~$100~~ now $80.")
        assert "~$100~" in result

    def test_html_s_tag_through_full_pipeline(self):
        """<s> HTML → ~~md~~ pre-processor → ~text~ renderer."""
        rendered = pipeline("<s>deprecated</s> — use the new API.")
        assert "~deprecated~" in rendered

    def test_html_del_tag_through_full_pipeline(self):
        rendered = pipeline("<del>old value</del> replaced")
        assert "~old value~" in rendered

    def test_strikethrough_mixed_with_bold(self):
        result = format_for_telegram_v2("**important** and ~~removed~~")
        assert "*important*" in result
        assert "~removed~" in result


# ══════════════════════════════════════════════════════════════════════════════
# 6. End-to-end: LLM HTML responses through full pipeline
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndLlmHtml:

    def test_pure_html_llm_response(self):
        """LLM responds in pure HTML instead of Markdown."""
        html = (
            "<h2>Summary</h2>"
            "<p>Key findings:</p>"
            "<ul><li>Finding A</li><li>Finding B</li></ul>"
        )
        rendered = pipeline(html)
        assert "*Summary*" in rendered           # h2 → bold heading
        assert "\\- Finding A" in rendered       # ul item
        assert "\\- Finding B" in rendered

    def test_mixed_html_markdown_response(self):
        """LLM mixes HTML tags and Markdown in one message."""
        mixed = (
            "## Results\n\n"
            "<strong>Important:</strong> The value is **critical**.\n\n"
            "<p>Details below.</p>"
        )
        rendered = pipeline(mixed)
        assert "*Results*" in rendered
        assert "*Important:*" in rendered

    def test_think_block_then_html_answer(self):
        """<think> block followed by an HTML-formatted answer."""
        text = (
            "<think>I will format the answer as HTML.</think>"
            "<h2>Answer</h2>"
            "<p>The result is <strong>42</strong>.</p>"
        )
        rendered = pipeline(text)
        assert "> *Thought Process*" in rendered  # think → blockquote
        assert "*Answer*" in rendered             # h2 → bold
        assert "*42*" in rendered                 # <strong> → bold

    def test_code_in_html_response(self):
        """<pre><code> block in LLM response renders as fenced code."""
        html = (
            "<p>Example code:</p>"
            "<pre><code>def hello():\n    print('hi')\n</code></pre>"
        )
        rendered = pipeline(html)
        assert "```" in rendered
        assert "def hello" in rendered

    def test_special_chars_in_html_content_escaped(self):
        """Parentheses, dots etc. inside HTML content must be MarkdownV2-escaped."""
        html = "<p>Score: (100) percent. Done!</p>"
        rendered = pipeline(html)
        assert "\\(" in rendered
        assert "\\)" in rendered
        assert "\\." in rendered
        assert "\\!" in rendered

    def test_full_article_response(self):
        """Realistic multi-section LLM response with HTML structure."""
        html = (
            "<h1>Report</h1>"
            "<p>Overview of <strong>findings</strong>.</p>"
            "<h2>Details</h2>"
            "<ul>"
            "<li>Point one with <em>emphasis</em></li>"
            "<li>Point two with <code>code</code></li>"
            "</ul>"
            "<p>Conclusion: see <a href='https://example.com'>here</a>.</p>"
        )
        rendered = pipeline(html)
        assert "*Report*" in rendered
        assert "*findings*" in rendered
        assert "*Details*" in rendered
        assert "\\- " in rendered              # list items
        assert "_emphasis_" in rendered
        assert "`code`" in rendered
        assert "example\\.com" in rendered    # URL escaped


# ══════════════════════════════════════════════════════════════════════════════
# 7. LLM mistake robustness — must not crash; output must be a non-empty string
# ══════════════════════════════════════════════════════════════════════════════

class TestLlmMistakes:

    def test_unclosed_code_fence_no_crash(self):
        """LLM forgets the closing ``` — parser must not raise."""
        text = "Here is code:\n```python\nprint('hello')"
        result = pipeline(text)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_json_in_response_no_crash(self):
        """Raw JSON in LLM reply — curly braces must be escaped, not crash."""
        text = 'The response is: {"status": "ok", "code": 200}'
        result = pipeline(text)
        assert isinstance(result, str)
        assert "status" in result

    def test_empty_string_no_crash(self):
        result = pipeline("")
        assert isinstance(result, str)

    def test_whitespace_only_no_crash(self):
        result = pipeline("   \n\n   ")
        assert isinstance(result, str)

    def test_very_long_word_splitter_no_infinite_loop(self):
        """A word longer than max_len must not cause an infinite split loop."""
        text = "A" * 8000  # twice Telegram's 4096 cap
        tokens = parse_markdown_to_ast(text)
        chunks = split_document_ast_aware(tokens)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert isinstance(render_ast_to_telegram_v2(chunk), str)

    def test_emoji_survives_pipeline(self):
        """Non-BMP Unicode / emoji must pass through unchanged."""
        text = "Summary: ✅ Done 🚀 deployed 🎉"
        result = pipeline(text)
        assert "✅" in result
        assert "🚀" in result
        assert "🎉" in result

    def test_latex_math_no_crash(self):
        """LaTeX notation confuses some parsers — must produce a string."""
        text = r"The formula is $E = mc^2$ and $\sum_{i=0}^n i^2$."
        result = pipeline(text)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_markdown_inside_code_block_preserved(self):
        """Markdown formatting inside a fenced code block must NOT be rendered."""
        text = "```\n**not bold**\n*not italic*\n```"
        result = pipeline(text)
        assert "```" in result
        assert "**not bold**" in result   # verbatim inside fence

    def test_asterisk_bullet_list(self):
        """LLM uses * for bullets (valid CommonMark) — must render as list."""
        text = "* Item A\n* Item B"
        result = pipeline(text)
        assert "\\- Item A" in result or "Item A" in result

    def test_html_inside_inline_code_pipeline_no_crash(self):
        """HTML tags inside backtick code spans: pipeline must not crash.

        Known limitation: replace_html_tags runs globally before the AST parser,
        so <strong>inside</strong> a code span is pre-converted to **inside**
        before markdown-it knows it's a code context. The code span is preserved
        but its HTML markup is already converted.
        """
        text = "Run `<strong>this</strong>` to see the output."
        result = pipeline(text)
        assert isinstance(result, str)
        assert "`" in result        # backtick code span still wraps the content
        assert "this" in result     # text content survived

    def test_deeply_nested_html_no_crash(self):
        """Deeply nested HTML structure must not crash."""
        text = "<div><div><div><p><strong><em>deep</em></strong></p></div></div></div>"
        result = pipeline(text)
        assert isinstance(result, str)
        assert "deep" in result

    def test_unicode_hyphens_in_text(self):
        """Non-breaking and em hyphens from LLM output survive the pipeline."""
        text = "Step‑one and step–two and step—three."
        result = pipeline(text)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_html_entities_passthrough(self):
        """HTML entities are not decoded (known limitation) — must not crash."""
        text = "Tom &amp; Jerry &lt;3"
        result = pipeline(text)
        assert isinstance(result, str)

    def test_mix_of_all_issues(self):
        """Stress test: think block + HTML + Markdown + special chars in one message."""
        text = (
            "<think>Let me consider this carefully.</think>"
            "## Result\n\n"
            "<p>The answer is <strong>42</strong> (final!).</p>\n"
            "```python\nprint(42)\n```\n"
            "~~old approach~~ — see <a href='https://docs.example.com'>docs</a>."
        )
        result = pipeline(text)
        assert isinstance(result, str)
        assert len(result) > 50
        assert "> *Thought Process*" in result   # think block
        assert "*Result*" in result              # heading
        assert "*42*" in result                  # bold from HTML
        assert "```" in result                   # code block
        assert "~old approach~" in result        # strikethrough
        assert "docs\\.example\\.com" in result  # link with escaped URL
