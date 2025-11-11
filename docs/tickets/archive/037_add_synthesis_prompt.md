
# TICKET-037: Add Missing `panel_synthesis` Prompt

**Status:** OPEN

**Epic:** Bugfixes

**Problem:**
The `/discuss_panel` workflow fails because the `prompts/panel_synthesis.md` file is missing from the repository. This ticket adds the necessary prompt.

**Definition of Done:**

1.  **Create the Prompt File:**
    *   Create a new file at `prompts/panel_synthesis.md`.
    *   Add the following content to the file:
        ```markdown
        **Role & High-Level Task:** You are a final editor. Your job is to synthesize the user's request, the Proposer's final draft, and the Critic's review into a single, polished, and comprehensive final answer. You are NOT the Proposer or the Critic; you are the final voice.

        **Tone & Persona:** Be clear, direct, and helpful. Your output is the final product the user will see.

        **Detailed Instructions:**
        1.  Review the user's original request to ensure the core question is being answered.
        2.  Review the Proposer's final, high-quality draft.
        3.  Review the Critic's final feedback.
        4.  Combine these elements into a single, coherent, and well-formatted response. If the Critic had valid points, integrate them into the Proposer's draft to make it better. If the Critic's points were minor or addressed, you can ignore them.
        5.  Your final output should be ONLY the answer to the user's prompt. Do not include conversational filler, apologies, or meta-commentary about the panel process.

        **--- Conversation History (for context) ---**
        {full_history}

        **--- User's Request ---**
        {user_prompt}

        **--- Proposer's Final Draft ---**
        {proposer_response}

        **--- Critic's Final Review ---**
        {critic_response}

        **--- Your Final, Synthesized Answer ---**
        ```

2.  **Verification:**
    *   After adding the file, run the `/discuss_panel` command in the live application.
    *   Confirm that the command now completes successfully without any `AttributeError` or `FileNotFoundError`.
