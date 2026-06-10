# 029 — Partial-failure path in `get_provider_details()` leaks httpx pools

## Severity: Low–Medium

## Problem

`bot/providers.py:get_provider_details` initializes services into the
module-level `_initialized_services` dict as each provider succeeds.
If a *subsequent* provider's constructor raises an exception that is
NOT caught (the inner `try/except` only wraps custom providers, not the
built-in `OpenRouterService`/`GeminiService`), the function aborts mid-
init and never assigns `_provider_details_cache`. The next call re-
enters from scratch, finds the surviving entries in `_initialized_services`,
reuses them, and constructs *new* instances for the rest — but the
previously-created instances for providers that ran in this attempt are
orphaned without an `await service.close()`, holding httpx pools.

## Failure mode

Flaky custom-provider config (e.g. malformed `base_url`) → every init
attempt builds and orphans a Gemini/OpenRouter instance. After enough
retries, FD pressure climbs.

## Fix direction

Wrap the body of `get_provider_details()` in a try/except that
explicitly aclose's any provider added to `_initialized_services` during
the current attempt before re-raising. Or: don't add to
`_initialized_services` until all built-in providers succeeded.
