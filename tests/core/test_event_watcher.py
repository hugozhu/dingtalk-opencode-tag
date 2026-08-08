#!/usr/bin/env python3
"""test_event_watcher.py — core event_watcher.py 解析逻辑单测

覆盖：
- parse_sse_events（SSE 流解析）——此前无测试，且最易因 serve 传输编码变化而崩
- _reboot_body（/reboot 通知正文附带 session id/title，#98）

不依赖网络：用假响应对象喂 read1/read；session 信息用 patch 注入。
"""

import os
import socket
import sys
import unittest
from unittest.mock import patch

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


def _task(sid, title, elapsed, conv_id="cid1"):
    """构造 core.brain.list_inflight 形状的一条记录（started 已被 core 归一成 elapsed）。"""
    return {"conv_id": conv_id, "sid": sid, "title": title,
            "started": 0.0, "elapsed": elapsed}


class TestRebootBody(unittest.TestCase):
    """Test _reboot_body — /reboot 通知列出「这一停会打断哪些在跑任务」(#98)."""

    @patch.object(event_watcher, "list_inflight", return_value=[
        _task("ses_abc123456789", "[群] 张三 · 看下这个报错", 125.7),
        _task("ses_def987654321", "[私] 李四 · 帮我查下日志", 8.2, conv_id="cid2"),
    ])
    def test_lists_inflight_tasks(self, _li):
        body = event_watcher._reboot_body()
        self.assertIn("将中断 2 个在跑任务", body)
        # sid 截断到 12 位，够定位又不刷屏
        self.assertIn("`ses_abc12345`", body)
        self.assertIn("`ses_def98765`", body)
        self.assertIn("[群] 张三 · 看下这个报错", body)
        self.assertIn("已跑 125s", body)
        self.assertIn("约 10s 后恢复", body)

    @patch.object(event_watcher, "list_inflight", return_value=[])
    def test_omits_section_when_idle(self, _li):
        # 空闲时整节消失，而不是留一个「将中断 0 个」的空壳
        body = event_watcher._reboot_body()
        self.assertNotIn("在跑任务", body)
        self.assertIn("约 10s 后恢复", body)

    @patch.object(event_watcher, "list_inflight",
                  return_value=[_task("ses_notitle0000", "", 5.0)])
    def test_untitled_task_renders_placeholder(self, _li):
        body = event_watcher._reboot_body()
        self.assertIn("`ses_notitle0`", body)
        self.assertIn("(无标题)", body)

    @patch.object(event_watcher, "_REBOOT_INFLIGHT_MAX", new=3)
    @patch.object(event_watcher, "list_inflight", return_value=[
        _task(f"ses_{i}" + "0" * 10, f"任务{i}", float(i)) for i in range(5)
    ])
    def test_truncates_long_list(self, _li):
        body = event_watcher._reboot_body()
        self.assertIn("将中断 5 个在跑任务", body)
        self.assertIn("…另 2 个", body)
        self.assertIn("任务2", body)
        self.assertNotIn("任务3", body)   # 超出 MAX 的不逐条列

    @patch.object(event_watcher, "list_inflight", side_effect=RuntimeError("boom"))
    def test_registry_failure_does_not_block_reboot(self, _li):
        # 最关键的一条：可观测性绝不能挡住重启本身
        body = event_watcher._reboot_body()
        self.assertIn("约 10s 后恢复", body)
        self.assertNotIn("在跑任务", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
