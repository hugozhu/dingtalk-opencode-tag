#!/usr/bin/env python3
"""test_digital_employee.py — 数字员工回复链路单测（custom）

覆盖：
  1. brain.generate_reply —— echo 后端规则 + 空输入 + 截断
  2. replier.send_reply —— log 模式（默认，不真发）+ 空/无 conv 兜底
  3. route_reply —— 防回环（过滤自己）+ msgId 去重 + convId 提取 + 派发大脑/发送

不依赖网络/钉钉：proxy 后端和真实发送用 mock/默认 log 模式。
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain, replier, routes
from custom.capabilities import text_reply
from core import inbound

# 测试隔离：把 opencode 调用日志重定向到临时文件，避免污染项目根的运行时 opencode.log
import tempfile
brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test.log")


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


class TestBrainOpencodeCliFallback(unittest.TestCase):
    """opencode 后端的 CLI 回退路径：serve 不可用时走 `opencode run`。

    强制 find_serve_credentials 返回空 → _brain_opencode_http 返回 None → 回退 CLI，
    再 mock subprocess 验证 JSON 事件解析（不依赖真实 opencode / serve）。
    """

    def _events(self, *texts):
        lines = [json.dumps({"type": "step_start", "part": {}})]
        for t in texts:
            lines.append(json.dumps({"type": "text", "part": {"text": t}}))
        lines.append(json.dumps({"type": "step_finish", "part": {}}))
        return "\n".join(lines)

    def test_concatenates_text_events(self):
        fake = MagicMock(returncode=0, stdout=self._events("苹果,", "香蕉,", "橙子"), stderr="")
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(None, None, None)), \
             patch("subprocess.run", return_value=fake):
            self.assertEqual(brain.generate_reply("u", "列水果"), "苹果,香蕉,橙子")

    def test_nonzero_rc_returns_empty(self):
        fake = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(None, None, None)), \
             patch("subprocess.run", return_value=fake):
            self.assertEqual(brain.generate_reply("u", "hi"), "")

    def test_ignores_non_text_events(self):
        fake = MagicMock(returncode=0,
                         stdout=self._events("答案") + "\nnot-json-line", stderr="")
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(None, None, None)), \
             patch("subprocess.run", return_value=fake):
            self.assertEqual(brain.generate_reply("u", "q"), "答案")


class TestBrainOpencodeHttp(unittest.TestCase):
    """opencode 后端的 HTTP 优先路径：走 serve /session，不碰 CLI 子进程。"""

    def _serve_side_effect(self, calls):
        """按 (method, path) 返回假响应，并把调用记进 calls 供断言。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            calls.append((method, path, body))
            if method == "POST" and path == "/session":
                return {"id": "ses_test"}
            if method == "POST" and path.endswith("/message"):
                return {"parts": [{"type": "text", "text": "2"}]}
            return None  # DELETE
        return fake

    def test_http_path_returns_reply_without_cli(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve_side_effect(calls)), \
             patch("subprocess.run") as mock_run:
            self.assertEqual(brain.generate_reply("hugozhu", "1+1"), "2")
            mock_run.assert_not_called()  # HTTP 成功不应回退 CLI

    def test_http_body_has_nested_model_and_system(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_OPENCODE_MODEL", "opencode/deepseek-v4-flash-free"), \
             patch.object(brain, "_SYSTEM_PROMPT", "SYS"), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve_side_effect(calls)):
            brain.generate_reply("hugozhu", "1+1")
        msg_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/message")]
        self.assertEqual(len(msg_calls), 1)
        body = msg_calls[0][2]
        self.assertEqual(body["model"], {"providerID": "opencode", "modelID": "deepseek-v4-flash-free"})
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["parts"][0]["text"], "hugozhu：1+1")
        # 建了 session 就要删（无状态语义，避免堆积）
        self.assertTrue(any(c[0] == "DELETE" for c in calls))

    def test_http_error_falls_back_to_cli(self):
        fake = MagicMock(returncode=0,
                         stdout=json.dumps({"type": "text", "part": {"text": "cli-2"}}),
                         stderr="")
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=RuntimeError("boom")), \
             patch("subprocess.run", return_value=fake) as mock_run:
            self.assertEqual(brain.generate_reply("u", "1+1"), "cli-2")
            mock_run.assert_called_once()  # HTTP 抛错 → 回退 CLI


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
             patch.object(replier, "PROFILE", "real-profile"), \
             patch("subprocess.run") as mock_run:
            self.assertFalse(replier.send_reply("cid", 2, "hi"))
            mock_run.assert_not_called()

    def test_user_mode_failfast_on_placeholder_profile(self):
        # 真发模式下 PROFILE 仍是占位值 → 提前跳过，不调 dws
        with patch.object(replier, "_REPLY_MODE", "user"), \
             patch.object(replier, "PROFILE", "your-profile"), \
             patch("subprocess.run") as mock_run:
            self.assertFalse(replier.send_reply("cid", 2, "hi"))
            mock_run.assert_not_called()


