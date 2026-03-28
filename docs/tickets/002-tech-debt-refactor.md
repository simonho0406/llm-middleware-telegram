# Ticket 002: Code Health & Technical Debt Refactoring

## 1. Goal Description
The purpose of this ticket is to execute a "Code Health" refactoring sprint on the core messaging and routing layers (`bot/response_generator.py`, `bot/handlers/chat.py`, and `utils/text_processing.py`). Static analysis identified a deep structural complexity rating (4.56/10) directly responsible for allowing hidden state leaks and silent parser crashes into production. We must pay down this debt to ensure future feature stability.

## 2. User Review Required
> [!IMPORTANT]
> This refactor will require touching the core `_generate_llm_response` streaming loop and the `MessageHandler` event loops. We must decide if we want to pause all other feature tickets (e.g., further Telegram UI tuning) while this is executed to prevent merge conflicts.

## 3. The Implementation Directives

### Objective A: Eradicate "Broad Exception" Swallowing
**The Problem**: Codeblocks like `TelegramV2Renderer` and `_generate_and_send_response` utilize massive `except Exception as e:` blocks. This suppresses real parser and state errors (like the unexpected inline table tokens), masking bugs by forcing a silent UI fallback instead of failing loud.
**Execution (Hands)**:
1. Replace broad `Exception` catches in text rendering with scoped logic (e.g. `except httpx.NetworkError` or `except KeyError`).
2. Where general failures occur, they should explicitly raise internal subsystem exceptions (e.g., `ASTParsingError`) so the upper framework layer can cleanly decide to retry or notify the user with a specific failure code.

### Objective B: Decompose "God Functions" (Cyclomatic Complexity)
**The Problem**: Functions like `_generate_llm_response` try to parse database history, trim context tokens, connect to LLM APIs via Async generators, capture `<search>` tags, and throttle UI drafts all in ~150 lines. They violate SRP.
**Execution (Hands)**:
1. Extract History Parsing / DB De-duping into a dedicated `ContextBuilder` class/helper.
2. Extract the Auto-Search tag intercept into an `OutputInterceptor` helper.
3. Extract UI Streaming Throttling out of the core generator loop, isolating network ingestion from UI emitting. `response_generator.py` should act as an orchestrator calling these modular utilities, not doing the work inline.

### Objective C: Decouple Circular Routing
**The Problem**: `chat.py` is utilizing dynamic `import` statements natively inside `handle_edited_message` to access `discuss_panel_handler.py`. This indicates handlers are tangling state variables across the architecture (likely the original culprit of `llm_tasks` behaving unpredictably).
**Execution (Hands)**:
1. Extract shared panel state negotiation logic into a neutral `state_manager.py` or `ContextUtility`.
2. Ensure handlers operate functionally on the neutral `chat_data`/`user_data` injected by PTB, and do not directly import each other to execute sub-routines.

## 4. Verification Plan
### Automated Tests
*   Run the full `pytest tests/` battery to ensure 100% passage on existing logic.
*   Run `pylint bot/response_generator.py utils/text_processing.py bot/handlers/chat.py` and raise the threshold score from 4.56/10 to at least **8.0/10** with 0 functional feature regressions.
