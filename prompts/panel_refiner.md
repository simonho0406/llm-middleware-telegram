{base_refiner_prompt}

**CRITICAL CONSTRAINT: Do NOT make any tool calls, function calls, or web searches. You are a text-polishing step only — all research has already been completed. Refine only the document text provided below.**

**Instructions: Polish and refine the response below for clarity, style, and readability. Remove all `[UNVERIFIED]` annotation tags from the text — they are internal review markers and must not appear in the final user-facing output. Use standard Markdown formatting:**

**Output Formatting Rules (Telegram Constraints):**
• **NO TABLES:** Telegram cannot render proportional-font markdown tables properly. If the text contains tables, you MUST convert them into structured bulleted or numbered lists.
• Use **bold text** or *italic text* for emphasis
• Use `code snippets` for technical terms and ```code blocks``` for longer code
• Use headings sparingly (never use `#`, use `##` or `###`)
• Write clearly and concisely, prioritizing density of information over conversational filler

**Output only your polished response using clean, standard Markdown:**

--- DOCUMENT TO REFINE ---
{proposer_response}