# User Stories

Derived from real usage in the thread-history DB (chat 0). These are the
recurring jobs the bot must do well; `scripts/story_qa.py` exercises each against
the live system (real MCP + real LLM). Keep this list and the QA script in sync.

| # | Story | What the user actually said (DB) | System path | "Acts correct" means |
|---|---|---|---|---|
| 1 | **Panel: deep Notion retrieval + verify** | *"List the exact REDACTED — it's in the Notion page 'Reference Catalog', a certain h3 section. Retrieve, then verify with research."* (REDACTED) | `/discuss_panel` → orchestrator → workspace pre-query + refinement loop with notion MCP | Final answer is **grounded in the actual page content** (not "content not retrieved" / "Extraction Failure"); grounding does **not** decline across refinement rounds; the same large page is **not re-fetched** every round |
| 2 | **Chat-history mining** | *"Dive deeper into chat history to see if there's valuable interaction worth pulling back."* / *"Go check the old interaction yourself."* (default thread) | normal chat → sqlite-tools MCP over the `conversation_history` view | Model issues a `conversation_history` query **scoped to this chat_id AND current thread_id** and surfaces real past content |
| 3 | **Multi-source overview** | *"Do a brief overview on my Notion, our chat history, and the overall discussion around the lately REDACTED."* (default thread) | normal chat → notion MCP + sqlite MCP + web `<search>` | A single coherent answer that integrates all three sources; no silent failure |
| 4 | **Real-time / current info (auto-search)** | *"等等REDACTED會下雨嗎"* (will it rain in Taipei), *"REDACTED去REDACTED機票一般多少錢"* (flight prices) | normal chat → model emits `<search>` → search delegation | Auto-search **triggers** (search queries produced) and a reply is delivered — never a silent self-cancel |
| 5 | **Normal chat / long technical reasoning** | REDACTED REDACTED discussion, quant strategy on REDACTED/REDACTED, general Q&A (electronic scales, etc.) | normal chat (no tools required) | Coherent, non-empty, grounded answer; harness reports no silent failure |

## Notes

- Story 1 is the one that failed in production (three retries before a partial
  answer) and is the primary target of the discuss_panel context-management fix
  (cumulative grounding dossier, token-aware truncation, tool-call dedup).
- Stories 2–3 depend on the `conversation_history` SQLite view and the
  chat+thread-scoped cheat-sheet injected into the system prompt.
- Story 4 depends on the auto-search self-cancel fix (the search path running
  inside the LLM task without cancelling itself).
