#!/usr/bin/env python3
"""test_brain_timeout.py — 活动感知超时 + 用户取消单测（#75）

覆盖 custom/brain.py 的：
  1. 长任务不被杀：POST 阻塞期间 session 持续产出（指纹递增）→ 正常返回，不 abort。
  2. 空闲 abort：POST 阻塞、指纹静止 → 到 IDLE 阈值 abort → generate_reply_ex 返回
     ("", failed) 且**不回退 CLI**。
  3. 总上限 abort：即使持续有活动，超 MAX 也 abort。
  4. 快返回零探测：POST 立即返回时 watchdog 不发 GET /message（证明不破坏现有 mock 测试）。
  5. 兼容：旧 AGENT_OPENCODE_TIMEOUT 作为 IDLE_TIMEOUT 默认。
  6. 用户取消：cancel_inflight + cancel 能力 on_inbound。

不依赖网络：全程 patch brain._serve_request。
"""

import importlib
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain
from core import inbound
from core.brain import STATUS_FAILED

import tempfile
brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test.log")


def _msg_list(textlen, updated):
    """构造 GET /session/{id}/message 的返回：单条 assistant 消息。"""
    return [{"info": {"time": {"updated": updated}},
             "parts": [{"type": "text", "text": "x" * textlen}]}]


class _FakeServe:
    """可配置的假 serve。POST message 阻塞直到 unblock 事件；GET 返回可变/静止指纹。"""

    def __init__(self, *, activity="static", block=True, reply="done"):
        self.activity = activity          # "static" | "growing"
        self.block = block                # POST message 是否阻塞到 unblock
        self.reply = reply
        self.unblock = threading.Event()  # 由 abort 或测试主动 set 解阻塞
        self.calls = []
        self.get_count = 0
        self._tick = 0
        self.lock = threading.Lock()

    def __call__(self, method, port, pwd, path, body=None, timeout=8):
        with self.lock:
            self.calls.append((method, path))
        if method == "POST" and path == "/session":
            return {"id": "ses_1"}
        if method == "POST" and path.endswith("/abort"):
            self.unblock.set()            # abort 解阻塞正卡住的 POST message
            return {}
        if method == "GET" and path.endswith("/message"):
            with self.lock:
                self.get_count += 1
                self._tick += 1
            if self.activity == "growing":
                return _msg_list(10 * self._tick, 1000 + self._tick)
            return _msg_list(10, 1000)    # static
        if method == "POST" and path.endswith("/message"):
            if self.block:
                self.unblock.wait(timeout=5)
            return {"parts": [{"type": "text", "text": self.reply}]}
        return None


def _ctx(conv_id="cidT"):
    return {"conv_id": conv_id, "conv_type": "2", "msg_id": "m", "user": "u"}


class TestActivityAwareTimeout(unittest.TestCase):
    def setUp(self):
        brain._reset_sessions()

    def _run(self, fake, **overrides):
        cfg = {"_BRAIN": "opencode", "_SESSION_REUSE": True,
               "_OPENCODE_ACTIVITY_POLL": 1, "_OPENCODE_IDLE_TIMEOUT": 300,
               "_OPENCODE_MAX_TIMEOUT": 0, "_OPENCODE_SOCK_TIMEOUT": None}
        cfg.update(overrides)
        cli = MagicMock(return_value="CLI-SHOULD-NOT-RUN")
        with patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=fake), \
             patch.object(brain, "_brain_opencode_cli", cli), \
             patch.multiple(brain, **cfg):
            reply, status = brain.generate_reply_ex("u", "长任务", ctx=_ctx())
        return reply, status, cli

    def test_long_task_not_killed(self):
        """持续产出（指纹递增）→ IDLE 不触发，正常返回。"""
        fake = _FakeServe(activity="growing", block=True)
        # POST 阻塞 0.25s 后由测试解阻塞（模拟任务完成）
        threading.Timer(0.25, fake.unblock.set).start()
        reply, status, cli = self._run(
            fake, _OPENCODE_IDLE_TIMEOUT=10, _OPENCODE_ACTIVITY_POLL=1)
        self.assertEqual(reply, "done")
        self.assertNotIn((("POST"), "/session/ses_1/abort"), fake.calls)
        cli.assert_not_called()

    def test_idle_abort_fails_without_cli(self):
        """指纹静止 → 到 IDLE abort → ('', failed)，不回退 CLI。"""
        fake = _FakeServe(activity="static", block=True)
        with patch.object(brain, "_OPENCODE_ACTIVITY_POLL", 1):
            reply, status, cli = self._run(
                fake, _OPENCODE_IDLE_TIMEOUT=1, _OPENCODE_ACTIVITY_POLL=1)
        self.assertEqual(reply, "")
        self.assertEqual(status, STATUS_FAILED)
        self.assertTrue(any(p.endswith("/abort") for _, p in fake.calls), "应触发 abort")
        cli.assert_not_called()

    def test_max_timeout_abort_even_with_activity(self):
        """持续有活动但超 MAX → 仍 abort。"""
        fake = _FakeServe(activity="growing", block=True)
        reply, status, cli = self._run(
            fake, _OPENCODE_IDLE_TIMEOUT=999, _OPENCODE_MAX_TIMEOUT=1,
            _OPENCODE_ACTIVITY_POLL=1)
        self.assertEqual(status, STATUS_FAILED)
        self.assertTrue(any(p.endswith("/abort") for _, p in fake.calls))
        cli.assert_not_called()

    def test_fast_return_zero_get_probes(self):
        """POST 立即返回时 watchdog 不发 GET（默认 POLL 大，done 早于首次探测）。"""
        fake = _FakeServe(block=False)
        reply, status, cli = self._run(fake)  # POLL 默认由 patch.multiple 未覆盖→用 cfg=1
        # 用大 POLL 复测更稳：POST 立即返回，done 先 set
        self.assertEqual(reply, "done")

    def test_fast_return_zero_get_probes_large_poll(self):
        fake = _FakeServe(block=False)
        reply, status, cli = self._run(fake, _OPENCODE_ACTIVITY_POLL=15)
        self.assertEqual(fake.get_count, 0, "快返回不应有 GET 探测")


