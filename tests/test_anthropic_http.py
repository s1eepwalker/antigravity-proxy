import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import antigravity_proxy as proxy


class AnthropicHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.project_patch = patch.object(proxy, "get_project_id", return_value="test-project")
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path="/v1/messages", body=None):
        if body is None:
            body = {"model": "claude-opus-5-5", "max_tokens": 128,
                    "messages": [{"role": "user", "content": "hello"}]}
        request = Request(f"http://127.0.0.1:{self.server.server_port}{path}",
                          json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return response.status, response.read().decode()

    def stream_events(self, events):
        with patch.object(proxy, "stream_generate_content", return_value=iter(events)):
            status, raw = self.post(body={"model": "claude-opus-5-5", "max_tokens": 128,
                                          "messages": [{"role": "user", "content": "hello"}], "stream": True})
        parsed = []
        for chunk in raw.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in chunk.splitlines())
            parsed.append((fields["event"], json.loads(fields["data"])))
        return status, parsed

    def test_json_messages(self):
        upstream = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}
        with patch.object(proxy, "call_generate_content", return_value=upstream):
            status, raw = self.post()
        response = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(response["type"], "message")
        self.assertEqual(response["role"], "assistant")
        self.assertEqual(response["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(response["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_stream_text_and_usage(self):
        upstream = [
            {"response": {"candidates": [{"content": {"parts": [{"text": "hel"}]}}]}},
            {"response": {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}},
            {"response": {"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}},
        ]
        status, events = self.stream_events(upstream)
        self.assertEqual(status, 200)
        self.assertEqual([kind for kind, _ in events], [
            "message_start", "content_block_start", "content_block_delta", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ])
        self.assertEqual("".join(e["delta"]["text"] for kind, e in events if kind == "content_block_delta"), "hello")
        self.assertEqual(events[-2][1]["usage"], {"input_tokens": 7, "output_tokens": 3})
        self.assertEqual(events[0][1]["message"]["usage"]["input_tokens"], 0)

    def test_stream_multiple_same_named_tools(self):
        parts = [
            {"functionCall": {"id": "f1", "name": "Bash", "args": {"command": "pwd"}}, "thoughtSignature": "sig1"},
            {"functionCall": {"id": "f2", "name": "Bash", "args": {"command": "ls"}}, "thoughtSignature": "sig2"},
        ]
        upstream = [{"response": {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
                                 "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 8}}}]
        _, events = self.stream_events(upstream)
        starts = [e for kind, e in events if kind == "content_block_start"]
        self.assertEqual([e["index"] for e in starts], [0, 1])
        self.assertEqual([e["content_block"]["id"] for e in starts], ["call_f1|sig1", "call_f2|sig2"])
        arguments = [json.loads(e["delta"]["partial_json"]) for kind, e in events if kind == "content_block_delta"]
        self.assertEqual(arguments, [{"command": "pwd"}, {"command": "ls"}])
        self.assertEqual([e["index"] for kind, e in events if kind == "content_block_stop"], [0, 1])
        self.assertEqual([e["delta"]["stop_reason"] for kind, e in events if kind == "message_delta"], ["tool_use"])

    def test_stream_error_before_and_after_first_chunk(self):
        def before(_):
            raise proxy.UpstreamError("backend unavailable", status=502)
            yield

        with patch.object(proxy, "stream_generate_content", side_effect=before):
            with self.assertRaises(HTTPError) as error:
                self.post(body={"model": "claude-opus-5-5", "max_tokens": 128,
                                "messages": [{"role": "user", "content": "hello"}], "stream": True})
        self.assertEqual(error.exception.code, 502)
        self.assertEqual(json.loads(error.exception.read())["error"]["type"], "api_error")

        def after(_):
            yield {"response": {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}}
            raise proxy.UpstreamError("mid-stream failure", status=502)

        with patch.object(proxy, "stream_generate_content", side_effect=after):
            _, raw = self.post(body={"model": "claude-opus-5-5", "max_tokens": 128,
                                     "messages": [{"role": "user", "content": "hello"}], "stream": True})
        self.assertIn("event: error", raw)
        self.assertNotIn("event: message_stop", raw)
        self.assertIn("mid-stream failure", raw)

    def test_invalid_numeric_parameter_is_anthropic_400(self):
        with self.assertRaises(HTTPError) as error:
            self.post(body={"model": "claude-opus-5-5", "max_tokens": [],
                            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.loads(error.exception.read())["error"]["type"], "invalid_request_error")

    def test_json_excludes_thoughts_and_preserves_token_limit(self):
        upstream = {"candidates": [{"content": {"parts": [
            {"text": "private", "thought": True},
            {"functionCall": {"id": "f1", "name": "Bash", "args": {"command": "pwd"}}},
        ]}, "finishReason": "MAX_TOKENS"}]}
        with patch.object(proxy, "call_generate_content", return_value=upstream):
            _, raw = self.post()
        response = json.loads(raw)
        self.assertEqual(response["content"], [{"type": "tool_use", "id": "call_f1|", "name": "Bash",
                                                "input": {"command": "pwd"}}])
        self.assertEqual(response["stop_reason"], "max_tokens")

    def test_stream_rejects_error_frame_and_unfinished_reply(self):
        with patch.object(proxy, "stream_generate_content", return_value=iter([
            {"error": {"code": 503, "message": "unavailable"}},
        ])):
            with self.assertRaises(HTTPError) as error:
                self.post(body={"model": "claude-opus-5-5", "max_tokens": 128, "stream": True,
                                "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(error.exception.code, 502)
        for upstream in (
            [{"response": {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}}],
            [{"response": {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}},
             {"error": {"code": 503, "message": "unavailable"}}],
        ):
            with self.subTest(upstream=upstream):
                _, events = self.stream_events(upstream)
                self.assertEqual(events[-1][0], "error")
                self.assertNotIn("message_stop", [name for name, _ in events])

    def test_stream_tool_turn_retries_nonstream_after_400(self):
        def rejected(_):
            raise proxy.UpstreamError("tool result rejected", status=400)
            yield

        response = {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}
        with patch.object(proxy, "stream_generate_content", side_effect=rejected), \
             patch.object(proxy, "call_generate_content", return_value=response):
            _, raw = self.post(body={"model": "claude-opus-5-5", "max_tokens": 128, "stream": True,
                                     "messages": [{"role": "assistant", "content": [
                                         {"type": "tool_use", "id": "call_f1|sig", "name": "Bash", "input": {}}]},
                                         {"role": "user", "content": [
                                         {"type": "tool_result", "tool_use_id": "call_f1|sig", "content": "ok"}]}]})
        self.assertIn("event: message_stop", raw)
        self.assertIn('"text": "done"', raw)

    def test_invalid_body_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post(body={"model": "claude-opus-5-5", "max_tokens": 128,
                            "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.loads(error.exception.read())["error"]["type"], "invalid_request_error")

    def test_argon_context_suffix_maps_to_backend_model(self):
        upstream = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        with patch.object(proxy, "call_generate_content", return_value=upstream) as generate:
            status, _ = self.post(body={"model": "gemini-4-argon[1m]", "max_tokens": 128,
                                        "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertEqual(generate.call_args.args[0]["model"], "gemini-4-argon-medium")

    def test_openai_context_suffix_maps_to_backend_model(self):
        upstream = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        with patch.object(proxy, "call_generate_content", return_value=upstream) as generate:
            status, _ = self.post("/v1/chat/completions", {"model": "gemini-4-argon[1m]",
                                                          "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertEqual(generate.call_args.args[0]["model"], "gemini-4-argon-medium")

    def test_count_tokens_includes_cyrillic_and_tool_definitions(self):
        body = {"model": "gemini-4-argon[1m]", "messages": [
            {"role": "user", "content": "я" * 3000}], "tools": [
            {"name": "Bash", "description": "x" * 900, "input_schema": {"type": "object"}}]}
        status, raw = self.post("/v1/messages/count_tokens", body)
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 2200)

    def test_count_tokens_counts_image_without_base64_text(self):
        body = {"model": "gemini-4-argon", "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 30000}}]}]}
        status, raw = self.post("/v1/messages/count_tokens", body)
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 1000)
        self.assertLess(json.loads(raw)["input_tokens"], 5000)

    def test_count_tokens_rejects_invalid_messages(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/v1/messages/count_tokens", {"model": "gemini-4-argon", "messages": "invalid"})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.loads(error.exception.read())["error"]["type"], "invalid_request_error")

    def test_context_overflow_returns_prompt_too_long_error(self):
        overflow = proxy.UpstreamError(
            'Upstream HTTP 400: {"error": {"message": "The input token count exceeds '
            'the maximum number of tokens allowed 1048576."}}', status=400)
        with patch.object(proxy, "call_generate_content", side_effect=overflow):
            with self.assertRaises(HTTPError) as error:
                self.post(body={"model": "gemini-4-argon", "max_tokens": 128,
                                "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(error.exception.code, 400)
        response = json.loads(error.exception.read())
        self.assertEqual(response["error"]["type"], "invalid_request_error")
        self.assertIn("prompt is too long", response["error"]["message"])
        self.assertIn("1048576", response["error"]["message"])

    def test_stream_overflow_with_tool_result_does_not_retry(self):
        overflow = proxy.UpstreamError(
            "The input token count exceeds the maximum number of tokens allowed 1048576.", status=400)

        def rejected(_):
            raise overflow
            yield

        body = {"model": "gemini-4-argon", "max_tokens": 128, "stream": True,
                "messages": [
                    {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "call_f1|sig", "name": "Bash", "input": {}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "call_f1|sig", "content": "ok"}]}]}
        with patch.object(proxy, "stream_generate_content", side_effect=rejected), \
             patch.object(proxy, "call_generate_content") as generate:
            with self.assertRaises(HTTPError) as error:
                self.post(body=body)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.loads(error.exception.read())["error"]["type"], "invalid_request_error")
        generate.assert_not_called()

    def test_unrelated_upstream_400_is_not_context_overflow(self):
        with patch.object(proxy, "call_generate_content", side_effect=proxy.UpstreamError("bad model", status=400)):
            with self.assertRaises(HTTPError) as error:
                self.post()
        response = json.loads(error.exception.read())
        self.assertEqual(response["error"], {"type": "api_error", "message": "bad model"})

    def test_empty_upstream_rejected(self):
        with patch.object(proxy, "call_generate_content", return_value={"candidates": []}):
            with self.assertRaises(HTTPError) as error:
                self.post()
        self.assertEqual(error.exception.code, 502)

    def test_existing_openai_route_unchanged(self):
        upstream = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}
        with patch.object(proxy, "call_generate_content", return_value=upstream):
            status, raw = self.post("/v1/chat/completions", {"model": "gemini-3.8-flash", "messages": [
                {"role": "user", "content": "hello"},
            ]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["object"], "chat.completion")


if __name__ == "__main__":
    unittest.main()
