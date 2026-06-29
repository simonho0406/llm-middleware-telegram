"""
Tests for transient-server-error classification (P1.2). A 503/500/504 from a provider is
transient upstream overload and should be retried with backoff, NOT surfaced to the user
as a hard error — while genuine terminal conditions (quota 429, token overflow) are not
mis-classified as retryable.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from services.gemini_service import _is_retryable_server_error


@pytest.mark.parametrize("msg", [
    "503 UNAVAILABLE. The model is overloaded.",
    "This model is currently experiencing high demand",
    "500 Internal error, please try again later",
    "504 Gateway Timeout",
    "Service UNAVAILABLE",
])
def test_transient_server_errors_are_retryable(msg):
    assert _is_retryable_server_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "429 RESOURCE_EXHAUSTED quota exceeded",
    "You exceeded your current quota",
    "generate_content_free_tier_input_token_count exhausted",
    "400 invalid request",
    "permission denied",
])
def test_quota_and_client_errors_are_not_retryable(msg):
    # 429/quota are handled by key rotation, not the server-error retry; client errors
    # are terminal. Misclassifying these as retryable would waste attempts/quota.
    assert _is_retryable_server_error(Exception(msg)) is False
