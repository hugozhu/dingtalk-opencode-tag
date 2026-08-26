#!/usr/bin/env python3
"""test_brain_turn_error.py — serve 回合带错收尾（info.error）不再伪装成空回复

事故复现（2026-08-21）：模型网关 dev2:4000 失联 ~80 分钟，serve POST /message 仍返回
HTTP 200，但回合 info.error 记着 APIError("Cannot connect to API")。旧代码只拼 text
parts（空）→ status=empty → text_reply 静默吞掉、ack 永远「仍在处理中」心跳到超时。

本单测覆盖 custom/brain.py 修复后的语义：
  1. _post_message：200 但 info.error 且无文本产出 → 抛 _ServeTurnError。
  2. _post_message：有文本产出时即使带 error 也不抛（部分产出照常交付）。
  3. 复用会话回合带错 → 中毒自愈：丢弃重建一次重试，重试成功 → ok（行为同回空自愈）。
  4. 新会话/重试仍带错 + CLI 也失败 → ("", failed)（上层发兜底 + ack 落失败终态）。
  5. 回合带错 + CLI 回退成功 → 回复照常（HTTP 不可用回退语义不变）。

不依赖网络：全程 patch brain._serve_request。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom import brain
from core.brain import STATUS_OK, STATUS_EMPTY, STATUS_FAILED

brain._OPENCODE_LOG = os.path.join(tempfile.gettempdir(), "opencode_test.log")


def _turn_err_body():
    """serve 200 返回体：回合以 APIError 收尾、无文本（事故现场结构）。"""
    return {
        "info": {
            "tokens": {"input": 0, "output": 0},
            "error": {"name": "APIError",
                      "data": {"message": "Cannot connect to API: Unable to connect."}},
        },
        "parts": [],
    }


def _ok_body(text):
    return {"info": {"tokens": {"input": 1, "output": 1}},
            "parts": [{"type": "text", "text": text}]}


class _FakeServe:
    """按 POST /message 次序回放响应序列（消耗完则用最后一个）。"""

    def __init__(self, message_bodies):
        self.bodies = list(message_bodies)
        self.calls = []
        self._n = 0
        self._sid = 0

    def __call__(self, method, port, pwd, path, body=None, timeout=8):
        self.calls.append((method, path))
        if method == "POST" and path == "/session":
            self._sid += 1
            return {"id": f"ses_{self._sid}"}
        if method == "DELETE":
            return True
        if method == "GET" and path.endswith("/message"):
            return []
        if method == "POST" and path.endswith("/message"):
            b = self.bodies[min(self._n, len(self.bodies) - 1)]
            self._n += 1
            return b
        return None


def _ctx(conv_id="cidT"):
    return {"conv_id": conv_id, "conv_type": "2", "msg_id": "m", "user": "u"}


def _run_generate(fake, cli, reuse=True):
    cfg = {"_BRAIN": "opencode", "_SESSION_REUSE": reuse,
           "_OPENCODE_ACTIVITY_POLL": 60, "_OPENCODE_IDLE_TIMEOUT": 300,
           "_OPENCODE_MAX_TIMEOUT": 0, "_OPENCODE_SOCK_TIMEOUT": None,
           "_OPENCODE_EMPTY_RETRY": True}
    with patch.object(brain, "find_serve_credentials", return_value=(1, 4096, "pw")), \
         patch.object(brain, "_serve_request", side_effect=fake), \
         patch.object(brain, "_brain_opencode_cli", cli), \
         patch.multiple(brain, **cfg):
        return brain.generate_reply_ex("u", "hi", ctx=_ctx())


class TestPostMessageTurnError(unittest.TestCase):
    def test_error_no_text_raises(self):
        fake = _FakeServe([_turn_err_body()])
        with patch.object(brain, "_serve_request", side_effect=fake), \
             patch.multiple(brain, _OPENCODE_ACTIVITY_POLL=60,
                            _OPENCODE_IDLE_TIMEOUT=300, _OPENCODE_MAX_TIMEOUT=0,
                            _OPENCODE_SOCK_TIMEOUT=None):
            with self.assertRaises(brain._ServeTurnError) as cm:
                brain._post_message(4096, "pw", "ses_x", "hi", "local", "qwen3-8-max")
        self.assertIn("Cannot connect to API", str(cm.exception))

    def test_error_with_text_kept(self):
        body = _turn_err_body()
        body["parts"] = [{"type": "text", "text": "部分产出"}]
        fake = _FakeServe([body])
        with patch.object(brain, "_serve_request", side_effect=fake), \
             patch.multiple(brain, _OPENCODE_ACTIVITY_POLL=60,
                            _OPENCODE_IDLE_TIMEOUT=300, _OPENCODE_MAX_TIMEOUT=0,
                            _OPENCODE_SOCK_TIMEOUT=None):
            reply, _usage = brain._post_message(
                4096, "pw", "ses_x", "hi", "local", "qwen3-8-max")
        self.assertEqual(reply, "部分产出")


class TestGenerateReplyTurnError(unittest.TestCase):
    def setUp(self):
        brain._reset_sessions()

    def test_reuse_rebuild_retry_heals(self):
        """复用会话回合带错 → 丢弃重建一次，重试成功 → ok（中毒自愈保留）。"""
        brain._remember_sid("cidT", "ses_old")
        fake = _FakeServe([_turn_err_body(), _ok_body("好了")])
        cli = MagicMock(return_value="CLI-SHOULD-NOT-RUN")
        reply, status = _run_generate(fake, cli)
        self.assertEqual((reply, status), ("好了", STATUS_OK))
        cli.assert_not_called()
        self.assertEqual(sum(1 for m, p in fake.calls
                             if m == "POST" and p == "/session"), 1, "应重建一次会话")

    def test_new_session_error_cli_fail_is_failed(self):
        """事故场景：新会话回合带错 + CLI 回退也失败 → failed（不再静默 empty）。"""
        fake = _FakeServe([_turn_err_body()])
        cli = MagicMock(side_effect=RuntimeError("opencode run rc=1"))
        reply, status = _run_generate(fake, cli, reuse=False)
        self.assertEqual((reply, status), ("", STATUS_FAILED))
        cli.assert_called_once()

    def test_new_session_error_cli_ok_fallback(self):
        """回合带错但 CLI 回退成功 → 回复照常（回退语义不变）。"""
        fake = _FakeServe([_turn_err_body()])
        cli = MagicMock(return_value="CLI 回复")
        reply, status = _run_generate(fake, cli, reuse=False)
        self.assertEqual((reply, status), ("CLI 回复", STATUS_OK))

    def test_reuse_error_retry_still_error_is_failed(self):
        """复用会话重建重试后仍带错 + CLI 失败 → failed。"""
        brain._remember_sid("cidT", "ses_old")
        fake = _FakeServe([_turn_err_body(), _turn_err_body()])
        cli = MagicMock(side_effect=RuntimeError("opencode run rc=1"))
        reply, status = _run_generate(fake, cli)
        self.assertEqual((reply, status), ("", STATUS_FAILED))


if __name__ == "__main__":
    unittest.main()