class TestTextReplyCapability(unittest.TestCase):
    """text_reply 能力：InboundMessage(kind=text) → 防回环 + 去重 + 提交大脑。"""

    def setUp(self):
        text_reply._reply_seen.clear()

    def _msg(self, user, text, conv="cidXYZ==", mid="msg1=="):
        line = f"[connect] 收到 @{user}: {text} (convType=2 convId={conv} msgId={mid})"
        return inbound.parse_line(line)

    def test_normal_message_dispatches(self):
        calls = []
        with patch.object(text_reply, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            consumed = text_reply.on_inbound(self._msg("张三", "你好"))
        self.assertTrue(consumed)
        self.assertEqual(len(calls), 1)
        # args: user, text, conv_type, conv_id, msg_id
        self.assertEqual(calls[0][0], "张三")
        self.assertEqual(calls[0][3], "cidXYZ==")
        self.assertEqual(calls[0][4], "msg1==")

    def test_self_message_filtered(self):
        calls = []
        with patch.object(text_reply, "_SELF_NAMES", {"数字员工"}), \
             patch.object(text_reply, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            consumed = text_reply.on_inbound(self._msg("数字员工", "你好"))
        self.assertTrue(consumed)      # 消费掉（不再往下传）
        self.assertEqual(calls, [])    # 但不提交大脑（自己发的）

    def test_duplicate_msgid_dedup(self):
        calls = []
        with patch.object(text_reply, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            m = self._msg("张三", "你好", mid="dupmsg==")
            text_reply.on_inbound(m)
            text_reply.on_inbound(self._msg("张三", "你好", mid="dupmsg=="))
        self.assertEqual(len(calls), 1)  # 第二次去重

    def test_handle_text_reply_calls_brain_and_replier(self):
        with patch.object(text_reply, "generate_reply", return_value="生成的回复") as g, \
             patch.object(text_reply, "send_reply", return_value=True) as s:
            text_reply._handle_text_reply("张三", "问题", "2", "cid==", "msg==")
            g.assert_called_once()
            s.assert_called_once()
            self.assertEqual(s.call_args[0][2], "生成的回复")

    def test_empty_brain_reply_not_sent(self):
        with patch.object(text_reply, "generate_reply", return_value=""), \
             patch.object(text_reply, "send_reply") as s:
            text_reply._handle_text_reply("张三", "x", "2", "cid==", "msg==")
            s.assert_not_called()

    def test_route_reply_shim_still_works(self):
        """兼容垫片 routes.route_reply 仍能派发（走 InboundMessage → 能力）。"""
        calls = []
        with patch.object(text_reply, "submit_handler",
                          side_effect=lambda fn, *a: calls.append(a)):
            routes.route_reply("张三", "你好", "2", self._msg("张三", "你好").raw_line)
        self.assertEqual(len(calls), 1)


class TestTextreplySessionSuppression(unittest.TestCase):
    """brain 临时 session 的 SSE 事件应被 route_sse_event 抑制（不发业务通知）。"""

    def _evt(self, sid):
        return {"type": "session.idle", "properties": {"sessionID": sid}}

    def test_registered_sid_suppressed(self):
        brain._register_textreply_sid("ses_brain_1")
        self.assertTrue(routes.route_sse_event(self._evt("ses_brain_1"), 4096, "pw"))

    def test_business_sid_not_suppressed(self):
        # 未登记的（合并转发业务）session 照常走 core 默认转发
        self.assertFalse(routes.route_sse_event(self._evt("ses_business_x"), 4096, "pw"))

    def test_empty_sid_not_suppressed(self):
        self.assertFalse(routes.route_sse_event(self._evt(""), 4096, "pw"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
