#!/usr/bin/env python3
"""test_subagent_watchdog.py — Task 委派子会话的活动感知超时单测（#125）

2026-09-19 wx post 408 事故回归：主模型把重活委派给 flash-worker 子代理后，
子代理全程跑在独立 child session，主会话指纹静止 → 被 idle>900s 误杀。

覆盖 custom/brain.py `_activity_fingerprint` 的子会话跟踪：
  1. in-flight task part → 递归取子会话指纹拼进第 5 维；子会话活动 → 父指纹变化。
  2. 子会话未完结 reasoning → 向上传播 reasoning_in_progress（保活）。
  3. 子会话 GET 全失败 → 视为仍在产出（保活偏向），不误杀。
  4. 子会话 sid 登记进 textreply 注册表（SSE 业务通知抑制）。
  5. 子代理已完成（completed）的 task part 不再跟踪。
  6. e2e：父静止 + 子持续产出 → watchdog 不 abort，正常返回。
  7. e2e：父静止 + 子也静止 → 仍按 idle abort（不破坏真卡死检测）。

覆盖 custom/capabilities/sse_suppress.py：
  8. 未登记会话的 busy/idle 事件被吞；已登记会话放行给 text_reply；其他事件放行。

不依赖网络：全程 patch brain._serve_request。
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain
from core import brain as core_brain
from core.brain import STATUS_FAILED, STATUS_OK

brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test_subagent.log")


def _task_msg(child_sid, child_status="running", textlen=0, updated=1000):
    """父会话消息：文本 part + 一个 Task 委派 tool part。"""
    return [{"info": {"time": {"updated": updated}},
             "parts": [{"type": "text", "text": "x" * textlen},
                       {"type": "tool", "tool": "task",
                        "state": {"status": child_status, "input": {},
                                  "metadata": {"parentSessionId": "ses_p",
                                               "sessionId": child_sid}}}]}]


def _child_msg(textlen, updated, reasoning_start=None):
    parts = [{"type": "text", "text": "y" * textlen}]
    if reasoning_start:
        parts.append({"type": "reasoning", "text": "",
                      "time": {"start": reasoning_start}})
    return [{"info": {"time": {"updated": updated}}, "parts": parts}]


class TestFingerprintChildTracking(unittest.TestCase):
    def setUp(self):
        brain._reset_sessions()

    def _fp(self, fake, sid="ses_p"):
        with patch.object(brain, "_serve_request", side_effect=fake):
            return brain._activity_fingerprint(4096, "pw", sid)

    def test_child_activity_changes_parent_fp(self):
        """子在跑（running task part）→ 父指纹带第 5 维子指纹，子活动则父指纹变化。"""
        ticks = {"n": 0}

        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c")
            if path == "/session/ses_c/message":
                ticks["n"] += 1
                return _child_msg(10 * ticks["n"], 1000 + ticks["n"])
            return None

        fp1, rip1, _ = self._fp(fake)
        fp2, _, _ = self._fp(fake)
        self.assertEqual(len(fp1), 5)
        self.assertNotEqual(fp1, fp2)          # 子活动 → 父指纹变化
        self.assertFalse(rip1)

    def test_child_static_keeps_fp_static(self):
        """子会话指纹静止 → 父指纹也静止（真卡死仍可被 idle 判）。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c")
            if path == "/session/ses_c/message":
                return _child_msg(10, 1000)
            return None

        fp1, _, _ = self._fp(fake)
        fp2, _, _ = self._fp(fake)
        self.assertEqual(fp1, fp2)

    def test_child_reasoning_propagates(self):
        """子会话有未完结 reasoning → 父 reasoning_in_progress=True（保活）。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c")
            if path == "/session/ses_c/message":
                return _child_msg(10, 1000, reasoning_start=123)
            return None

        _, rip, _ = self._fp(fake)
        self.assertTrue(rip)

    def test_child_get_fail_biases_alive(self):
        """子会话 GET 全失败（一个指纹都取不到）→ 视为仍在产出，不误杀。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c")
            raise RuntimeError("child boom")

        _, rip, _ = self._fp(fake)
        self.assertTrue(rip)

    def test_child_sid_registered_for_sse_suppress(self):
        """发现的子会话 sid 登记进 textreply 注册表（SSE 业务通知抑制）。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_child_x")
            if path == "/session/ses_child_x/message":
                return _child_msg(1, 1)
            return None

        self._fp(fake)
        self.assertTrue(core_brain.is_textreply_session("ses_child_x"))

    def test_completed_task_not_tracked(self):
        """子代理已完成的 task part 不再跟踪 → 指纹维持 4 维。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c", child_status="completed")
            raise AssertionError("不应再查子会话")

        fp, rip, _ = self._fp(fake)
        self.assertEqual(len(fp), 4)
        self.assertFalse(rip)

    def test_depth_limited(self):
        """子代理再委派（孙会话）也能跟上，但递归限深 2 层不无限展开。"""
        def fake(method, port, pwd, path, body=None, timeout=8):
            if path == "/session/ses_p/message":
                return _task_msg("ses_c1")
            if path == "/session/ses_c1/message":
                return _task_msg("ses_c2")
            if path == "/session/ses_c2/message":
                return _task_msg("ses_c3")   # 第 3 层：深度已到，不再递归
            raise AssertionError(f"不应查到 {path}")

        fp, _, _ = self._fp(fake)
        self.assertEqual(len(fp), 5)


