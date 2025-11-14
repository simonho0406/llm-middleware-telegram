# TICKET-051: Clarify Docker Host Networking for Ollama

**User Story:**
As a user running the bot in Docker and Ollama on my host machine, I want the documentation to clearly explain that I cannot use `localhost` for `OLLAMA_HOST` and must use `host.docker.internal` instead.

**The Problem:**
The current documentation and `.env.example` suggest using `http://localhost:11434` for `OLLAMA_HOST`. This is incorrect in a Docker environment, as `localhost` inside the container refers to the container itself, not the host machine. This leads to connection errors that are confusing for users to diagnose.

**Acceptance Criteria:**
1.  **Update `.env.example`:**
    - The comment for `OLLAMA_HOST` must be updated.
    - It must explicitly state: "If you are running this bot in Docker and Ollama on your host machine, use `http://host.docker.internal:11434`".
    - The default value should be changed to `http://host.docker.internal:11434` to be more helpful to the primary user base.

2.  **Update `README.md`:**
    - A new sub-section or note will be added under the "Configuration" section.
    - This note will be titled "A Note for Docker Users" or similar.
    - It will explain that to connect to services running on the host machine (like Ollama), the address `host.docker.internal` must be used instead of `localhost`.
