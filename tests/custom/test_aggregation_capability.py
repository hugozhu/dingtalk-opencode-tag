#!/usr/bin/env python3
"""test_aggregation_capability.py — 群消息聚合能力单测（custom）

覆盖：缓冲、数量上限立即 flush、窗口 flush、prompt 组装、单聊放行、防回环、去重、
默认关闭。mock 定时器 + brain + send_reply。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.builtin_caps import aggregation as A
from core.capabilities import Capability
from core.inbound import InboundMessage, KIND_TEXT


class TestDefaultOff(unittest.TestCase):
    def test_registered_but_off_by_default(self):
        self.assertFalse(A.CAPABILITY.default_enabled)
        self.assertEqual(A.CAPABILITY.priority, 90)   # 先于 text_reply(100)


class TestBuffering(unittest.TestCase):
    def setUp(self):
        A._reset()

    def _msg(self, text, user="张三", conv="cid==", ctype="2", mid=None):
        return InboundMessage(user=user, text=text, conv_type=ctype, conv_id=conv,
                              msg_id=mid or f"m{text}", kind=KIND_TEXT)

    def test_group_text_buffered_consumed(self):
        with patch("threading.Timer"):  # 不真起定时器
            consumed = A.on_inbound(self._msg("你好"))
        self.assertTrue(consumed)                      # 消费掉（不逐条回复）
        self.assertIn("cid==", A._buffers)
        self.assertEqual(len(A._buffers["cid=="]["msgs"]), 1)

    def test_single_chat_passes_through(self):
        # 单聊(convType=1) → 放行给 text_reply
        self.assertFalse(A.on_inbound(self._msg("你好", ctype="1")))

    def test_self_filtered(self):
        with patch.object(A, "_SELF_NAMES", {"opencode"}), patch("threading.Timer"):
            consumed = A.on_inbound(self._msg("你好", user="opencode"))
        self.assertTrue(consumed)                      # 消费
        self.assertNotIn("cid==", A._buffers)          # 但不缓冲

    def test_dedup(self):
        with patch("threading.Timer"):
            A.on_inbound(self._msg("a", mid="dup"))
            A.on_inbound(self._msg("a", mid="dup"))
        self.assertEqual(len(A._buffers["cid=="]["msgs"]), 1)

    def test_max_msgs_triggers_immediate_flush(self):
        with patch("threading.Timer"), \
             patch.object(A, "_AGG_MAX_MSGS", 3), \
             patch.object(A, "submit_handler") as sh:
            for i in range(3):
                A.on_inbound(self._msg(f"m{i}", mid=f"m{i}"))
        # 第 3 条达到上限 → submit_handler(_flush) 被调
        self.assertTrue(any(c.args and c.args[0] is A._flush for c in sh.call_args_list))


class TestFlush(unittest.TestCase):
    def setUp(self):
        A._reset()

    def test_flush_builds_prompt_and_replies(self):
        # 手动塞缓冲
        import time
        A._buffers["cid=="] = {
            "conv_type": "2", "timer": None, "seen": set(),
            "msgs": [("张三", "服务器又挂了", time.time()),
                     ("李四", "我看看日志", time.time())],
        }
        with patch.object(A, "generate_reply", return_value="总结：服务器故障，李四排查中") as gen, \
             patch.object(A, "send_reply") as snd:
            A._flush("cid==")
        gen.assert_called_once()
        prompt = gen.call_args[0][1]
        self.assertIn("服务器又挂了", prompt)
        self.assertIn("我看看日志", prompt)
        self.assertIn("2 条消息", prompt)
        self.assertTrue(gen.call_args.kwargs.get("raw"))     # raw prompt
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")        # 发回群
        self.assertNotIn("cid==", A._buffers)                 # flush 后清空

    def test_flush_empty_noop(self):
        with patch.object(A, "generate_reply") as gen, patch.object(A, "send_reply") as snd:
            A._flush("nonexistent==")
        gen.assert_not_called()
        snd.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