class _FakeServeParentChild:
    """e2e 假 serve：POST message 阻塞；父会话静止（带 in-flight task part），
    子会话按 activity 产出。"""

    def __init__(self, child_activity="growing"):
        self.child_activity = child_activity
        self.unblock = threading.Event()
        self.calls = []
        self._tick = 0
        self.lock = threading.Lock()

    def __call__(self, method, port, pwd, path, body=None, timeout=8):
        with self.lock:
            self.calls.append((method, path))
        if method == "POST" and path == "/session":
            return {"id": "ses_1"}
        if method == "POST" and path.endswith("/abort"):
            self.unblock.set()
            return {}
        if method == "GET" and path == "/session/ses_1/message":
            return _task_msg("ses_child")
        if method == "GET" and path == "/session/ses_child/message":
            with self.lock:
                self._tick += 1
            if self.child_activity == "growing":
                return _child_msg(10 * self._tick, 1000 + self._tick)
            return _child_msg(10, 1000)      # static
        if method == "POST" and path.endswith("/message"):
            self.unblock.wait(timeout=5)
            return {"parts": [{"type": "text", "text": "done"}]}
        return None


class TestWatchdogE2E(unittest.TestCase):
    def setUp(self):
        brain._reset_sessions()

    def _run(self, fake, **overrides):
        cfg = {"_BRAIN": "opencode", "_SESSION_REUSE": True,
               "_OPENCODE_ACTIVITY_POLL": 1, "_OPENCODE_IDLE_TIMEOUT": 300,
               "_OPENCODE_MAX_TIMEOUT": 0, "_OPENCODE_SOCK_TIMEOUT": None,
               "_OPENCODE_ERROR_ABORT": 0}
        cfg.update(overrides)
        cli = MagicMock(return_value="CLI-SHOULD-NOT-RUN")
        ctx = {"conv_id": "cidT", "conv_type": "2", "msg_id": "m", "user": "u"}
        with patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
             patch.object(brain, "_serve_request", side_effect=fake), \
             patch.object(brain, "_brain_opencode_cli", cli), \
             patch.multiple(brain, **cfg):
            reply, status = brain.generate_reply_ex("u", "wx post 408", ctx=ctx)
        return reply, status, cli

    def test_delegated_long_task_not_killed(self):
        """父会话静止但子代理持续产出 → 不 abort，正常返回（#125 回归）。"""
        fake = _FakeServeParentChild(child_activity="growing")
        threading.Timer(0.25, fake.unblock.set).start()
        reply, status, cli = self._run(
            fake, _OPENCODE_IDLE_TIMEOUT=1, _OPENCODE_ACTIVITY_POLL=1)
        self.assertEqual(reply, "done")
        self.assertEqual(status, STATUS_OK)
        self.assertNotIn(("POST", "/session/ses_1/abort"), fake.calls)
        cli.assert_not_called()

    def test_delegated_stuck_task_still_aborts(self):
        """父子都静止 → 仍按 idle abort（真卡死检测不被破坏）。"""
        fake = _FakeServeParentChild(child_activity="static")
        reply, status, cli = self._run(
            fake, _OPENCODE_IDLE_TIMEOUT=1, _OPENCODE_ACTIVITY_POLL=1)
        self.assertEqual(reply, "")
        self.assertEqual(status, STATUS_FAILED)
        self.assertTrue(any(p.endswith("/abort") for _, p in fake.calls))
        cli.assert_not_called()


