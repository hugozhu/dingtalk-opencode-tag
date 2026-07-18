#!/usr/bin/env python3
"""test_event_watcher.py — core event_watcher.py 解析逻辑单测

覆盖 parse_sse_events（SSE 流解析）——此前无测试，且最易因 serve 传输编码变化而崩。
不依赖网络：用假响应对象喂 read1/read。
"""

import os
import socket
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core import event_watcher


class _FakeResp:
    """假 HTTPResponse：按预置块返回，'TIMEOUT' 触发 socket.timeout。"""
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.i = 0

    def read1(self, n):
        if self.i >= len(self.chunks):
            return b""
        c = self.chunks[self.i]
        self.i += 1
        if c == "TIMEOUT":
            raise socket.timeout()
        return c


class TestParseSSEEvents(unittest.TestCase):
    def setUp(self):
        event_watcher.running = True

    def test_basic_data_lines(self):
        resp = _FakeResp([b'data: {"a":1}\ndata: {"b":2}\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)),
                         ['{"a":1}', '{"b":2}'])

    def test_frame_split_across_reads(self):
        # 一个 data 帧被拆到两次 read
        resp = _FakeResp([b'data: {"b":', b'2}\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), ['{"b":2}'])

    def test_timeout_breaks_for_reconnect(self):
        # timeout 表示 serve 静默过久（心跳都没了）→ socket 已中毒，break 让上层重连。
        # （见 SSE 心跳/超时修复：CPython 带缓冲 socket 超时后无法续读）
        resp = _FakeResp([b'data: one\n', "TIMEOUT", b'data: two\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), ['one'])

    def test_data_without_space(self):
        resp = _FakeResp([b'data:nospace\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), ['nospace'])

    def test_non_data_lines_ignored(self):
        resp = _FakeResp([b'event: message\n:comment\ndata: payload\n\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), ['payload'])

    def test_empty_chunk_ends_stream(self):
        resp = _FakeResp([b'data: last\n'])  # 之后返回 b'' → 结束
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), ['last'])

    def test_stops_when_running_false(self):
        event_watcher.running = False
        resp = _FakeResp([b'data: never\n'])
        self.assertEqual(list(event_watcher.parse_sse_events(resp)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
