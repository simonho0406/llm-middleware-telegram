"""
Unit tests for bot.response_generator._parse_xml_tool_calls.

NVIDIA nemotron models emit tool calls as XML rather than JSON. This pure parser
converts that XML into the same dict shape the JSON tool-call path produces, so the
agentic loop can treat both uniformly. Previously untested.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.response_generator import _parse_xml_tool_calls


def test_single_call_with_parameters():
    text = (
        "<tool_call><function=sqlite-tools__read_query>"
        "<parameter=query>SELECT 1</parameter>"
        "<parameter=limit>5</parameter>"
        "</function></tool_call>"
    )
    calls = _parse_xml_tool_calls(text)
    assert calls == [{
        "id": "xml_tc_0",
        "function": {
            "name": "sqlite-tools__read_query",
            "arguments": {"query": "SELECT 1", "limit": "5"},
        },
    }]


def test_multiple_calls_get_incrementing_ids():
    text = (
        "<tool_call><function=a__one><parameter=x>1</parameter></function></tool_call>"
        "noise between"
        "<tool_call><function=b__two><parameter=y>2</parameter></function></tool_call>"
    )
    calls = _parse_xml_tool_calls(text)
    assert [c["id"] for c in calls] == ["xml_tc_0", "xml_tc_1"]
    assert calls[0]["function"]["name"] == "a__one"
    assert calls[1]["function"]["arguments"] == {"y": "2"}


def test_block_without_function_tag_is_skipped():
    text = "<tool_call>just some prose, no function tag</tool_call>"
    assert _parse_xml_tool_calls(text) == []


def test_call_with_no_parameters_yields_empty_args():
    text = "<tool_call><function=server__ping></function></tool_call>"
    calls = _parse_xml_tool_calls(text)
    assert calls == [{"id": "xml_tc_0", "function": {"name": "server__ping", "arguments": {}}}]


def test_no_tool_call_tags_returns_empty():
    assert _parse_xml_tool_calls("plain assistant response, no tools") == []
    assert _parse_xml_tool_calls("") == []


def test_parameter_values_are_whitespace_trimmed():
    text = (
        "<tool_call><function=s__t>"
        "<parameter=q>   padded value   </parameter>"
        "</function></tool_call>"
    )
    calls = _parse_xml_tool_calls(text)
    assert calls[0]["function"]["arguments"]["q"] == "padded value"
