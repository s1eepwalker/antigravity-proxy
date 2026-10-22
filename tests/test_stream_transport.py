import threading
import unittest
from unittest.mock import patch

import antigravity_proxy as proxy


class StreamTransportTests(unittest.TestCase):
    def test_small_sse_event_arrives_before_upstream_eof(self):
        release = threading.Event()
        received = threading.Event()
        events = []

        class Response:
            def __init__(self):
                self.lines = iter([b'data: {"response":{"candidates":[{"finishReason":"STOP"}]}}\n', b'\n'])

            def readline(self):
                try:
                    return next(self.lines)
                except StopIteration:
                    release.wait(2)
                    return b""

            def read(self, amount):
                release.wait(2)
                return b'data: {"response":{"candidates":[{"finishReason":"STOP"}]}}\n\n'

            def close(self):
                release.set()

        def consume():
            stream = proxy.stream_generate_content({"request": {}})
            try:
                events.append(next(stream))
                received.set()
            except Exception as error:
                events.append(error)
                received.set()
            finally:
                stream.close()

        with patch.object(proxy, "urlopen", return_value=Response()), \
             patch.object(proxy, "get_access_token", return_value="unused"):
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            try:
                self.assertTrue(received.wait(0.5), "small SSE event was buffered until EOF")
                self.assertEqual(events[0]["response"]["candidates"][0]["finishReason"], "STOP")
            finally:
                release.set()
                worker.join(3)

    def test_multiline_sse_waits_for_event_terminator(self):
        self.assertIsNone(proxy._try_parse_one_event('data: {"a":\n'))
        parsed = proxy._try_parse_one_event('data: {"a":\ndata: 1}\n\n')
        self.assertEqual(parsed[0], {"a": 1})


if __name__ == "__main__":
    unittest.main()
