import json
import unittest
from unittest.mock import patch

import antigravity_proxy as proxy
import anthropic_compat as compat


class AnthropicConversionTests(unittest.TestCase):
    def test_text_and_system_to_openai(self):
        body = {
            "model": "claude-opus-5-5",
            "max_tokens": 128,
            "system": "rule",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stop_sequences": ["STOP"],
            "tools": [{"name": "Bash", "description": "Run commands", "input_schema": {
                "type": "object", "properties": {"command": {"type": "string"}},
            }}],
        }
        result = compat.anthropic_request_to_openai(body)
        self.assertEqual(result["messages"], [
            {"role": "system", "content": "rule"},
            {"role": "user", "content": "hello"},
        ])
        self.assertEqual(result["max_tokens"], 128)
        self.assertEqual(result["stop"], ["STOP"])
        self.assertEqual(result["tools"][0]["function"]["parameters"]["properties"],
                         {"command": {"type": "string"}})

    def test_mid_conversation_system_from_claude_code(self):
        body = {"model": "claude-opus-5-5", "system": [{"type": "text", "text": "base"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "system", "content": [{"type": "text", "text": "current rules"}]},
        ]}
        result = compat.anthropic_request_to_openai(body)
        self.assertEqual(result["messages"], [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "current rules"},
        ])
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(result, "claude-opus-5-5-medium")
        self.assertEqual(request["request"]["systemInstruction"]["parts"][0]["text"], "base\n\ncurrent rules")

    def test_gemini_target_omits_claude_code_identity_blocks(self):
        body = {"model": "gemini-3.8-flash", "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.305.ebb; cc_entrypoint=sdk-cli;"},
            {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "CWD: /tmp\\nDate: 2026-10-22"},
            {"type": "text", "text": "Reply concisely"},
        ], "messages": [{"role": "user", "content": "pong"}]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        self.assertEqual(result["messages"][0], {"role": "system", "content": body["system"][2]["text"] + "\nReply concisely"})
        self.assertEqual(len(body["system"]), 4)
        original = compat.anthropic_request_to_openai(body)
        self.assertIn("x-anthropic-billing-header:", original["messages"][0]["content"])
        self.assertIn("You are a Claude agent", original["messages"][0]["content"])

    def test_gemini_target_uses_default_after_removing_only_identity(self):
        body = {"model": "gemini-3.8-flash", "system": [
            {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
        ], "messages": [{"role": "user", "content": "pong"}]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(result, "gemini-3.8-flash-tiered", "medium")
        self.assertEqual(request["request"]["systemInstruction"]["parts"][0]["text"], proxy.DEFAULT_SYSTEM_INSTRUCTION)

    def test_gemini_target_normalizes_system_roles_in_messages(self):
        body = {"model": "gemini-3.8-flash", "system": "Preserve this rule", "messages": [
            {"role": "system", "content": [{"type": "text", "text":
                "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]},
            {"role": "user", "content": "pong"},
        ]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(result, "gemini-3.8-flash-tiered", "medium")
        self.assertEqual(request["request"]["systemInstruction"]["parts"][0]["text"], "Preserve this rule")

    def test_gemini_target_preserves_instructions_after_billing_header(self):
        body = {"model": "gemini-3.8-flash", "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.305.ebb; cc_entrypoint=sdk-cli;\nNever disclose passwords"},
            {"type": "text", "text": "Preserve this rule"},
        ], "messages": [{"role": "user", "content": "pong"}]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        self.assertEqual(result["messages"][0]["content"], "Never disclose passwords\nPreserve this rule")

    def test_gemini_target_keeps_unknown_billing_line_with_rule(self):
        text = "x-anthropic-billing-header: cc_version=2.1.305.ebb; cc_entrypoint=sdk-cli; Never disclose passwords"
        body = {"model": "gemini-3.8-flash", "system": [{"type": "text", "text": text}],
                "messages": [{"role": "user", "content": "pong"}]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        self.assertEqual(result["messages"][0]["content"], text)

    def test_gemini_target_filters_exact_identity_in_string_system(self):
        body = {"model": "gemini-3.8-flash",
                "system": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
                "messages": [{"role": "user", "content": "pong"}]}
        result = compat.anthropic_request_to_openai(body, gemini_target=True)
        self.assertEqual(result["messages"], [{"role": "user", "content": "pong"}])
        self.assertEqual(compat.anthropic_request_to_openai(body)["messages"][0]["content"], body["system"])

    def test_tool_roundtrip_preserves_name_id_signature(self):
        body = {
            "model": "claude-opus-5-5", "max_tokens": 128,
            "messages": [
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_f1|sig1", "name": "Bash", "input": {"command": "pwd"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_f1|sig1", "content": "/tmp"},
                ]},
            ],
        }
        translated = compat.anthropic_request_to_openai(body)
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(translated, "claude-opus-5-5-medium")
        model_part = request["request"]["contents"][1]["parts"][0]
        result_part = request["request"]["contents"][2]["parts"][0]
        self.assertEqual(model_part["functionCall"], {"name": "Bash", "args": {"command": "pwd"}, "id": "f1"})
        self.assertEqual(model_part["thoughtSignature"], "sig1")
        self.assertEqual(result_part["functionResponse"]["name"], "Bash")
        self.assertEqual(result_part["functionResponse"]["id"], "f1")

    def test_two_same_named_tools_stay_distinct(self):
        body = {"model": "claude-opus-5-5", "max_tokens": 128, "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_f1|sig1", "name": "Bash", "input": {"command": "pwd"}},
                {"type": "tool_use", "id": "call_f2|sig2", "name": "Bash", "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_f2|sig2", "content": "files"},
                {"type": "tool_result", "tool_use_id": "call_f1|sig1", "content": "/tmp"},
            ]},
        ]}
        translated = compat.anthropic_request_to_openai(body)
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(translated, "claude-opus-5-5-medium")
        contents = request["request"]["contents"]
        self.assertEqual([p["functionCall"]["id"] for p in contents[0]["parts"]], ["f1", "f2"])
        self.assertEqual([c["parts"][0]["functionResponse"]["id"] for c in contents[1:]], ["f2", "f1"])
        self.assertEqual([c["parts"][0]["functionResponse"]["name"] for c in contents[1:]], ["Bash", "Bash"])

    def test_response_text_usage_and_stop_reason(self):
        openai = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        response = compat.openai_response_to_anthropic(openai, "claude-opus-5-5")
        self.assertEqual(response["type"], "message")
        self.assertEqual(response["role"], "assistant")
        self.assertEqual(response["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(response["stop_reason"], "end_turn")
        self.assertEqual(response["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_response_tool_use(self):
        openai = {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_f1|sig1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'},
            }]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        }
        response = compat.openai_response_to_anthropic(openai, "claude-opus-5-5")
        self.assertEqual(response["content"], [{"type": "tool_use", "id": "call_f1|sig1", "name": "Bash", "input": {"command": "pwd"}}])
        self.assertEqual(response["stop_reason"], "tool_use")

    def test_reject_unsupported_content(self):
        for block in ({"type": "image", "source": {}}, {"type": "unknown", "text": "ignored"}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                compat.anthropic_request_to_openai({"messages": [{"role": "user", "content": [block]}]})

    def test_tool_result_order_matches_user_blocks(self):
        body = {"model": "claude-opus-5-5", "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_f1|sig1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_f1|sig1", "content": "done"},
                {"type": "text", "text": "Explain it"},
            ]},
        ]}
        result = compat.anthropic_request_to_openai(body)
        self.assertEqual([item["role"] for item in result["messages"]], ["assistant", "tool", "user"])
        self.assertEqual(result["messages"][-1]["content"], "Explain it")

    def test_reject_malformed_tool_result_id_and_tool_choice(self):
        for broken in (
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": []}]}]},
            {"messages": [{"role": "user", "content": "hello"}], "tool_choice": "any"},
        ):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                compat.anthropic_request_to_openai(broken)

    def test_reject_unsupported_parallel_tool_restriction(self):
        with self.assertRaises(ValueError):
            compat.anthropic_request_to_openai({"messages": [{"role": "user", "content": "hello"}],
                                                  "tool_choice": {"type": "any", "disable_parallel_tool_use": True}})

    def test_stream_preserves_repeated_incremental_text(self):
        events = [
            {"response": {"candidates": [{"content": {"parts": [{"text": "ha"}]}}]}},
            {"response": {"candidates": [{"content": {"parts": [{"text": "ha"}]}, "finishReason": "STOP"}]}},
        ]
        converted = list(compat.gemini_stream_to_anthropic(events, "claude-opus-5-5"))
        self.assertEqual("".join(event["delta"]["text"] for kind, event in converted
                                 if event.get("delta", {}).get("type") == "text_delta"), "haha")

    def test_stream_uses_last_tool_arguments(self):
        events = [
            {"response": {"candidates": [{"content": {"parts": [
                {"functionCall": {"id": "f1", "name": "Bash", "args": {"command": "p"}}}]}}]}},
            {"response": {"candidates": [{"content": {"parts": [
                {"functionCall": {"id": "f1", "name": "Bash", "args": {"command": "pwd"}}}]},
                "finishReason": "STOP"}]}},
        ]
        converted = list(compat.gemini_stream_to_anthropic(events, "claude-opus-5-5"))
        arguments = "".join(event["delta"]["partial_json"] for kind, event in converted
                            if event.get("delta", {}).get("type") == "input_json_delta")
        self.assertEqual(json.loads(arguments), {"command": "pwd"})

    def test_claude_code_tool_schema_omits_unsupported_gemini_keywords(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {
            "answers": {"type": "object", "propertyNames": {"pattern": "^q"},
                        "additionalProperties": {"type": "string", "exclusiveMinimum": 0}},
            "nested": {"type": "array", "items": {"type": "object", "propertyNames": {
                "pattern": "^key"}, "properties": {"value": {"type": "number", "minimum": 0,
                                                           "exclusiveMinimum": 0, "exclusiveMaximum": 10,
                                                           "unknownKeyword": "ignored"}}}},
        }, "required": ["answers"]}
        original = json.loads(json.dumps(schema))
        body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "AskUserQuestion", "input_schema": schema}]}
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            request = proxy._build_gemini_request(compat.anthropic_request_to_openai(body), "claude-opus-5-5-medium")
        params = request["request"]["tools"][0]["functionDeclarations"][0]["parameters"]
        for unsupported in ("$schema", "propertyNames", "exclusiveMinimum", "exclusiveMaximum", "unknownKeyword"):
            self.assertNotIn(f'"{unsupported}"', json.dumps(params))
        self.assertEqual(params["properties"]["answers"]["additionalProperties"], {"type": "string"})
        self.assertEqual(params["properties"]["nested"]["items"]["properties"]["value"],
                         {"type": "number", "minimum": 0})
        self.assertEqual(params["required"], ["answers"])
        self.assertEqual(schema, original)

    def test_reject_tool_result_without_prior_call(self):
        with self.assertRaises(ValueError):
            compat.anthropic_request_to_openai({"messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "missing", "content": "oops"},
            ]}]})


if __name__ == "__main__":
    unittest.main()