class TestLegacyTimeoutCompat(unittest.TestCase):
    def test_legacy_timeout_seeds_idle(self):
        """旧 AGENT_OPENCODE_TIMEOUT 未设 IDLE 时作为其默认值。"""
        env = dict(os.environ)
        env.pop("AGENT_OPENCODE_IDLE_TIMEOUT", None)
        env["AGENT_OPENCODE_TIMEOUT"] = "123"
        try:
            with patch.dict(os.environ, env, clear=True):
                importlib.reload(brain)
                self.assertEqual(brain._OPENCODE_IDLE_TIMEOUT, 123)
        finally:
            with patch.dict(os.environ, dict(os.environ), clear=False):
                importlib.reload(brain)   # 恢复默认，避免影响其他测试文件


class TestCancelCapability(unittest.TestCase):
    def setUp(self):
        with brain._inflight_lock:
            brain._inflight.clear()

    def test_cancel_inflight_hits(self):
        brain._mark_inflight("cidX", "ses_9", 4096, "pw")
        with patch.object(brain, "_serve_request", return_value={}) as sr:
            self.assertTrue(brain.cancel_inflight("cidX"))
        self.assertTrue(any(c.args[3].endswith("/abort") for c in sr.call_args_list))

    def test_cancel_inflight_miss(self):
        self.assertFalse(brain.cancel_inflight("no-such-conv"))

    def test_clear_inflight_sid_guard(self):
        brain._mark_inflight("cidX", "ses_1", 4096, "pw")
        brain._clear_inflight("cidX", sid="ses_OTHER")   # sid 不匹配，不清
        self.assertTrue(brain.cancel_inflight("cidX"))

    def _inbound(self, text, conv_id="cidX"):
        return inbound.InboundMessage(
            kind=inbound.KIND_TEXT, user="u", text=text,
            conv_type="2", conv_id=conv_id, msg_id="m1")

    def test_cancel_cap_consumes_when_inflight(self):
        from custom.capabilities import cancel
        brain._mark_inflight("cidX", "ses_1", 4096, "pw")
        with patch.object(cancel, "send_reply") as sr, \
             patch("custom.brain.cancel_inflight", return_value=True) as ci:
            self.assertTrue(cancel.on_inbound(self._inbound("取消")))
        ci.assert_called_once_with("cidX")
        sr.assert_called_once()

    def test_cancel_cap_passthrough_when_no_inflight(self):
        from custom.capabilities import cancel
        with patch("custom.brain.cancel_inflight", return_value=False):
            self.assertFalse(cancel.on_inbound(self._inbound("取消")))

    def test_cancel_cap_ignores_non_keyword(self):
        from custom.capabilities import cancel
        self.assertFalse(cancel.on_inbound(self._inbound("你好呀")))

    def test_cancel_cap_yields_to_pending_question(self):
        """有待答提问时「取消」放行给 question，不去 abort 在跑任务（#71）。

        「取消」同时是两个能力的关键词，cancel(5) 跑在 question(20) 前面。用户为撤掉提问
        而回「取消」时，若恰好又有在跑任务，cancel 会抢先杀掉那个任务、还把消息吃掉——
        提问依然挂着，用户取消了自己没打算取消的东西。
        """
        from custom.capabilities import cancel
        from core.builtin_caps import question as Q
        brain._mark_inflight("cidX", "ses_1", 4096, "pw")
        with patch.object(Q, "_find_pending_for_conv", return_value=("req_1", {})), \
             patch("custom.brain.cancel_inflight") as ci:
            self.assertFalse(cancel.on_inbound(self._inbound("取消")))
        ci.assert_not_called()          # 在跑任务没被误杀

    def test_cancel_cap_acts_when_no_pending_question(self):
        """无待答提问 → 维持原行为：正常 abort 在跑任务（放行逻辑不能过度生效）。"""
        from custom.capabilities import cancel
        from core.builtin_caps import question as Q
        brain._mark_inflight("cidX", "ses_1", 4096, "pw")
        with patch.object(Q, "_find_pending_for_conv", return_value=(None, None)), \
             patch.object(cancel, "send_reply"), \
             patch("custom.brain.cancel_inflight", return_value=True) as ci:
            self.assertTrue(cancel.on_inbound(self._inbound("取消")))
        ci.assert_called_once_with("cidX")


if __name__ == "__main__":
    unittest.main(verbosity=2)
