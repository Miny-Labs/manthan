"""Tests for the OpenAI-compat chat shim (pure converters — no network).

Regression for: chat_loop.py imports `chat` from manthan_agent.llm; the
AI Studio rewrite must keep that interface working end to end.
"""

from __future__ import annotations

import json

from manthan_agent.llm import (
    genai_response_to_shim,
    openai_messages_to_genai,
    openai_tools_to_genai,
)


def test_chat_is_importable_the_way_chat_loop_does():
    # Exactly chat_loop.py line 34.
    from manthan_agent.llm import chat as llm_chat  # noqa: F401


def test_system_and_user_messages_convert():
    system, contents = openai_messages_to_genai([
        {"role": "system", "content": "You are Manthan."},
        {"role": "user", "content": "What happened on case 42?"},
    ])
    assert system == "You are Manthan."
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "What happened on case 42?"


def test_assistant_tool_calls_and_tool_results_pair_by_id():
    system, contents = openai_messages_to_genai([
        {"role": "user", "content": "look it up"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_7",
                "type": "function",
                "function": {"name": "coral_sql", "arguments": json.dumps({"query": "SELECT 1"})},
            }],
        },
        {"role": "tool", "tool_call_id": "call_7", "content": json.dumps({"rows": [{"x": 1}]})},
    ])
    assert system is None
    # assistant turn -> model role with a function_call part
    fc_part = contents[1].parts[0]
    assert fc_part.function_call.name == "coral_sql"
    assert dict(fc_part.function_call.args) == {"query": "SELECT 1"}
    # tool turn -> user role with a function_response named after the call
    fr_part = contents[2].parts[0]
    assert fr_part.function_response.name == "coral_sql"
    assert fr_part.function_response.response == {"rows": [{"x": 1}]}


def test_tools_convert_and_strict_keys_stripped():
    tools = openai_tools_to_genai([{
        "type": "function",
        "function": {
            "name": "record_finding",
            "description": "Assert a claim.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "strict": True,
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }])
    assert tools is not None
    decl = tools[0].function_declarations[0]
    assert decl.name == "record_finding"
    dumped = decl.parameters
    # The strict-mode keys must not survive conversion.
    as_json = json.dumps(dumped, default=lambda o: getattr(o, "__dict__", str(o)))
    assert "additionalProperties" not in as_json
    assert "strict" not in as_json


def test_response_shim_extracts_text_and_tool_calls():
    class FakeFC:
        id = None
        name = "reply"
        args = {"text": "done"}

    class FakePartFC:
        function_call = FakeFC()
        text = None

    class FakePartText:
        function_call = None
        text = "thinking…"

    class FakeContent:
        parts = [FakePartText(), FakePartFC()]

    class FakeCandidate:
        content = FakeContent()

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20

    class FakeResp:
        candidates = [FakeCandidate()]
        usage_metadata = FakeUsage()

    shim = genai_response_to_shim(FakeResp())
    msg = shim.choices[0].message
    assert msg.content == "thinking…"
    assert msg.tool_calls is not None and len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.function.name == "reply"
    assert json.loads(tc.function.arguments) == {"text": "done"}
    assert tc.id == "call_1"
    assert shim.usage.prompt_tokens == 100


def test_empty_response_yields_none_content_no_tool_calls():
    class FakeResp:
        candidates = []
        usage_metadata = None

    shim = genai_response_to_shim(FakeResp())
    msg = shim.choices[0].message
    assert msg.content is None
    assert msg.tool_calls is None
