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
import urllib.error
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain, replier, routes
from core.builtin_caps import text_reply
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

    def test_raw_skips_user_prefix(self):
        # raw=True：text 已是完整 prompt，不拼 "{user}：" 前缀（合并转发用）
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve_side_effect(calls)):
            brain.generate_reply("hugozhu", "完整的结构化 prompt", raw=True)
        msg_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/message")]
        self.assertEqual(msg_calls[0][2]["parts"][0]["text"], "完整的结构化 prompt")

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


class TestSessionReuse(unittest.TestCase):
    """会话连续性（#56）：AGENT_SESSION_REUSE 开启后同一 conv 复用 serve session。"""

    def setUp(self):
        brain._reset_sessions()

    def _serve(self, calls, sids=None, fail_msg_404=False):
        """假 serve：POST /session 依次发 sids（默认 ses_1/ses_2…），message 回带 sid 的文本。"""
        seq = iter(sids or [f"ses_{i}" for i in range(1, 99)])

        def fake(method, port, pwd, path, body=None, timeout=8):
            calls.append((method, path, body))
            if method == "POST" and path == "/session":
                return {"id": next(seq)}
            if method == "POST" and path.endswith("/message"):
                if fail_msg_404:
                    raise urllib.error.HTTPError(path, 404, "gone", {}, None)
                sid = path.split("/")[2]
                return {"parts": [{"type": "text", "text": f"reply@{sid}"}]}
            return None  # DELETE
        return fake

    def _ctx(self, conv_id="cidA"):
        return {"conv_id": conv_id, "conv_type": "2", "msg_id": "m", "user": "u"}

    def test_reuse_same_conv_no_second_create_no_delete(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            r1 = brain.generate_reply("u", "第一句", ctx=self._ctx())
            r2 = brain.generate_reply("u", "第二句", ctx=self._ctx())
        self.assertEqual(r1, "reply@ses_1")
        self.assertEqual(r2, "reply@ses_1")            # 复用同一 session
        creates = [c for c in calls if c[0] == "POST" and c[1] == "/session"]
        deletes = [c for c in calls if c[0] == "DELETE"]
        self.assertEqual(len(creates), 1)              # 只建一次
        self.assertEqual(len(deletes), 0)              # 复用期间不删

    def test_different_conv_independent_sessions(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            brain.generate_reply("u", "hi", ctx=self._ctx("cidA"))
            brain.generate_reply("u", "hi", ctx=self._ctx("cidB"))
        self.assertEqual(brain._lookup_sid("cidA"), "ses_1")
        self.assertEqual(brain._lookup_sid("cidB"), "ses_2")

    def test_oneshot_mode_creates_and_deletes_each_time(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", False), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            brain.generate_reply("u", "a", ctx=self._ctx())
            brain.generate_reply("u", "b", ctx=self._ctx())
        self.assertEqual(len([c for c in calls if c[1] == "/session"]), 2)
        self.assertEqual(len([c for c in calls if c[0] == "DELETE"]), 2)

    def test_ttl_expiry_rebuilds(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "_SESSION_TTL", 0), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            brain.generate_reply("u", "a", ctx=self._ctx())
            time.sleep(0.01)
            brain.generate_reply("u", "b", ctx=self._ctx())
        # TTL=0 → 第二条视为过期，重建 session
        self.assertEqual(len([c for c in calls if c[1] == "/session"]), 2)

    def test_lru_evicts_and_deletes_remote(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "_SESSION_MAX", 1), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            brain.generate_reply("u", "a", ctx=self._ctx("cidA"))
            brain.generate_reply("u", "b", ctx=self._ctx("cidB"))
        # MAX=1：cidB 挤掉 cidA，cidA 的远端 session (ses_1) 被 DELETE
        self.assertIsNone(brain._lookup_sid("cidA"))
        self.assertEqual(brain._lookup_sid("cidB"), "ses_2")
        self.assertIn(("DELETE", "/session/ses_1", None), calls)

    def test_reset_keyword_forgets_and_confirms(self):
        calls = []
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve(calls)):
            brain.generate_reply("u", "第一句", ctx=self._ctx())
            self.assertEqual(brain._lookup_sid("cidA"), "ses_1")
            reply = brain.generate_reply("u", "/new", ctx=self._ctx())
        self.assertIn("新话题", reply)
        self.assertIsNone(brain._lookup_sid("cidA"))       # 记录已清
        self.assertIn(("DELETE", "/session/ses_1", None), calls)  # 旧 session 删了

    def test_404_rebuilds_once_and_succeeds(self):
        # 复用的 session POST 报 404 → 重建重试成功
        calls = []
        seq = iter(["ses_new"])   # 预置的 ses_old 直接进表，首个 create 发生在重建时
        state = {"first": True}

        def fake(method, port, pwd, path, body=None, timeout=8):
            calls.append((method, path, body))
            if method == "POST" and path == "/session":
                return {"id": next(seq)}
            if method == "POST" and path.endswith("/message"):
                if "ses_old" in path and state["first"]:
                    state["first"] = False
                    raise urllib.error.HTTPError(path, 404, "gone", {}, None)
                return {"parts": [{"type": "text", "text": "ok"}]}
            return None

        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=fake):
            brain._remember_sid("cidA", "ses_old")        # 预置一个"已存在"的复用会话
            reply = brain.generate_reply("u", "hi", ctx=self._ctx())
        self.assertEqual(reply, "ok")
        self.assertEqual(brain._lookup_sid("cidA"), "ses_new")   # 换成新 session

    def test_post_error_forgets_and_falls_back(self):
        # 复用会话 POST 持续失败（非 404）→ 清记录 + 返回 None 走 CLI 回退
        fake = MagicMock(returncode=0,
                         stdout=json.dumps({"type": "text", "part": {"text": "cli"}}),
                         stderr="")
        with patch.object(brain, "_BRAIN", "opencode"), \
             patch.object(brain, "_SESSION_REUSE", True), \
             patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=self._serve([], fail_msg_404=True)), \
             patch("subprocess.run", return_value=fake):
            reply = brain.generate_reply("u", "hi", ctx=self._ctx())
        # 404 但非复用（首次新建即 404）→ 不重试，清记录回退 CLI
        self.assertEqual(reply, "cli")
        self.assertIsNone(brain._lookup_sid("cidA"))


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
    """text_reply 能力：InboundMessage(kind=text) → 提交大脑（防回环+去重由 core 声明式处理）。"""

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

    def test_declares_dedup_and_loop_guard(self):
        # 防回环 + 去重交给 core（见 tests/core/test_capabilities）
        self.assertTrue(text_reply.CAPABILITY.loop_guard)
        self.assertTrue(text_reply.CAPABILITY.dedup)

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