class TestSseSuppressCapability(unittest.TestCase):
    def setUp(self):
        brain._reset_sessions()
        from custom.capabilities import sse_suppress
        self.cap = sse_suppress
        with sse_suppress._cache_lock:
            sse_suppress._child_cache.clear()

    def _busy(self, sid):
        return {"type": "session.status",
                "properties": {"sessionID": sid, "status": {"type": "busy"}}}

    def _idle(self, sid):
        return {"type": "session.idle", "properties": {"sessionID": sid}}

    def test_child_session_suppressed(self):
        """parentID 非空（Task 子代理会话）→ busy/idle 都吞掉。"""
        with patch.object(self.cap, "serve_request",
                          return_value={"id": "ses_sub1", "parentID": "ses_p"}):
            self.assertTrue(self.cap.on_sse_event(self._busy("ses_sub1"), 4096, "pw"))
            self.assertTrue(self.cap.on_sse_event(self._idle("ses_sub1"), 4096, "pw"))

    def test_business_session_passthrough(self):
        """parentID 为空（未登记业务 session）→ 放行走 core 默认转发。"""
        with patch.object(self.cap, "serve_request",
                          return_value={"id": "ses_biz", "parentID": None}):
            self.assertFalse(self.cap.on_sse_event(self._idle("ses_biz"), 4096, "pw"))

    def test_registered_left_to_text_reply(self):
        core_brain.register_session("ses_biz", {"conv_id": "c"})
        with patch.object(self.cap, "serve_request") as sr:
            self.assertFalse(self.cap.on_sse_event(self._busy("ses_biz"), 4096, "pw"))
            sr.assert_not_called()   # 已登记 → 不查 serve，交 text_reply 抑制

    def test_get_fail_passthrough(self):
        """serve 查询失败 → 放行（偏保守，不误吞业务事件），且不缓存。"""
        with patch.object(self.cap, "serve_request", side_effect=RuntimeError("boom")):
            self.assertFalse(self.cap.on_sse_event(self._idle("ses_x"), 4096, "pw"))
        with self.cap._cache_lock:
            self.assertNotIn("ses_x", self.cap._child_cache)

    def test_result_cached(self):
        with patch.object(self.cap, "serve_request",
                          return_value={"parentID": "ses_p"}) as sr:
            self.cap.on_sse_event(self._busy("ses_sub2"), 4096, "pw")
            self.cap.on_sse_event(self._idle("ses_sub2"), 4096, "pw")
        self.assertEqual(sr.call_count, 1)

    def test_non_busy_status_passthrough(self):
        ev = {"type": "session.status",
              "properties": {"sessionID": "ses_x", "status": {"type": "idle"}}}
        self.assertFalse(self.cap.on_sse_event(ev, 4096, "pw"))

    def test_other_events_passthrough(self):
        ev = {"type": "message.part.updated", "properties": {"sessionID": "ses_x"}}
        self.assertFalse(self.cap.on_sse_event(ev, 4096, "pw"))
        self.assertFalse(self.cap.on_sse_event({"type": "session.idle", "properties": {}}, 4096, "pw"))

    def test_capability_registered_and_enabled(self):
        from core.capabilities import enabled_capabilities
        names = [c.name for c in enabled_capabilities()]
        self.assertIn("sse_suppress", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
