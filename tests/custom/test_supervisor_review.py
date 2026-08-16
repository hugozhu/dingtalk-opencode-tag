#!/usr/bin/env python3
"""supervisor_review — 主管审核回路单测。

覆盖：拦截非主管单聊 / 主管单聊放行 / 三种裁决（同意·改写·忽略）/ 编号对应 /
并发不串 / 超时兜底 / 群聊同样送审（#107，含主管自己在群里提问）/ 学习只在改写时发生。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from core.inbound import InboundMessage  # noqa: E402
from custom.capabilities import supervisor_review as sr  # noqa: E402


def _msg(user, text, conv_id="cidAsker", conv_type="1", msg_id="m1"):
    return InboundMessage(user=user, text=text, conv_type=conv_type,
                          conv_id=conv_id, msg_id=msg_id)


class _Base(unittest.TestCase):
    def setUp(self):
        sr._reset()
        self.tmpdir = tempfile.mkdtemp()
        self.kf = os.path.join(self.tmpdir, "qa.jsonl")
        # 模块属性在 import 时定型，测试直接 patch 属性（同 brain 知识注入测试）
        self.patches = [
            patch.object(sr, "_KNOWLEDGE_FILE", self.kf),
            patch.object(sr, "_TIMEOUT", 0),        # 0=不起定时器，超时单独测
            patch.object(sr, "_O2O_ONLY", False),   # 默认：单聊 + 群聊都审（#107）
        ]
        for p in self.patches:
            p.start()
        os.environ["AGENT_SUPERVISOR_USER_ID"] = "sup123"
        os.environ["AGENT_SUPERVISOR_NAME"] = "boss"
        os.environ["AGENT_SUPERVISOR_ALIASES"] = "老板"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        sr._reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for k in ("AGENT_SUPERVISOR_USER_ID", "AGENT_SUPERVISOR_NAME",
                  "AGENT_SUPERVISOR_ALIASES"):
            os.environ.pop(k, None)

    def _escalate(self, user="张三", text="问题", conv_id="cidZhang", msg_id="m1",
                  draft="AI草稿", conv_type="1"):
        """走一遍拦截 → 草稿 → 转交，返回 (send_to_sup mock, send_reply mock)。"""
        with patch.object(sr, "generate_reply_ex", return_value=(draft, "ok")), \
             patch.object(sr, "_send_to_supervisor", return_value=True) as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward(user, text, conv_type, conv_id, msg_id)
        return sup, rep


class TestInterception(_Base):
    def test_non_supervisor_o2o_intercepted(self):
        """非主管单聊被消费（不落到 text_reply），并提交后台生成草稿。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("张三", "你好")))
            sub.assert_called_once()

    def test_group_message_intercepted(self):
        """群聊同样送审（#107）：群里的回答是公开发言，比单聊更该先过主管。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("张三", "你好", conv_type="2")))
            sub.assert_called_once()

    def test_group_message_from_supervisor_intercepted(self):
        """主管在群里提问也送审（#107）—— 闸门管的是"数字员工说什么"，不是"谁在问"。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertTrue(sr.on_inbound(_msg("boss", "季度目标是啥", conv_type="2")))
            sub.assert_called_once()

    def test_group_message_from_supervisor_is_not_a_verdict(self):
        """裁决只认主管单聊：主管在群里说「同意」是新提问，不能放行别人的待审。"""
        self._escalate(user="张三", conv_id="cidZhang")
        with patch.object(sr, "submit_reply") as sub, \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "同意", conv_type="2",
                                               conv_id="cidGroup", msg_id="m9")))
        sub.assert_called_once()          # 当成新提问送审
        rep.assert_not_called()           # 张三的待审没被放行
        self.assertEqual(len(sr._pending), 1)

    def test_group_passes_through_when_o2o_only(self):
        """SUPERVISOR_REVIEW_O2O_ONLY=1 → 回到只审单聊的老行为。"""
        with patch.object(sr, "_O2O_ONLY", True), \
             patch.object(sr, "submit_reply") as sub:
            self.assertFalse(sr.on_inbound(_msg("张三", "你好", conv_type="2")))
            sub.assert_not_called()

    def test_group_card_marks_public_scene(self):
        """卡片必须标出群聊 —— 主管得知道这条答案会公开发出去。"""
        sup, _ = self._escalate(conv_type="2", conv_id="cidGroup")
        self.assertIn("群聊", sup.call_args[0][0])
        sup2, _ = self._escalate(conv_type="1", msg_id="m2")
        self.assertIn("单聊", sup2.call_args[0][0])

    def test_supervisor_without_pending_passes_through(self):
        """主管没有待审时正常对话 —— 必须放行给 text_reply，否则主管没法用数字员工。"""
        with patch.object(sr, "submit_reply") as sub:
            self.assertFalse(sr.on_inbound(_msg("boss", "今天天气如何")))
            sub.assert_not_called()

    def test_supervisor_alias_recognized(self):
        """别名也认作主管（bridge 只传显示名，可能是姓名或花名）。"""
        self.assertTrue(sr._is_supervisor("老板"))
        self.assertTrue(sr._is_supervisor("boss"))
        self.assertFalse(sr._is_supervisor("张三"))

    def test_no_supervisor_configured_passes_through(self):
        """没配主管 → 本能力等于关闭，不能把消息吞掉。"""
        for k in ("AGENT_SUPERVISOR_USER_ID", "AGENT_SUPERVISOR_NAME",
                  "AGENT_SUPERVISOR_ALIASES"):
            os.environ.pop(k, None)
        self.assertFalse(sr.on_inbound(_msg("张三", "你好")))

    def test_draft_not_sent_to_asker(self):
        """核心契约：草稿只发主管，**不发提问者**。"""
        sup, rep = self._escalate()
        sup.assert_called_once()
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)

    def test_card_contains_question_and_draft(self):
        sup, _ = self._escalate(text="报销怎么走", draft="找财务")
        card = sup.call_args[0][0]
        self.assertIn("报销怎么走", card)
        self.assertIn("找财务", card)
        self.assertIn("#1", card)

    def test_forward_failure_falls_back_to_asker(self):
        """转交主管失败不能把提问者永久挂着 → 回退直接回复。"""
        with patch.object(sr, "generate_reply_ex", return_value=("草稿", "ok")), \
             patch.object(sr, "_send_to_supervisor", return_value=False), \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        rep.assert_called_once()
        self.assertEqual(len(sr._pending), 0)

    def test_draft_failure_still_escalates(self):
        """模型挂了也要转交 —— 主管仍可手写答案，问题不能丢。"""
        with patch.object(sr, "generate_reply_ex", return_value=("", "failed")), \
             patch.object(sr, "_send_to_supervisor", return_value=True) as sup, \
             patch.object(sr, "send_reply") as rep:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        sup.assert_called_once()
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 1)


