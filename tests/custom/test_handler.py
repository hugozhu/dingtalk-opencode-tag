#!/usr/bin/env python3
"""test_handler.py — custom handler.py 纯逻辑单测

覆盖两块此前无测试、最易随外部日志/消息格式变化而崩的逻辑：
  1. render_prompt —— 零 I/O 纯函数（含不改调用方 senders 的纯函数性质）
  2. match_business_line —— 跨行状态机 + 线程安全去重

FDE 改 handler.py 后，这些测试是回归基线。不依赖网络。
"""

import os
import sys
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import handler


class TestRenderPrompt(unittest.TestCase):
    def _body(self, n):
        return {"messages": [{"createTime": f"t{i}", "content": f"c{i}"} for i in range(n)]}

    def test_returns_none_on_empty(self):
        self.assertIsNone(handler.render_prompt({"messages": []}, [], [], "bob"))

    def test_pads_missing_senders(self):
        body = self._body(2)
        atts = [{"time": "t0", "text": "c0"}, {"time": "t1", "text": "c1"}]
        out = handler.render_prompt(body, [], atts, "bob")
        self.assertIn("未知发送人", out)
        self.assertIn("共 2 条", out)

    def test_does_not_mutate_caller_senders(self):
        body = self._body(3)
        atts = [{"time": f"t{i}", "text": f"c{i}"} for i in range(3)]
        senders = ["alice"]
        handler.render_prompt(body, senders, atts, "bob")
        self.assertEqual(senders, ["alice"])  # 纯函数：不改入参

    def test_uses_attachment_text_over_raw(self):
        body = self._body(1)
        atts = [{"time": "t0", "text": "[图片，识别内容]"}]
        out = handler.render_prompt(body, ["alice"], atts, "bob")
        self.assertIn("[图片，识别内容]", out)
        self.assertIn("alice", out)


class TestMatchBusinessLine(unittest.TestCase):
    def setUp(self):
        handler.reset_dedup_state()

    def test_single_line_match(self):
        line = 'stuff msgtype="business-special" more msgId=msgABC end'
        self.assertEqual(handler.match_business_line(line), ("msgABC", []))

    def test_dedup_same_msgid(self):
        line = 'msgtype="business-special" msgId=msgABC'
        self.assertIsNotNone(handler.match_business_line(line))
        self.assertIsNone(handler.match_business_line(line))  # 第二次去重

    def test_non_business_line_ignored(self):
        self.assertIsNone(handler.match_business_line("just a normal log line"))

    def test_cross_line_match(self):
        # 行1 有 msgtype 无 msgId → 暂存；行2 有 msgId → 命中
        self.assertIsNone(handler.match_business_line('msgtype="business-special" no id here'))
        self.assertEqual(handler.match_business_line("next line msgId=msgXYZ"), ("msgXYZ", []))

    def test_thread_safe_dedup(self):
        # 并发喂同一 msgId，只应命中一次
        line = 'msgtype="business-special" msgId=msgRACE'
        hits = []
        lock = threading.Lock()

        def worker():
            r = handler.match_business_line(line)
            if r:
                with lock:
                    hits.append(r)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(hits), 1)


class TestBoundedSeen(unittest.TestCase):
    def test_fifo_eviction(self):
        s = handler._BoundedSeen(3)
        for k in ["a", "b", "c", "d"]:
            s.add(k)
        self.assertNotIn("a", s)  # 最旧被淘汰
        self.assertIn("d", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
