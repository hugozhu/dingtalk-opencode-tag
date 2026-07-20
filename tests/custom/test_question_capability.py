#!/usr/bin/env python3
"""test_question_capability.py — Question 交互能力单测（custom）

覆盖：渲染、_match_option 三级匹配、单选自动提交、多选累积+提交、取消、超时 reject、
未匹配放行、session→群 路由、pending 状态。mock serve HTTP（_post_question）+ send_reply。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.builtin_caps import question as Q
from core.inbound import InboundMessage, KIND_TEXT

_SINGLE = [{"question": "晚饭吃什么？", "header": "", "multiple": False,
            "options": [{"label": "面", "description": ""}, {"label": "饭", "description": ""}]}]
_MULTI = [{"question": "选配料", "header": "", "multiple": True,
           "options": [{"label": "葱", "description": ""}, {"label": "蒜", "description": ""},
                       {"label": "辣", "description": ""}]}]


class TestRenderMatch(unittest.TestCase):
    def test_render_has_numbers_and_footer(self):
        s = Q._render_question(_SINGLE)
        self.assertIn("1. 面", s)
        self.assertIn("2. 饭", s)
        self.assertIn("取消", s)

    def test_render_multi_shows_submit(self):
        self.assertIn("提交", Q._render_question(_MULTI))

    def test_match_by_index(self):
        self.assertEqual(Q._match_option("1", _SINGLE, set()), (0, "面"))

    def test_match_by_label(self):
        self.assertEqual(Q._match_option("饭", _SINGLE, set()), (0, "饭"))

    def test_match_by_contains(self):
        self.assertEqual(Q._match_option("我想吃面", _SINGLE, set()), (0, "面"))

    def test_no_match(self):
        self.assertIsNone(Q._match_option("随便啦", _SINGLE, set()))

    def test_answered_single_skipped(self):
        # 单选第0题已答 → 序号不再命中它
        self.assertIsNone(Q._match_option("1", _SINGLE, {0}))


class TestOnSSE(unittest.TestCase):
    def setUp(self):
        Q._reset()

    def _evt(self, sid="ses_x", req="que_1", questions=_SINGLE):
        return {"type": "question.asked",
                "properties": {"id": req, "sessionID": sid, "questions": questions}}

    def test_asked_renders_to_source_group(self):
        with patch.object(Q, "session_conv", return_value={"conv_id": "cid==", "conv_type": "2"}), \
             patch.object(Q, "send_reply") as snd, \
             patch("threading.Timer"):  # 不真起定时器
            consumed = Q.on_sse_event(self._evt(), 4096, "pw")
        self.assertTrue(consumed)
        snd.assert_called_once()
        self.assertEqual(snd.call_args[0][0], "cid==")     # 发到来源群
        self.assertIn("需要你的输入", snd.call_args[0][2])
        # pending 记录建立
        self.assertIn("que_1", Q._pending)

    def test_no_conv_mapping_not_claimed(self):
        with patch.object(Q, "session_conv", return_value=None), \
             patch.object(Q, "send_reply") as snd:
            self.assertFalse(Q.on_sse_event(self._evt(), 4096, "pw"))
            snd.assert_not_called()

    def test_non_question_event_ignored(self):
        self.assertFalse(Q.on_sse_event({"type": "session.idle"}, 4096, "pw"))


class TestOnInbound(unittest.TestCase):
    def setUp(self):
        Q._reset()

    def _seed(self, questions=_SINGLE, conv="cid=="):
        # 直接塞一个 pending（跳过 SSE），timer 用假的
        import threading
        t = threading.Timer(999, lambda: None); t.daemon = True
        Q._pending["que_1"] = {"sid": "ses_x", "conv_id": conv, "conv_type": "2",
                               "questions": questions, "answers": {}, "timer": t}

    def _msg(self, text, conv="cid=="):
        return InboundMessage(user="u", text=text, conv_type="2", conv_id=conv,
                              msg_id="m==", kind=KIND_TEXT)

    def test_no_pending_passes_through(self):
        self.assertFalse(Q.on_inbound(self._msg("1")))  # 无 pending → 放行

    def test_single_answer_auto_submits(self):
        self._seed()
        with patch.object(Q, "_post_question", return_value=(True, "ok")) as pq, \
             patch.object(Q, "send_reply"):
            consumed = Q.on_inbound(self._msg("1"))
        self.assertTrue(consumed)
        pq.assert_called_once()
        self.assertEqual(pq.call_args[0][1], "reply")
        self.assertEqual(pq.call_args[0][2], [["面"]])   # answers_arr
        self.assertNotIn("que_1", Q._pending)            # 提交后 pop

    def test_unmatched_reply_passes_through(self):
        self._seed()
        with patch.object(Q, "send_reply"):
            self.assertFalse(Q.on_inbound(self._msg("今天天气不错")))  # 放行给 text_reply

    def test_cancel_rejects(self):
        self._seed()
        with patch.object(Q, "_post_question", return_value=(True, "ok")) as pq, \
             patch.object(Q, "send_reply"):
            self.assertTrue(Q.on_inbound(self._msg("取消")))
        self.assertEqual(pq.call_args[0][1], "reject")
        self.assertNotIn("que_1", Q._pending)

    def test_multi_accumulate_then_submit(self):
        self._seed(questions=_MULTI)
        with patch.object(Q, "_post_question", return_value=(True, "ok")) as pq, \
             patch.object(Q, "send_reply"):
            Q.on_inbound(self._msg("1"))   # 葱
            Q.on_inbound(self._msg("3"))   # 辣
            self.assertIn("que_1", Q._pending)   # 多选不自动提交
            Q.on_inbound(self._msg("提交"))
        self.assertEqual(pq.call_args[0][1], "reply")
        self.assertEqual(pq.call_args[0][2], [["葱", "辣"]])
        self.assertNotIn("que_1", Q._pending)

    def test_multi_toggle_off(self):
        self._seed(questions=_MULTI)
        with patch.object(Q, "send_reply"):
            Q.on_inbound(self._msg("1"))   # 选葱
            Q.on_inbound(self._msg("1"))   # 再选 → 取消葱
        self.assertEqual(Q._pending["que_1"]["answers"][0], [])

    def test_timeout_rejects_and_notifies(self):
        self._seed()
        with patch.object(Q, "_post_question", return_value=(True, "ok")) as pq, \
             patch.object(Q, "send_reply") as snd:
            Q._timeout("que_1")
        self.assertEqual(pq.call_args[0][1], "reject")
        self.assertIn("超时", snd.call_args[0][2])
        self.assertNotIn("que_1", Q._pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
