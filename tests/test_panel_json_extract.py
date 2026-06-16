"""
Unit tests for the orchestrator-plan JSON extractor (_extract_json_object).

QA surfaced that a single malformed orchestrator plan killed the whole panel turn.
The fix is (1) a string-aware extractor that won't miscount braces inside string
values, and (2) a retry loop in _run_panel_workflow. This locks in (1).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import pytest
from bot.handlers.discuss_panel_handler import _extract_json_object


def test_plain_object():
    assert json.loads(_extract_json_object('{"a": 1, "b": "x"}')) == {"a": 1, "b": "x"}


def test_object_with_prose_around():
    text = 'Sure, here is the plan:\n```json\n{"requires_search": true, "tasks": []}\n```\nHope that helps.'
    assert json.loads(_extract_json_object(text)) == {"requires_search": True, "tasks": []}


def test_braces_inside_string_value_do_not_miscount():
    # A naive brace counter would close early at the first '}' inside the string.
    text = '{"query": "find page {A study} now", "n": 2}'
    obj = json.loads(_extract_json_object(text))
    assert obj["query"] == "find page {A study} now"
    assert obj["n"] == 2


def test_nested_objects():
    text = 'noise {"a": {"b": {"c": 1}}, "d": [1,2,3]} trailing'
    obj = json.loads(_extract_json_object(text))
    assert obj["a"]["b"]["c"] == 1
    assert obj["d"] == [1, 2, 3]


def test_escaped_quote_in_string():
    text = r'{"q": "she said \"hi\"", "k": 1}'
    obj = json.loads(_extract_json_object(text))
    assert obj["q"] == 'she said "hi"'


def test_no_json_returns_empty():
    assert _extract_json_object("no json here at all") == ""
    assert _extract_json_object("") == ""


def test_extracts_first_complete_object():
    text = '{"first": 1} {"second": 2}'
    assert json.loads(_extract_json_object(text)) == {"first": 1}
