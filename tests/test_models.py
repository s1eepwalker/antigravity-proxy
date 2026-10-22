import unittest
from unittest.mock import patch

import antigravity_proxy as proxy


class ModelMappingTests(unittest.TestCase):
    def test_current_model_map_and_legacy_aliases(self):
        models = {
            "gemini-4-argon": "gemini-4-argon-medium",
            "gemini-3.8-flash": "gemini-3.8-flash-tiered",
            "claude-opus-5-5": "claude-opus-5-5-medium",
            "claude-sonnet-5-5": "claude-sonnet-5-5-medium",
        }
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            for name, backend in models.items():
                with self.subTest(model=name):
                    result = proxy._build_passthrough_envelope(name, {"contents": []})
                    self.assertEqual(result["model"], backend)
            old = proxy._build_passthrough_envelope("claude-opus-4.6", {"contents": []})
        self.assertEqual(old["model"], "claude-opus-4-6-thinking")
        self.assertEqual(proxy.DEFAULT_MODEL, "gemini-3.8-flash")

    def test_flash_medium_uses_tiered_backend_with_medium_thinking(self):
        backend, level = proxy.MODEL_MAP["gemini-3.8-flash"]
        with patch.object(proxy, "get_project_id", return_value="test-project"):
            result = proxy._build_gemini_request({"messages": [{"role": "user", "content": "pong"}]}, backend, level)
        self.assertEqual(result["model"], "gemini-3.8-flash-tiered")
        self.assertEqual(result["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "medium")

    def test_upstream_urls_match_current_agy_api(self):
        self.assertEqual(proxy.LOAD_CODEASSIST_URL,
                         "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist")
        self.assertEqual(proxy.GENERATE_CONTENT_URL,
                         "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent")
        self.assertEqual(proxy.STREAM_GENERATE_CONTENT_URL,
                         "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse")



if __name__ == "__main__":
    unittest.main()
