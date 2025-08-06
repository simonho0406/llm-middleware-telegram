# Code Review & Strategic Roadmap

**Date:** 2025-08-01
**Reviewer:** Gemini-CLI & Human Supervisor

### **1. Current Roadmap**

The project has reached a new level of architectural maturity. The recent development cycle was not just about adding features; it was about hardening the very core of the application against the complex challenges of stateful, asynchronous, multi-agent workflows. The lessons learned have been directly translated into robust code.

*   **Phase 2.1: "Agentic Workshop" (✅ Complete):**  The core iterative panel is built and stable.

*   **Phase 2.2: "The Interactive Workshop" (Immediate Next Step):**
    *   **Goal:** Give the user direct control over the composition and execution of the Expert Panel.
    *   **Concept:**
        1.  **Universal `/cancel`:** Implement a global `/cancel` command that can gracefully stop any long-running operation, starting with the panel discussion. This is a prerequisite for any complex UI.
        2.  **Panel Configuration UI:** Create a new `ConversationHandler` (e.g., `/configure_panel`) that allows the user to select the specific models for the Orchestrator, Proposer, Critic, and Refiner roles. These choices will be saved to the user's thread data in the database.
        3.  **Dynamic Panel Loading:** The `/discuss_panel` command will be updated to load the user's custom configuration for the panel, falling back to the `config.yaml` defaults if none is set.

*   **Phase 3: "Smart & Controllable Chat" (Future Phase):**
    *   **Goal:** Bring optional, user-controlled intelligence to the main chat.
    *   **Concept:** Implement an `/autosearch <on|off> [mode]` command. This will allow users to enable or disable automatic search detection for normal chat and panel discussions independently.

### **2. Key Lessons Learned from V2 Development**

This section preserves the most critical architectural lessons from the last development cycle.

1.  **State Cleanup is Non-Negotiable and Must Be Exhaustive:** For any stateful feature like a `ConversationHandler`, the cleanup and exit paths are more critical than the entry path. Cleanup logic must be triggered by all possible exits, including explicit user commands (`/end_discussion`), timeouts, and internal exceptions.

2.  **External State is Dangerous and Requires Specific Scopes:** State stored on external servers (like Telegram's command menu) is the most dangerous. Any feature that modifies it must have a corresponding cleanup function that precisely targets the same scope (e.g., `BotCommandScopeChat(chat_id)`) to guarantee it can be reset.

3.  **Proactive Task Cancellation is Mandatory:** For any long-running asynchronous operation spawned by a stateful handler, its `asyncio.Task` handle must be stored in the conversation's context. The state cleanup function *must* explicitly find and cancel this task to prevent "zombie" processes.

4.  **The "Happy Path" Fallacy:** We must design and test for "unhappy paths" (timeouts, invalid inputs, restarts) as a mandatory part of our workflow, not as an afterthought.

5.  **Orchestrator-Led Tool Use:** Empowering the Orchestrator to decide when a tool (like web search) is needed, and to specify the tool's parameters as part of its plan, is a superior architectural pattern. It correctly assigns responsibility, centralizes tool control, and increases reliability by leveraging the most capable agent for planning.

### **3. UX Gap Analysis & Next Priorities**

A review from the user's perspective has identified several gaps where the bot's behavior, while technically correct, violates standard user expectations. Closing these gaps is our top priority.

*   **1. Critical Gap: Lack of "Edit" Functionality.**
    *   **Problem:** Users cannot edit their last prompt to fix a typo. The bot ignores the edit and responds to the original, incorrect prompt.
    *   **User Expectation:** Editing a message should cancel the old request and trigger a new one with the corrected text.

*   **2. High-Impact Gap: No Way to "Cancel" a Request.**
    *   **Problem:** A user cannot stop a long-running `/discuss_panel` or a request to a slow model.
    *   **User Expectation:** A `/cancel` command should be available to immediately stop the in-flight operation and clean up the state.

*   **3. Medium-Impact Gap: The Panel Workflow is a "Black Box".**
    *   **Problem:** The user has no visibility into the panel's internal process (the Proposer's draft, the Critic's review, etc.).
    *   **User Expectation:** A "Show/Hide Work" toggle would provide transparency, build trust, and make the feature more powerful and engaging.

*   **4. Low-Impact Gap: Thread Management Lacks Context.**
    *   **Problem:** The `/threads` list does not show any preview of the conversation content.
    *   **User Expectation:** The list should include the last message from each thread to make it easier to identify and navigate conversations.

*   **5. Medium-Impact Gap: Abrupt Conversation Timeouts.**
    *   **Problem:** The current `conversation_timeout` abruptly ends the discussion after 30 minutes of inactivity, which can feel jarring.
    *   **User Expectation:** A more graceful system would first send a warning message (e.g., "This discussion will time out in 5 minutes due to inactivity.") before taking action, giving the user a chance to continue.
