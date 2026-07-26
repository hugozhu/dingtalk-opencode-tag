#!/usr/bin/env python3
"""test_task_stats.py — 单次任务统计推送单测（#76）

覆盖：
- brain.format_task_stats：耗时/工具调用/tokens/推理/缓存命中率 的展示与省略规则
- brain 暂存/取出：_stash_task_stats / pop_task_stats（有界、pop 清除）
- task_stats.on_reply_sent：ok+有暂存→send_notice 推送；失败/无暂存/群聊(O2O_ONLY)→不推送
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain
from custom.capabilities import task_stats


class TestFormatTaskStats(unittest.TestCase):
    def test_full_fields_and_cache_hit_rate(self):
        rec = {"elapsed": 12.34, "usage": {
            "input_tokens": 1000, "output_tokens": 500, "reasoning_tokens": 300,
            "cache_read": 4000, "cache_write": 0, "tool_calls": 3}}
        out = brain.format_task_stats(rec)
        self.assertIn("本次任务统计", out)
        self.assertIn("12.3s", out)
        self.assertIn("工具调用:** 3", out)
        self.assertIn("输入 1.0K↑", out)
        self.assertIn("输出 500↓", out)
        self.assertIn("推理:** 300", out)
        # 缓存命中率 = 4000/(1000+4000) = 80.0%
        self.assertIn("缓存命中:** 80.0%", out)

    def test_omits_zero_optionals(self):
        rec = {"elapsed": 1.0, "usage": {"input_tokens": 10, "output_tokens": 5}}
        out = brain.format_task_stats(rec)
        self.assertNotIn("工具调用", out)     # tool_calls=0 省略
        self.assertNotIn("推理", out)         # reasoning=0 省略
        self.assertNotIn("缓存命中", out)     # cache_read=0 省略
        self.assertIn("耗时:** 1.0s", out)

    def test_empty_returns_none(self):
        self.assertIsNone(brain.format_task_stats(None))


class TestStashPop(unittest.TestCase):
    def setUp(self):
        with brain._last_task_stats_lock:
            brain._last_task_stats.clear()

    def test_stash_then_pop_once(self):
        brain._stash_task_stats("cX", {"input_tokens": 5}, 2.0)
        rec = brain.pop_task_stats("cX")
        self.assertEqual(rec["usage"]["input_tokens"], 5)
        self.assertEqual(rec["elapsed"], 2.0)
        self.assertIsNone(brain.pop_task_stats("cX"))   # pop 后清除

    def test_stash_empty_conv_noop(self):
        brain._stash_task_stats("", {"input_tokens": 5}, 1.0)
        self.assertIsNone(brain.pop_task_stats(""))

    def test_bounded(self):
        for i in range(brain._TASK_STATS_MAX + 20):
            brain._stash_task_stats(f"c{i}", {"input_tokens": i}, 0.1)
        with brain._last_task_stats_lock:
            self.assertLessEqual(len(brain._last_task_stats), brain._TASK_STATS_MAX)


class TestOnReplySent(unittest.TestCase):
    def setUp(self):
        with brain._last_task_stats_lock:
            brain._last_task_stats.clear()

    def test_pushes_when_ok_and_stash_present(self):
        brain._stash_task_stats("cO2O", {"input_tokens": 100, "output_tokens": 50}, 3.0)
        sent = []
        with patch.object(task_stats, "_O2O_ONLY", True), \
             patch("custom.replier.send_notice", lambda cid, ct, text: sent.append((cid, ct, text))):
            task_stats.on_reply_sent("cO2O", "1", True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "cO2O")
        self.assertIn("本次任务统计", sent[0][2])
        self.assertIsNone(brain.pop_task_stats("cO2O"))   # 已取出

    def test_no_push_when_failed(self):
        brain._stash_task_stats("cF", {"input_tokens": 1}, 1.0)
        sent = []
        with patch("custom.replier.send_notice", lambda *a, **k: sent.append(a)):
            task_stats.on_reply_sent("cF", "1", False)
        self.assertEqual(sent, [])
        # 失败不消费暂存（保留，避免丢数据）
        self.assertIsNotNone(brain.pop_task_stats("cF"))

    def test_no_push_when_no_stash(self):
        sent = []
        with patch("custom.replier.send_notice", lambda *a, **k: sent.append(a)):
            task_stats.on_reply_sent("cNone", "1", True)
        self.assertEqual(sent, [])

    def test_group_gated_by_o2o_only(self):
        brain._stash_task_stats("cG", {"input_tokens": 1}, 1.0)
        sent = []
        with patch.object(task_stats, "_O2O_ONLY", True), \
             patch("custom.replier.send_notice", lambda *a, **k: sent.append(a)):
            task_stats.on_reply_sent("cG", "2", True)   # 群聊
        self.assertEqual(sent, [], "O2O_ONLY 时群聊不推送")

    def test_group_allowed_when_o2o_off(self):
        brain._stash_task_stats("cG2", {"input_tokens": 1}, 1.0)
        sent = []
        with patch.object(task_stats, "_O2O_ONLY", False), \
             patch("custom.replier.send_notice", lambda cid, ct, text: sent.append((cid, ct))):
            task_stats.on_reply_sent("cG2", "2", True)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
