#!/usr/bin/env python3
"""test_digital_employee.py — 数字员工回复链路单测（custom）

覆盖：
  1. brain.generate_reply —— echo 后端规则 + 空输入 + 截断
  2. replier.send_reply —— log 模式（默认，不真发）+ 空/无 conv 兜底
  3. route_reply —— 防回环（过滤自己）+ msgId 去重 + convId 提取 + 派发大脑/发送

不依赖网络/钉钉：proxy 后端和真实发送用 mock/默认 log 模式。
"""

import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain, replier, routes


class TestBrainEcho(unittest.TestCase):
    def test_ping(self):
        self.assertIn("在的", brain.generate_reply("张三", "ping"))

    def test_greeting_includes_user(self):
        self.assertIn("李四", brain.generate_reply("李四", "你好"))

    def test_default_echoes(self):
        self.assertIn("明天开会", brain.generate_reply("王五", "明天开会"))

    def test_empty_returns_empty(self):
        self.assertEqual(brain.generate_reply("x", "   "), "")

    def test_truncation(self):
        with patch.object(brain, "_MAX_REPLY_CHARS", 10):
            out = brain.generate_reply("u", "x" * 100)
            self.assertLessEqual(len(out), 10 + len("…（已截断）"))
            self.assertTrue(out.endswith("…（已截断）"))


class TestReplierLogMode(unittest.TestCase):
    def test_log_mode_returns_true_without_sending(self):
        # 默认 log 模式：不调 subprocess
        with patch.object(replier, "_REPLY_MODE", "log"), \
             patch("subprocess.run") as mock_run:
            self.assertTrue(replier.send_reply("cid123", 2, "hello"))
            mock_run.assert_not_called()

    def test_empty_text(self):
        self.assertFalse(replier.send_reply("cid", 2, "  "))

    def test_no_conv_id(self):
        self.assertFalse(replier.send_reply("", 2, "hi"))

    def test_bot_mode_skips_without_robot_code(self):
        with patch.object(replier, "_REPLY_MODE", "bot"), \
             patch.object(replier, "ROBOT_CODE", "your-robot-code"), \
             patch("subprocess.run") as mock_run:
            self.assertFalse(replier.send_reply("cid", 2, "hi"))
            mock_run.assert_not_called()


class TestRouteReply(unittest.TestCase):
    def setUp(self):
        routes._reply_seen.clear()

    def _line(self, user, text, conv="cidXYZ==", mid="msg1=="):
        return f"[connect] 收到 @{user}: {text} (convType=2 convId={conv} msgId={mid})"

    def test_normal_message_dispatches(self):
        calls = []
        with patch.object(routes, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            routes.route_reply("张三", "你好", "2", self._line("张三", "你好"))
        self.assertEqual(len(calls), 1)
        # args: user, text, conv_type, conv_id, msg_id
        self.assertEqual(calls[0][0], "张三")
        self.assertEqual(calls[0][3], "cidXYZ==")
        self.assertEqual(calls[0][4], "msg1==")

    def test_self_message_filtered(self):
        calls = []
        with patch.object(routes, "_SELF_NAMES", {"数字员工"}), \
             patch.object(routes, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            routes.route_reply("数字员工", "你好", "2", self._line("数字员工", "你好"))
        self.assertEqual(calls, [])  # 自己发的被过滤

    def test_duplicate_msgid_dedup(self):
        calls = []
        with patch.object(routes, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            line = self._line("张三", "你好", mid="dupmsg==")
            routes.route_reply("张三", "你好", "2", line)
            routes.route_reply("张三", "你好", "2", line)
        self.assertEqual(len(calls), 1)  # 第二次去重

    def test_handle_text_reply_calls_brain_and_replier(self):
        with patch.object(routes, "generate_reply", return_value="生成的回复") as g, \
             patch.object(routes, "send_reply", return_value=True) as s:
            routes._handle_text_reply("张三", "问题", "2", "cid==", "msg==")
            g.assert_called_once()
            s.assert_called_once()
            self.assertEqual(s.call_args[0][2], "生成的回复")

    def test_empty_brain_reply_not_sent(self):
        with patch.object(routes, "generate_reply", return_value=""), \
             patch.object(routes, "send_reply") as s:
            routes._handle_text_reply("张三", "x", "2", "cid==", "msg==")
            s.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
