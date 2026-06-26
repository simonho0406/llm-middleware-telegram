"""
Unit tests for extract_json_object (utils.llm_utilities).

QA surfaced that a single malformed orchestrator plan killed the whole panel turn.
The fix is (1) a string-aware extractor that won't miscount braces inside string
values, and (2) a retry loop in _run_panel_workflow. This locks in (1).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import pytest
from utils.llm_utilities import extract_json_object


def test_plain_object():
    assert json.loads(extract_json_object('{"a": 1, "b": "x"}')) == {"a": 1, "b": "x"}


def test_object_with_prose_around():
    text = 'Sure, here is the plan:\n```json\n{"requires_search": true, "tasks": []}\n```\nHope that helps.'
    assert json.loads(extract_json_object(text)) == {"requires_search": True, "tasks": []}


def test_braces_inside_string_value_do_not_miscount():
    # A naive brace counter would close early at the first '}' inside the string.
    text = '{"query": "find page {A study} now", "n": 2}'
    obj = json.loads(extract_json_object(text))
    assert obj["query"] == "find page {A study} now"
    assert obj["n"] == 2


def test_nested_objects():
    text = 'noise {"a": {"b": {"c": 1}}, "d": [1,2,3]} trailing'
    obj = json.loads(extract_json_object(text))
    assert obj["a"]["b"]["c"] == 1
    assert obj["d"] == [1, 2, 3]


def test_escaped_quote_in_string():
    text = r'{"q": "she said \"hi\"", "k": 1}'
    obj = json.loads(extract_json_object(text))
    assert obj["q"] == 'she said "hi"'


def test_no_json_returns_empty():
    assert extract_json_object("no json here at all") == ""
    assert extract_json_object("") == ""


def test_extracts_first_complete_object():
    text = '{"first": 1} {"second": 2}'
    assert json.loads(extract_json_object(text)) == {"first": 1}


def test_truncated_object_returns_empty():
    # No closing brace anywhere → nothing to extract, returns "" (never raises).
    # This is the exact failure mode the module docstring cites (a cut-off plan).
    assert extract_json_object('{"a": 1, "b":') == ""
    assert extract_json_object('Here is the plan: {"requires_search": true,') == ""


def test_garbage_braces_do_not_raise():
    # Unbalanced/garbage input must degrade to "" rather than throw.
    assert extract_json_object('{{{{') == ""
    assert extract_json_object('}}}}') == ""


def test_balanced_but_invalid_json_is_caller_responsibility():
    # Per the docstring: the extractor returns a brace-balanced substring but does
    # NOT guarantee json.loads succeeds (e.g. unescaped quotes inside a value).
    # The caller is expected to retry the LLM. We pin that contract here.
    text = '{"q": "she said "hi""}'
    extracted = extract_json_object(text)
    assert extracted != "", "a brace-balanced substring should still be returned"
    with pytest.raises(json.JSONDecodeError):
        json.loads(extracted)