class TestVerdict(_Base):
    def test_approve_sends_draft(self):
        self._escalate(draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][0], "cidZhang")   # 发给提问者
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(len(sr._pending), 0)

    def test_approve_group_pending_replies_to_group(self):
        """群里提的问题，主管在单聊放行后答案要发回**那个群**（#107）。"""
        self._escalate(conv_id="cidGroup", conv_type="2", draft="AI草稿")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss")))
        self.assertEqual(rep.call_args[0][0], "cidGroup")
        self.assertEqual(rep.call_args[0][1], "2")     # 按群聊发，不是单聊
        self.assertEqual(rep.call_args[0][2], "AI草稿")

    def test_approve_does_not_learn(self):
        """同意 = AI 本来就对，没有新知识，不该写知识库。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertFalse(os.path.exists(self.kf))

    def test_rewrite_sends_supervisor_answer_and_learns(self):
        self._escalate(text="报销怎么走", draft="AI瞎猜")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1 找财务小王签字", conv_id="cidBoss"))
        self.assertEqual(rep.call_args[0][2], "找财务小王签字")
        with open(self.kf, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["question"], "报销怎么走")
        self.assertEqual(recs[0]["answer"], "找财务小王签字")

    def test_ignore_sends_nothing_to_asker(self):
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            self.assertTrue(sr.on_inbound(_msg("boss", "#1 忽略", conv_id="cidBoss")))
        rep.assert_not_called()
        self.assertEqual(len(sr._pending), 0)

    def test_verdict_without_seq_uses_latest(self):
        """不带编号 → 对应最近一条待审（单人场景零心智负担）。"""
        self._escalate(user="张三", conv_id="cidZhang", msg_id="m1")
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "同意", conv_id="cidBoss"))
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][0], "cidZhang")

    def test_unknown_seq_reports_and_consumes(self):
        """指名一个不存在的编号 → 告知主管，但不能放行去当普通对话。"""
        with patch.object(sr, "_send_to_supervisor") as sup:
            self.assertTrue(sr.on_inbound(_msg("boss", "#99 同意", conv_id="cidBoss")))
        self.assertIn("#99", sup.call_args[0][0])

    def test_concurrent_askers_do_not_cross(self):
        """两个提问者并发：#1/#2 各自对应正确，答案不能发错人。"""
        self._escalate(user="张三", text="Q1", conv_id="cidZhang", msg_id="m1")
        self._escalate(user="李四", text="Q2", conv_id="cidLi", msg_id="m2")
        self.assertEqual(len(sr._pending), 2)

        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1 答张三", conv_id="cidBoss"))
            sr.on_inbound(_msg("boss", "#2 答李四", conv_id="cidBoss"))

        calls = {c[0][0]: c[0][2] for c in rep.call_args_list}
        self.assertEqual(calls["cidZhang"], "答张三")
        self.assertEqual(calls["cidLi"], "答李四")

    def test_rewrite_empty_answer_rejected(self):
        """空答案不发出（避免给提问者发一条空消息）。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr.on_inbound(_msg("boss", "#1   ", conv_id="cidBoss"))
        rep.assert_not_called()


class TestParseVerdict(_Base):
    def test_parse_variants(self):
        self.assertEqual(sr._parse_verdict("#3 同意"), (3, "approve", ""))
        self.assertEqual(sr._parse_verdict("#12 忽略"), (12, "ignore", ""))
        self.assertEqual(sr._parse_verdict("同意"), (None, "approve", ""))
        self.assertEqual(sr._parse_verdict("#3 你应该这样答"), (3, "rewrite", "你应该这样答"))
        self.assertEqual(sr._parse_verdict("直接写答案"), (None, "rewrite", "直接写答案"))

    def test_parse_is_case_insensitive(self):
        self.assertEqual(sr._parse_verdict("#1 OK")[1], "approve")
        self.assertEqual(sr._parse_verdict("#1 Yes")[1], "approve")


class TestTimeout(_Base):
    def test_timeout_releases_draft_to_asker(self):
        """主管一直不回 → 超时把草稿发给提问者，不能无限期挂着。"""
        self._escalate(draft="AI草稿")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr._timeout(seq)
        rep.assert_called_once()
        self.assertEqual(rep.call_args[0][2], "AI草稿")
        self.assertEqual(len(sr._pending), 0)

    def test_timeout_after_verdict_is_noop(self):
        """已裁决后定时器才触发 → 不能重复发第二条。"""
        self._escalate()
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"):
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        with patch.object(sr, "_send_to_supervisor"), \
             patch.object(sr, "send_reply") as rep:
            sr._timeout(seq)
        rep.assert_not_called()


class TestAckClosure(_Base):
    """不发消息的出口必须显式收尾 ack（#108）。

    ack 的「处理中→完成」只由 send_reply 广播的 reply-sent 驱动。本能力有几条路径
    压根不产生 send_reply，不收尾 → 那条消息的 ack worker 每 5 分钟往会话播一条
    「仍在处理中」直到 65 分钟超时（群聊里尤其刺眼：主管都决定忽略了还在报进度）。
    """

    def test_ignore_closes_asker_ack_silently(self):
        """忽略 → 提问者那条静默收尾（ok=None）：没答就别贴「完成」。"""
        self._escalate(conv_id="cidGroup", conv_type="2")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 忽略", conv_id="cidBoss"))
        self.assertIn(("cidGroup", "2", None), [c[0] for c in disp.call_args_list])

    def test_verdict_message_closes_its_own_ack(self):
        """主管的裁决消息自己也挂着 worker：回执裸发不经 send_reply，必须显式收尾。"""
        self._escalate()
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertIn(("cidBoss", "1", True), [c[0] for c in disp.call_args_list])

    def test_approve_does_not_double_close_asker(self):
        """同意 → 提问者那条由 send_reply 自带的 reply-sent 收尾，不该再手动发一次。"""
        self._escalate(conv_id="cidZhang")
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply"), \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr.on_inbound(_msg("boss", "#1 同意", conv_id="cidBoss"))
        self.assertEqual([c[0][0] for c in disp.call_args_list], ["cidBoss"])

    def test_supervisor_passthrough_does_not_close_ack(self):
        """主管无待审时是普通对话 → 交给 text_reply 回，提前收尾会把「处理中」掐掉。"""
        with patch.object(sr, "submit_reply"), patch.object(sr, "dispatch_reply_sent") as disp:
            self.assertFalse(sr.on_inbound(_msg("boss", "今天天气如何", conv_id="cidBoss")))
        disp.assert_not_called()

    def test_forward_failure_without_draft_closes_ack_failed(self):
        """草稿也没有、主管也没转成 → 什么都发不出去，落失败终态而非挂死。"""
        with patch.object(sr, "generate_reply_ex", return_value=("", "failed")), \
             patch.object(sr, "_send_to_supervisor", return_value=False), \
             patch.object(sr, "send_reply") as rep, \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr._draft_and_forward("张三", "问题", "1", "cidZhang", "m1")
        rep.assert_not_called()
        disp.assert_called_once_with("cidZhang", "1", False)

    def test_timeout_without_draft_closes_ack_failed(self):
        """超时且无草稿可放行 → 提问者什么也收不到，同样要收尾。"""
        self._escalate(draft="")
        seq = max(sr._pending)
        with patch.object(sr, "_send_to_supervisor"), patch.object(sr, "send_reply") as rep, \
             patch.object(sr, "dispatch_reply_sent") as disp:
            sr._timeout(seq)
        rep.assert_not_called()
        disp.assert_called_once_with("cidZhang", "1", False)

    def test_close_ack_survives_dispatch_failure(self):
        """收尾是 best-effort —— 广播抛错不能拖垮审核主流程。"""
        def boom(*a):
            raise RuntimeError("dispatch down")
        with patch.object(sr, "dispatch_reply_sent", boom):
            sr._close_ack("cidZhang", "1", True)   # 不应抛


class TestCapabilityWiring(_Base):
    def test_priority_between_forward_and_text_reply(self):
        """必须晚于 question(20)、早于 text_reply(100)，否则拦不住草稿外发。"""
        self.assertGreater(sr.CAPABILITY.priority, 20)
        self.assertLess(sr.CAPABILITY.priority, 100)

    def test_default_disabled(self):
        """改变默认回复行为的能力不该悄悄生效。"""
        self.assertFalse(sr.CAPABILITY.default_enabled)

    def test_loop_guard_and_dedup_on(self):
        self.assertTrue(sr.CAPABILITY.loop_guard)
        self.assertTrue(sr.CAPABILITY.dedup)


if __name__ == "__main__":
    unittest.main()
