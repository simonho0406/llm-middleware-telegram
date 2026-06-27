# User Stories

The recurring jobs the bot must do well. `scripts/story_qa.py` exercises each against
the live system (real MCP + real LLM). Keep this list and the QA script in sync. The
example phrasings below are illustrative, not transcripts.

| # | Story | Example request | System path | "Acts correct" means |
|---|---|---|---|---|
| 1 | **Panel: deep Notion retrieval + verify** | *"List the exact attributes recorded for each item in my Notion page 'Reference Catalog' — a certain h3 section. Retrieve, then verify with research."* | `/discuss_panel` → orchestrator → workspace pre-query + refinement loop with notion MCP | Final answer is **grounded in the actual page content** (not "content not retrieved" / "Extraction Failure"); grounding does **not** decline across refinement rounds; the same large page is **not re-fetched** every round |
| 2 | **Chat-history mining** | *"Dive into our chat history and pull back the earlier detail I mentioned — look it up, don't guess."* | normal chat → sqlite-tools MCP over the `conversation_history` view | Model issues a `conversation_history` query **scoped to this chat_id AND current thread_id** and surfaces real past content |
| 3 | **Multi-source overview** | *"Brief overview combining my Notion, our chat history, and current public tech news."* | normal chat → notion MCP + sqlite MCP + web `<search>` | A single coherent answer that integrates all three sources; no silent failure |
| 4 | **Real-time / current info (auto-search)** | a weather or current-events question (incl. non-English, e.g. a CJK weather query) | normal chat → model emits `<search>` → search delegation | Auto-search **triggers** (search queries produced) and a reply is delivered — never a silent self-cancel |
| 5 | **Normal chat / long technical reasoning** | open-ended technical Q&A requiring no tools | normal chat (no tools required) | Coherent, non-empty, grounded answer; harness reports no silent failure |

## Notes

- Story 1 is the one that failed in production (three retries before a partial
  answer) and is the primary target of the discuss_panel context-management fix
  (cumulative grounding dossier, token-aware truncation, tool-call dedup).
- Stories 2–3 depend on the `conversation_history` SQLite view and the
  chat+thread-scoped cheat-sheet injected into the system prompt.
- Story 4 depends on the auto-search self-cancel fix (the search path running
  inside the LLM task without cancelling itself).
