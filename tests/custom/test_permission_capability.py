#!/usr/bin/env python3
"""test_permission_capability.py — 工具授权审批能力单测

覆盖：两代 asked 事件归一化、按 req_id 去重、渲染、关键词分类、同意(once)/总是(always)/
拒绝(reject) 回复、未匹配放行、超时 reject、replied 事件清 pending、session→群 路由、
无映射不认领。mock serve HTTP（_post_reply）+ send_reply。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.builtin_caps import permission as P
from core.inbound import InboundMessage, KIND_TEXT

_CONV = {"conv_id": "cid==", "conv_type": "2"}


def _evt_v2(req="per_1", sid="ses_x", action="bash",
            resources=("rm -rf build",), metadata=None):
    return {"type": "permission.v2.asked",
            "properties": {"id": req, "sessionID": sid, "action": action,
                           "resources": list(resources),
                           "metadata": metadata or {}}}


def _evt_v1(req="per_1", sid="ses_x", permission="bash", patterns=("ls *",)):
    return {"type": "permission.asked",
            "properties": {"id": req, "sessionID": sid, "permission": permission,
                           "patterns": list(patterns), "metadata": {}, "always": []}}


def _msg(text, conv_id="cid=="):
    return InboundMessage(kind=KIND_TEXT, user="hugozhu", text=text,
                          conv_id=conv_id, conv_type="2", msg_id="m1")


class TestNormalizeRender(unittest.TestCase):
    def test_normalize_v2(self):
        p = P._normalize(_evt_v2())
        self.assertEqual(p["api"], "v2")
        self.assertEqual(p["action"], "bash")
        self.assertEqual(p["resources"], ["rm -rf build"])

    def test_normalize_v1(self):
        p = P._normalize(_evt_v1())
        self.assertEqual(p["api"], "v1")
        self.assertEqual(p["action"], "bash")
        self.assertEqual(p["resources"], ["ls *"])

    def test_normalize_other_event_none(self):
        self.assertIsNone(P._normalize({"type": "session.idle", "properties": {}}))

    def test_render_has_action_and_footer(self):
        s = P._render(P._normalize(_evt_v2(metadata={"command": "rm -rf build"})))
        self.assertIn("bash", s)
        self.assertIn("rm -rf build", s)
        self.assertIn("同意", s)
        self.assertIn("拒绝", s)

    def test_classify(self):
        self.assertEqual(P._classify_reply("同意"), "once")
        self.assertEqual(P._classify_reply("ALLOW"), "once")
        self.assertEqual(P._classify_reply("总是"), "always")
        self.assertEqual(P._classify_reply("拒绝"), "reject")
        self.assertIsNone(P._classify_reply("帮我看下天气"))
        self.assertIsNone(P._classify_reply(""))


class TestOnSSE(unittest.TestCase):
    def setUp(self):
        P._reset()

    def test_asked_sends_to_source_group(self):
        with patch.object(P, "session_conv", return_value=dict(_CONV)), \
             patch.object(P, "send_reply") as snd, \
             patch("threading.Timer"):
            consumed = P.on_sse_event(_evt_v2(), 4096, "pw")
        self.assertTrue(consumed)
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")
        self.assertIn("需要授权", snd.call_args[0][2])
        self.assertIn("per_1", P._pending)
        self.assertEqual(P._pending["per_1"]["api"], "v2")

    def test_dual_generation_dedup(self):
        # 同一 req_id 两代事件都到 → 只发一次群消息，第二个事件也认领（消费）
        with patch.object(P, "session_conv", return_value=dict(_CONV)), \
             patch.object(P, "send_reply") as snd, \
             patch("threading.Timer"):
            self.assertTrue(P.on_sse_event(_evt_v2(), 4096, "pw"))
            self.assertTrue(P.on_sse_event(_evt_v1(), 4096, "pw"))
        snd.assert_called_once()

    def test_no_conv_mapping_not_claimed(self):
        with patch.object(P, "session_conv", return_value=None), \
             patch.object(P, "send_reply") as snd:
            self.assertFalse(P.on_sse_event(_evt_v2(), 4096, "pw"))
            snd.assert_not_called()

    def test_replied_event_clears_pending(self):
        with patch.object(P, "session_conv", return_value=dict(_CONV)), \
             patch.object(P, "send_reply"), \
             patch("threading.Timer"):
            P.on_sse_event(_evt_v2(), 4096, "pw")
        evt = {"type": "permission.v2.replied",
               "properties": {"sessionID": "ses_x", "requestID": "per_1", "reply": "once"}}
        self.assertTrue(P.on_sse_event(evt, 4096, "pw"))
        self.assertNotIn("per_1", P._pending)
        # 已清空后再来 replied → 不认领
        self.assertFalse(P.on_sse_event(evt, 4096, "pw"))

    def test_other_event_ignored(self):
        self.assertFalse(P.on_sse_event({"type": "session.idle", "properties": {}}, 4096, "pw"))


class TestOnInbound(unittest.TestCase):
    def setUp(self):
        P._reset()

    def _pend(self, evt=None):
        with patch.object(P, "session_conv", return_value=dict(_CONV)), \
             patch.object(P, "send_reply"), \
             patch("threading.Timer"):
            P.on_sse_event(evt or _evt_v2(), 4096, "pw")

    def test_no_pending_passes_through(self):
        self.assertFalse(P.on_inbound(_msg("同意")))

    def test_approve_once(self):
        self._pend()
        with patch.object(P, "_post_reply", return_value=(True, "ok")) as post, \
             patch.object(P, "send_reply") as snd:
            self.assertTrue(P.on_inbound(_msg("同意")))
        post.assert_called_once_with("per_1", "ses_x", "v2", "once")
        self.assertIn("已放行", snd.call_args[0][2])
        self.assertNotIn("per_1", P._pending)

    def test_approve_always(self):
        self._pend()
        with patch.object(P, "_post_reply", return_value=(True, "ok")) as post, \
             patch.object(P, "send_reply"):
            self.assertTrue(P.on_inbound(_msg("总是")))
        post.assert_called_once_with("per_1", "ses_x", "v2", "always")

    def test_reject(self):
        self._pend()
        with patch.object(P, "_post_reply", return_value=(True, "ok")) as post, \
             patch.object(P, "send_reply") as snd:
            self.assertTrue(P.on_inbound(_msg("拒绝")))
        post.assert_called_once_with("per_1", "ses_x", "v2", "reject")
        self.assertIn("已拒绝", snd.call_args[0][2])

    def test_v1_uses_v1_api(self):
        self._pend(_evt_v1())
        with patch.object(P, "_post_reply", return_value=(True, "ok")) as post, \
             patch.object(P, "send_reply"):
            P.on_inbound(_msg("同意"))
        post.assert_called_once_with("per_1", "ses_x", "v1", "once")

    def test_unmatched_passes_through(self):
        self._pend()
        with patch.object(P, "_post_reply") as post:
            self.assertFalse(P.on_inbound(_msg("顺便问下几点了")))
        post.assert_not_called()
        self.assertIn("per_1", P._pending)   # pending 保留

    def test_other_conv_passes_through(self):
        self._pend()
        self.assertFalse(P.on_inbound(_msg("同意", conv_id="other==")))
        self.assertIn("per_1", P._pending)

    def test_post_failure_notifies(self):
        self._pend()
        with patch.object(P, "_post_reply", return_value=(False, "HTTP 500")), \
             patch.object(P, "send_reply") as snd:
            self.assertTrue(P.on_inbound(_msg("同意")))
        self.assertIn("失败", snd.call_args[0][2])


class TestTimeout(unittest.TestCase):
    def setUp(self):
        P._reset()

    def test_timeout_rejects_and_notifies(self):
        with patch.object(P, "session_conv", return_value=dict(_CONV)), \
             patch.object(P, "send_reply"), \
             patch("threading.Timer"):
            P.on_sse_event(_evt_v2(), 4096, "pw")
        with patch.object(P, "_post_reply", return_value=(True, "ok")) as post, \
             patch.object(P, "send_reply") as snd:
            P._timeout("per_1")
        post.assert_called_once_with("per_1", "ses_x", "v2", "reject")
        self.assertIn("超时", snd.call_args[0][2])
        self.assertNotIn("per_1", P._pending)

    def test_timeout_after_answered_noop(self):
        with patch.object(P, "_post_reply") as post:
            P._timeout("per_gone")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
