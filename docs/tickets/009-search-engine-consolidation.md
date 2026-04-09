# Ticket 009: Unify / Cleanup Search Engine Integration

## Problem
Currently, the middleware codebase might reference or have dangling code for the `Tavily` search engine, but in production, we are exclusively utilizing `Google Custom Search Engine`. To maintain a clean architecture, we need to decide whether to completely deprecate Tavily and remove its code, or fully support it as an alternative search provider configured in `config.yaml`.

## Architecture Guidelines (Immutable)
- **Configuration-Driven:** The chosen search provider must be dictated entirely by `config.yaml` / `providers.yaml`.
- **Stateless Class-Based:** If we keep multiple search engines, they must adhere to a common interface (e.g., `BaseSearchEngine`).

## Required Changes
1. **Audit `services/search_service.py`**:
   - Determine where Tavily code overlaps with Google Search.
2. **Refactor or Remove**:
   - Option A: Remove all Tavily references, standardizing the search wrapper purely on Google.
   - Option B: Wrap both in a unified factory (e.g. `SearchEngineFactory.get_engine(config.SEARCH_PROVIDER)`).

## Verification
- Run a `/search hello world` command via telegram and ensure no crashes or fallback failures occur.
